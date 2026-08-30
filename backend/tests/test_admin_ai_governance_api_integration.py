from __future__ import annotations

import os
import secrets

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.bootstrap.admin import provision_platform_super_admin
from app.core.config import get_settings
from app.core.security import SecurityService
from app.database.mysql import mysql_session
from app.modules.evaluation.models import AiEvaluationRun
from app.modules.rbac.models import AdminOperationLog
from app.modules.system.models import OutboxEvent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def _admin_auth(client: AsyncClient, suffix: str) -> dict[str, str]:
    username = f"ai_governance_{suffix}"
    password = f"AI-Governance-{suffix}-Correct-Horse!"
    async for session in mysql_session():
        provisioning = await provision_platform_super_admin(
            session,
            SecurityService(get_settings()),
            username=username,
            password=password,
        )
    login = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": username,
            "password": password,
            "client": {"client_type": "web", "device_name": "AI governance acceptance"},
        },
    )
    assert login.status_code == 200, login.text
    mfa = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"ai-governance-mfa-{suffix}"},
        json={
            "challenge_id": login.json()["data"]["challenge_id"],
            "method": "totp",
            "code": pyotp.TOTP(provisioning.totp_secret).now(),
        },
    )
    assert mfa.status_code == 200, mfa.text
    return {"Authorization": f"Bearer {mfa.json()['data']['session']['access_token']}"}


async def test_admin_dashboard_observability_evaluation_policy_and_skill_lifecycle(
    client: AsyncClient,
) -> None:
    assert (await client.get("/api/v1/admin/observability")).status_code == 401
    suffix = secrets.token_hex(5)
    auth = await _admin_auth(client, suffix)

    dashboard = await client.get("/api/v1/admin/dashboard", headers=auth)
    assert dashboard.status_code == 200, dashboard.text
    dashboard_data = dashboard.json()["data"]
    assert {"scope_type": "platform", "scope_id": 0} in dashboard_data["scopes"]
    # A platform administrator is deliberately not a consumer identity.
    assert dashboard_data["active_user_count"] == 0
    assert dashboard.headers["cache-control"] == "no-store"

    observability = await client.get("/api/v1/admin/observability", headers=auth)
    assert observability.status_code == 200, observability.text
    observability_data = observability.json()["data"]
    assert observability_data["trace_backend"] == "tempo"
    assert observability_data["log_backend"] == "loki"
    assert observability_data["sensitive_content_included"] is False
    assert isinstance(observability_data["metrics"], dict)

    evaluation_payload = {
        "dataset_id": "ecom-ai-release-holdout",
        "dataset_version": "2026.08.31-v2",
        "baseline_type": "agent",
        "baseline_version": "exclusive-v1",
        "candidate_type": "agent",
        "candidate_version": f"exclusive-{suffix}",
        "require_significant_gain": True,
    }
    evaluation = await client.post(
        "/api/v1/admin/ai/evaluations", headers=auth, json=evaluation_payload
    )
    assert evaluation.status_code == 202, evaluation.text
    evaluation_data = evaluation.json()["data"]
    assert evaluation_data["status"] == "queued"
    assert evaluation_data["release_gate"] is None
    assert evaluation_data["dataset_sha256"]
    evaluation_list = await client.get("/api/v1/admin/ai/evaluations", headers=auth)
    assert evaluation_list.status_code == 200
    assert evaluation_data["evaluation_id"] in {
        item["evaluation_id"] for item in evaluation_list.json()["data"]["items"]
    }

    skill_code = f"acceptance.skill-{suffix}"
    created_skill = await client.post(
        "/api/v1/admin/ai/skills",
        headers=auth,
        json={"skill_code": skill_code, "display_name": "验收 Skill"},
    )
    assert created_skill.status_code == 200, created_skill.text
    skill_id = created_skill.json()["data"]["skill_id"]
    duplicate_skill = await client.post(
        "/api/v1/admin/ai/skills",
        headers=auth,
        json={"skill_code": skill_code, "display_name": "重复 Skill"},
    )
    assert duplicate_skill.status_code == 409
    assert duplicate_skill.json()["code"] == "SKILL_CODE_EXISTS"
    version = await client.post(
        f"/api/v1/admin/ai/skills/{skill_id}/versions",
        headers=auth,
        json={
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
            "instructions": "仅根据授权范围内的可靠知识回答。",
            "evaluation_report": {"passed": True, "suite": "acceptance-v1"},
        },
    )
    assert version.status_code == 200, version.text
    assert version.json()["data"]["latest_version"] == 1
    publication_headers = {
        **auth,
        "Idempotency-Key": f"skill-publication-{suffix}-001",
    }
    publication = await client.post(
        f"/api/v1/admin/ai/skills/{skill_id}/versions/1/publications",
        headers=publication_headers,
    )
    assert publication.status_code == 202, publication.text
    assert publication.json()["data"]["command_status"] == "approval_required"
    publication_replay = await client.post(
        f"/api/v1/admin/ai/skills/{skill_id}/versions/1/publications",
        headers=publication_headers,
    )
    assert publication_replay.status_code == 202
    assert publication_replay.json()["data"] == publication.json()["data"]
    skills = await client.get("/api/v1/admin/ai/skills", headers=auth)
    assert skills.status_code == 200
    assert skill_id in {item["skill_id"] for item in skills.json()["data"]["items"]}

    mcp_servers = await client.get("/api/v1/admin/ai/mcp-servers", headers=auth)
    assert mcp_servers.status_code == 200
    assert {"catalog-mcp", "order-mcp", "memory-mcp"} <= {
        item["server_code"] for item in mcp_servers.json()["data"]["items"]
    }
    kill_target = f"acceptance-{suffix}"
    activated = await client.post(
        f"/api/v1/admin/ai/kill-switches/skill/{kill_target}/activations",
        headers=auth,
        json={"reason": "验收期间阻断目标 Skill"},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["data"]["is_active"] is True
    switches = await client.get("/api/v1/admin/ai/kill-switches", headers=auth)
    assert kill_target in {item["target_code"] for item in switches.json()["data"]["items"]}
    deactivated = await client.post(
        f"/api/v1/admin/ai/kill-switches/skill/{kill_target}/deactivations",
        headers=auth,
        json={"reason": "验收结束恢复目标 Skill"},
    )
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["data"]["is_active"] is False

    async for session in mysql_session():
        evaluation_row = await session.scalar(
            select(AiEvaluationRun).where(
                AiEvaluationRun.evaluation_run_no == evaluation_data["evaluation_id"]
            )
        )
        outbox = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_no == evaluation_data["evaluation_id"],
                OutboxEvent.event_type == "ai.evaluation.requested.v1",
            )
        )
        audit_actions = set(
            (
                await session.scalars(
                    select(AdminOperationLog.action).where(
                        AdminOperationLog.action.in_(
                            [
                                "ai.evaluation.run",
                                "skill.create",
                                "skill.version.create",
                                "ai.kill_switch.activate",
                                "ai.kill_switch.deactivate",
                            ]
                        )
                    )
                )
            ).all()
        )
        assert evaluation_row is not None and evaluation_row.run_status == "queued"
        assert outbox is not None and outbox.event_status == "pending"
        assert {
            "ai.evaluation.run",
            "skill.create",
            "skill.version.create",
            "ai.kill_switch.activate",
            "ai.kill_switch.deactivate",
        } <= audit_actions
