import os
import secrets

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.bootstrap.admin import provision_platform_super_admin
from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, canonical_request_hash, utc_now
from app.database.mysql import mysql_session
from app.modules.rbac.models import AdminApprovalRequest
from app.modules.system.models import DeadLetterEvent, OutboxEvent
from app.workers.admin_approval_worker import AdminApprovalWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_dead_letter_preview_requires_immutable_payload_and_dual_control(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    settings = get_settings()
    security = SecurityService(settings)
    password = f"Admin-Dead-Letter-{suffix}-Correct-Horse!"
    event_payload: dict[str, object] = {
        "schema_version": 1,
        "resource_id": f"resource_{suffix}",
        "operation": f"private-operation-{suffix}",
    }
    async for session in mysql_session():
        provisioning = await provision_platform_super_admin(
            session,
            security,
            username=f"dlq_admin_{suffix}",
            password=password,
        )
        source = OutboxEvent(
            event_no=new_prefixed_ulid("evt_"),
            event_type="integration.safe_rebuild.v1",
            aggregate_type="integration_resource",
            aggregate_no=f"resource_{suffix}",
            aggregate_version=3,
            payload=event_payload,
            event_status="failed",
            available_at=now,
            published_at=now,
            attempt_count=5,
            last_error_code="INTEGRATION_FAILURE",
            trace_id=f"trace_{suffix}",
        )
        session.add(source)
        await session.flush()
        dead = DeadLetterEvent(
            dead_letter_no=new_prefixed_ulid("dlq_"),
            source_type="outbox",
            source_no=source.event_no,
            event_type=source.event_type,
            schema_version=1,
            scope_type="platform",
            scope_id=0,
            payload_redacted={"schema_version": 1, "resource_id": f"resource_{suffix}"},
            payload_hash=canonical_request_hash(event_payload),
            failure_count=5,
            first_failed_at=now,
            last_failed_at=now,
            last_error_code="INTEGRATION_FAILURE",
            last_error="安全脱敏后的集成测试错误",
            dead_status="open",
            replay_count=0,
            original_trace_id=source.trace_id,
        )
        session.add(dead)
        await session.commit()
        dead_no = dead.dead_letter_no
        source_no = source.event_no

    login = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": f"dlq_admin_{suffix}",
            "password": password,
            "client": {"client_type": "web", "device_name": "DLQ Admin Test"},
        },
    )
    challenge_id = login.json()["data"]["challenge_id"]
    mfa = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"dlq-mfa-{suffix}-001"},
        json={
            "challenge_id": challenge_id,
            "method": "totp",
            "code": pyotp.TOTP(provisioning.totp_secret).now(),
        },
    )
    assert mfa.status_code == 200, mfa.text
    headers = {"Authorization": f"Bearer {mfa.json()['data']['session']['access_token']}"}

    detail = await client.get(f"/api/v1/admin/dead-letter-events/{dead_no}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert f"private-operation-{suffix}" not in detail.text
    assert detail.json()["data"]["payload_keys"] == ["resource_id", "schema_version"]
    preview = await client.post(
        f"/api/v1/admin/dead-letter-events/{dead_no}/replay-previews",
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["replayable"] is True
    assert preview.json()["data"]["required_approval_count"] == 2

    replay = await client.post(
        f"/api/v1/admin/dead-letter-events/{dead_no}/replays",
        headers={
            **headers,
            "If-Match": preview.headers["etag"],
            "Idempotency-Key": f"dlq-replay-{suffix}-001",
        },
        json={
            "preview_token": preview.json()["data"]["preview_token"],
            "reason_code": "CONTROLLED_EVENT_REPLAY",
            "reason": "已确认消费者幂等并申请受控重放",
        },
    )
    assert replay.status_code == 202, replay.text
    approval_no = replay.json()["data"]["approval_request_id"]
    assert replay.json()["data"]["required_approval_count"] == 2

    async for session in mysql_session():
        source = await session.scalar(select(OutboxEvent).where(OutboxEvent.event_no == source_no))
        dead = await session.scalar(
            select(DeadLetterEvent).where(DeadLetterEvent.dead_letter_no == dead_no)
        )
        approval = await session.scalar(
            select(AdminApprovalRequest).where(
                AdminApprovalRequest.approval_request_no == approval_no
            )
        )
        assert source is not None and source.event_status == "failed"
        assert dead is not None and dead.dead_status == "open" and dead.replay_count == 0
        assert approval is not None
        assert approval.action_code == "events.dead_letter.replay.v1"
        assert approval.required_approval_count == 2
        assert (
            "events.dead_letter.replay.v1"
            in AdminApprovalWorker(session, settings, security).executor.handlers
        )
