from __future__ import annotations

import os
import secrets

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.bootstrap.admin import provision_platform_super_admin
from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.modules.agent_runtime.models import AgentDefinition, AgentRun, AgentVersion
from app.modules.agent_runtime.operations_approval import (
    build_operations_approval,
    execute_operations_approval,
    prepare_operations_action,
)
from app.modules.agent_runtime.operations_context import ADMIN_TOOLS, TrustedOperationsContext
from app.modules.evaluation.models import AiEvaluationRun
from app.modules.identity.models import User
from app.modules.knowledge.models import SkillDefinition, SkillVersion
from app.modules.messaging.models import Conversation, Message
from app.modules.rbac.models import AdminApprovalRequest, AdminOperationLog
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
        "dataset_version": "2026.09.01-v3",
        "baseline_type": "prompt",
        "baseline_version": "ecom-safe-router-v1",
        "candidate_type": "prompt",
        "candidate_version": "ecom-safe-router-v3",
        "require_significant_gain": True,
    }
    evaluation_headers = {**auth, "Idempotency-Key": f"evaluation-{suffix}"}
    evaluation = await client.post(
        "/api/v1/admin/ai/evaluations", headers=evaluation_headers, json=evaluation_payload
    )
    assert evaluation.status_code == 202, evaluation.text
    evaluation_data = evaluation.json()["data"]
    assert evaluation_data["status"] == "queued"
    assert evaluation_data["release_gate"] is None
    assert evaluation_data["dataset_sha256"]
    replay = await client.post(
        "/api/v1/admin/ai/evaluations", headers=evaluation_headers, json=evaluation_payload
    )
    assert replay.status_code == 202
    assert replay.json()["data"]["evaluation_id"] == evaluation_data["evaluation_id"]
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


async def test_admin_ai_manager_requests_skill_publication_through_dual_control(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    username = f"ai_governance_{suffix}"
    auth = await _admin_auth(client, suffix)
    created_skill = await client.post(
        "/api/v1/admin/ai/skills",
        headers=auth,
        json={
            "skill_code": f"agent.release-{suffix}",
            "display_name": "AI 管家发布审批验收",
        },
    )
    assert created_skill.status_code == 200, created_skill.text
    skill_no = created_skill.json()["data"]["skill_id"]
    created_version = await client.post(
        f"/api/v1/admin/ai/skills/{skill_no}/versions",
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
            "instructions": "只处理授权范围内的发布治理任务。",
            "evaluation_report": {"passed": True, "suite": "agent-release-v1"},
        },
    )
    assert created_version.status_code == 200, created_version.text

    now = utc_now()
    admin_user_id = 0
    async for session in mysql_session():
        admin_user = await session.scalar(select(User).where(User.username == username))
        definition = await session.scalar(
            select(AgentDefinition).where(AgentDefinition.agent_code == "admin_copilot")
        )
        assert admin_user is not None and definition is not None
        admin_user_id = admin_user.id
        agent_version = await session.scalar(
            select(AgentVersion)
            .where(
                AgentVersion.agent_id == definition.id,
                AgentVersion.version_status == "published",
            )
            .order_by(AgentVersion.version_no.desc())
        )
        assert agent_version is not None
        conversation = Conversation(
            conversation_no=new_prefixed_ulid("cv_"),
            user_id=admin_user.id,
            conversation_type="exclusive",
            is_fixed=True,
            conversation_status="active",
            last_sequence_no=1,
            last_message_at=now,
        )
        session.add(conversation)
        await session.flush()
        trigger = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=1,
            sender_type="user",
            sender_id=admin_user.id,
            message_type="text",
            text_content=f"请发布 Skill {skill_no} 的版本 1 并发起审批",
            message_status="sent",
            moderation_status="passed",
            sent_at=now,
        )
        session.add(trigger)
        await session.flush()
        run = AgentRun(
            run_no=new_prefixed_ulid("run_"),
            conversation_id=conversation.id,
            trigger_message_id=trigger.id,
            agent_version_id=agent_version.id,
            run_status="running",
            current_phase="executing",
            trace_id=new_prefixed_ulid("trc_"),
            context_snapshot=[],
        )
        session.add(run)
        await session.flush()
        context = TrustedOperationsContext(
            run=run,
            conversation=conversation,
            trigger=trigger,
            user=admin_user,
            agent_definition=definition,
            agent_version=agent_version,
            allowed_tools=ADMIN_TOOLS,
            audience="admin",
            store=None,
        )
        prepared, error = await prepare_operations_action(
            session, context, trigger.text_content or ""
        )
        assert error is None and prepared is not None
        assert prepared.action_type == "admin_ai_skill_publish_request"
        agent_approval = await build_operations_approval(session, context, prepared)
        agent_approval.approval_status = "approved"
        agent_approval.decision = "approve"
        agent_approval.decided_at = utc_now()
        agent_approval.version += 1
        status, answer, result, error_code = await execute_operations_approval(
            session, context, agent_approval
        )
        assert status == "succeeded" and error_code is None
        assert result["status"] == "approval_required"
        assert "尚未发布" in answer
        approval_no = str(result["approval_request_id"])
        await session.commit()

    async for session in mysql_session():
        approval = await session.scalar(
            select(AdminApprovalRequest).where(
                AdminApprovalRequest.approval_request_no == approval_no
            )
        )
        skill = await session.scalar(
            select(SkillDefinition).where(SkillDefinition.skill_no == skill_no)
        )
        assert approval is not None and skill is not None
        version = await session.scalar(
            select(SkillVersion).where(
                SkillVersion.skill_id == skill.id,
                SkillVersion.version_no == 1,
            )
        )
        assert approval.action_code == "ai.skill.publish.v1"
        assert approval.required_approval_count == 2
        assert approval.initiator_user_id == admin_user_id
        assert version is not None and version.version_status == "draft"


async def test_admin_ai_manager_creates_immutable_prompt_draft_without_switching_live_version(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    await _admin_auth(client, suffix=f"prompt_{suffix}")
    # _admin_auth derives the username from its suffix; reuse that exact identity.
    username = f"ai_governance_prompt_{suffix}"
    target_code = f"acceptance_prompt_{suffix}"
    target_no = new_prefixed_ulid("agt_")
    now = utc_now()

    async for session in mysql_session():
        admin_user = await session.scalar(select(User).where(User.username == username))
        runtime_definition = await session.scalar(
            select(AgentDefinition).where(AgentDefinition.agent_code == "admin_copilot")
        )
        assert admin_user is not None and runtime_definition is not None
        runtime_version = await session.scalar(
            select(AgentVersion)
            .where(
                AgentVersion.agent_id == runtime_definition.id,
                AgentVersion.version_status == "published",
            )
            .order_by(AgentVersion.version_no.desc())
        )
        assert runtime_version is not None
        target_definition = AgentDefinition(
            agent_no=target_no,
            agent_code=target_code,
            agent_type="exclusive",
            scope_type="platform",
            store_id=None,
            display_name=f"Prompt 验收 Agent {suffix}",
            agent_status="active",
        )
        session.add(target_definition)
        await session.flush()
        old_prompt = "你是线上验收 Agent，只回答已经授权的商城问题。"
        session.add(
            AgentVersion(
                agent_id=target_definition.id,
                version_no=1,
                version_status="published",
                system_prompt=old_prompt,
                model_profile="primary",
                tool_allowlist=["rag.policy.search"],
                policy_config={
                    "evaluation_report": {"passed": True},
                    "context_budget": 4096,
                },
                published_at=now,
            )
        )
        conversation = Conversation(
            conversation_no=new_prefixed_ulid("cv_"),
            user_id=admin_user.id,
            conversation_type="exclusive",
            is_fixed=True,
            conversation_status="active",
            last_sequence_no=1,
            last_message_at=now,
        )
        session.add(conversation)
        await session.flush()
        new_prompt = (
            "你是商城运营诊断 Agent。先核验实时业务事实，再给出可执行建议；"
            "证据不足时明确说明，并且所有写操作都停在确认卡。"
        )
        trigger = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=1,
            sender_type="user",
            sender_id=admin_user.id,
            message_type="text",
            text_content=f"把 Agent {target_code} 的系统提示词改为：{new_prompt}",
            message_status="sent",
            moderation_status="passed",
            sent_at=now,
        )
        session.add(trigger)
        await session.flush()
        run = AgentRun(
            run_no=new_prefixed_ulid("run_"),
            conversation_id=conversation.id,
            trigger_message_id=trigger.id,
            agent_version_id=runtime_version.id,
            run_status="running",
            current_phase="executing",
            trace_id=new_prefixed_ulid("trc_"),
            context_snapshot=[],
        )
        session.add(run)
        await session.flush()
        context = TrustedOperationsContext(
            run=run,
            conversation=conversation,
            trigger=trigger,
            user=admin_user,
            agent_definition=runtime_definition,
            agent_version=runtime_version,
            allowed_tools=ADMIN_TOOLS,
            audience="admin",
            store=None,
        )
        prepared, error = await prepare_operations_action(
            session, context, trigger.text_content or ""
        )
        assert error is None and prepared is not None
        assert prepared.action_type == "admin_ai_agent_prompt_draft_create"
        assert prepared.payload["next_version_no"] == 2
        assert prepared.payload["policy_config"] == {"context_budget": 4096}
        approval = await build_operations_approval(session, context, prepared)
        approval.approval_status = "approved"
        approval.decision = "approve"
        approval.decided_at = utc_now()
        approval.version += 1
        status, answer, result, error_code = await execute_operations_approval(
            session, context, approval
        )
        assert status == "succeeded" and error_code is None
        assert result == {
            "agent_id": target_no,
            "agent_code": target_code,
            "version_no": 2,
            "status": "draft",
            "evaluation_required": True,
            "published_version_changed": False,
        }
        assert "线上版本未改变" in answer
        await session.commit()

    async for session in mysql_session():
        target = await session.scalar(
            select(AgentDefinition).where(AgentDefinition.agent_no == target_no)
        )
        assert target is not None
        versions = list(
            (
                await session.scalars(
                    select(AgentVersion)
                    .where(AgentVersion.agent_id == target.id)
                    .order_by(AgentVersion.version_no)
                )
            ).all()
        )
        assert [item.version_status for item in versions] == ["published", "draft"]
        assert versions[0].system_prompt == old_prompt
        assert versions[1].system_prompt == new_prompt
        assert versions[1].tool_allowlist == ["rag.policy.search"]
        assert versions[1].policy_config == {"context_budget": 4096}
        audit = await session.scalar(
            select(AdminOperationLog).where(
                AdminOperationLog.action == "admin_ai_agent_prompt_draft_create",
                AdminOperationLog.target_no == f"{target_no}:v2",
            )
        )
        assert audit is not None

        # Build a second confirmation from v2, then simulate another administrator
        # creating v3 before this confirmation is consumed.  The stale approval
        # must fail instead of overwriting or silently creating a duplicate draft.
        admin_user = await session.scalar(select(User).where(User.username == username))
        runtime_definition = await session.scalar(
            select(AgentDefinition).where(AgentDefinition.agent_code == "admin_copilot")
        )
        runtime_version = await session.scalar(
            select(AgentVersion)
            .where(
                AgentVersion.agent_id == runtime_definition.id,
                AgentVersion.version_status == "published",
            )
            .order_by(AgentVersion.version_no.desc())
        ) if runtime_definition is not None else None
        conversation = await session.scalar(
            select(Conversation)
            .where(Conversation.user_id == admin_user.id, Conversation.is_fixed.is_(True))
            .order_by(Conversation.id.desc())
        ) if admin_user is not None else None
        assert admin_user is not None and runtime_definition is not None
        assert runtime_version is not None and conversation is not None
        stale_prompt = "你是待提交的并发验收 Agent，只使用实时证据并等待管理员确认。"
        conversation.last_sequence_no += 1
        stale_trigger = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=conversation.last_sequence_no,
            sender_type="user",
            sender_id=admin_user.id,
            message_type="text",
            text_content=f"把 Agent {target_code} 的系统提示词改为：{stale_prompt}",
            message_status="sent",
            moderation_status="passed",
            sent_at=utc_now(),
        )
        session.add(stale_trigger)
        await session.flush()
        stale_run = AgentRun(
            run_no=new_prefixed_ulid("run_"),
            conversation_id=conversation.id,
            trigger_message_id=stale_trigger.id,
            agent_version_id=runtime_version.id,
            run_status="running",
            current_phase="executing",
            trace_id=new_prefixed_ulid("trc_"),
            context_snapshot=[],
        )
        session.add(stale_run)
        await session.flush()
        stale_context = TrustedOperationsContext(
            run=stale_run,
            conversation=conversation,
            trigger=stale_trigger,
            user=admin_user,
            agent_definition=runtime_definition,
            agent_version=runtime_version,
            allowed_tools=ADMIN_TOOLS,
            audience="admin",
            store=None,
        )
        stale_prepared, stale_error = await prepare_operations_action(
            session, stale_context, stale_trigger.text_content or ""
        )
        assert stale_error is None and stale_prepared is not None
        assert stale_prepared.payload["base_version_no"] == 2
        assert stale_prepared.payload["next_version_no"] == 3
        stale_approval = await build_operations_approval(
            session, stale_context, stale_prepared
        )
        stale_approval.approval_status = "approved"
        stale_approval.decision = "approve"
        stale_approval.decided_at = utc_now()
        stale_approval.version += 1

        concurrent_prompt = "另一名管理员先创建的并发版本。"
        session.add(
            AgentVersion(
                agent_id=target.id,
                version_no=3,
                version_status="draft",
                system_prompt=concurrent_prompt,
                model_profile=versions[-1].model_profile,
                tool_allowlist=list(versions[-1].tool_allowlist),
                policy_config=dict(versions[-1].policy_config),
                published_at=None,
            )
        )
        target.version += 1
        await session.flush()

        stale_status, stale_answer, stale_result, stale_error_code = (
            await execute_operations_approval(
                session, stale_context, stale_approval
            )
        )
        assert stale_status == "failed"
        assert stale_result == {}
        assert stale_error_code == "AGENT_ACTION_RESOURCE_CHANGED"
        assert "重新发起" in stale_answer
        current_versions = list(
            (
                await session.scalars(
                    select(AgentVersion)
                    .where(AgentVersion.agent_id == target.id)
                    .order_by(AgentVersion.version_no)
                )
            ).all()
        )
        assert [item.version_no for item in current_versions] == [1, 2, 3]
        assert current_versions[-1].system_prompt == concurrent_prompt
        assert all(item.system_prompt != stale_prompt for item in current_versions)
        await session.commit()
