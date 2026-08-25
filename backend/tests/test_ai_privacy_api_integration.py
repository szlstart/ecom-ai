import os
import secrets
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.database.postgres import postgres_session
from app.modules.agent_runtime.models import AiMemoryCleanupTask, UserAgentConsent
from app.modules.identity.models import AuthSession, User
from app.workers.ai_memory_cleanup_worker import process_one

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with isolated MySQL and PostgreSQL",
    ),
]


async def test_ai_memory_owner_revision_tombstone_disable_and_retry(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    memory_no = new_prefixed_ulid("mem_")
    async for session in mysql_session():
        user, auth_session = _identity(security, suffix, now)
        other, other_session = _identity(security, f"other_{suffix}", now)
        session.add_all([user, other])
        await session.flush()
        auth_session.user_id = user.id
        other_session.user_id = other.id
        consent = UserAgentConsent(
            consent_no=new_prefixed_ulid("con_"),
            user_id=user.id,
            consent_type="personalization",
            scope_type="user",
            scope_no=None,
            policy_version="ai-personalization-v1",
            consent_status="active",
        )
        session.add_all([auth_session, other_session, consent])
        await session.commit()
        user_no = user.user_no
        consent_no = consent.consent_no
        user_token, _ = security.create_access_token(
            user_no=user.user_no,
            session_no=auth_session.session_no,
            audience="user",
            permission_version=user.permission_version,
        )
        other_token, _ = security.create_access_token(
            user_no=other.user_no,
            session_no=other_session.session_no,
            audience="user",
            permission_version=other.permission_version,
        )

    ciphertext = security.encrypt("ai-memory-content", "偏好深蓝色商品")
    content_hash = security.keyed_hash("ai-memory-content-hash", "偏好深蓝色商品")
    async for postgres in postgres_session():
        await postgres.execute(
            text(
                """INSERT INTO memory.items
                (memory_no,user_no,namespace,store_no,memory_type,safe_text,embedding,confidence,
                 memory_status,consent_no,expires_at,memory_key,content_ciphertext,content_hash,
                 dedupe_fingerprint,key_version,source_type,source_ref,consent_policy_version,
                 validation_snapshot,salience,data_classification,memory_risk_level,valid_from,version)
                VALUES (:memory_no,:user_no,'exclusive',NULL,'preference',NULL,NULL,0.900,
                 'active',:consent_no,now()+interval '180 days','shopping.preferred_colors',
                 :ciphertext,:content_hash,:dedupe,1,'user_confirmation','integration',
                 'ai-personalization-v1','{}'::jsonb,0.800,'L2','low',now(),0)"""
            ),
            {
                "memory_no": memory_no,
                "user_no": user_no,
                "consent_no": consent_no,
                "ciphertext": ciphertext,
                "content_hash": content_hash,
                "dedupe": security.keyed_hash("ai-memory-dedupe", f"{user_no}:blue"),
            },
        )
        await postgres.commit()

    headers = {"Authorization": f"Bearer {user_token}"}
    listed = await client.get("/api/v1/users/me/ai-memory-items", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json()["data"]["items"][0]["value"] == "偏好深蓝色商品"
    foreign = await client.get(
        "/api/v1/users/me/ai-memory-items",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert foreign.status_code == 200 and foreign.json()["data"]["items"] == []

    revised = await client.post(
        f"/api/v1/users/me/ai-memory-items/{memory_no}/revisions",
        headers={**headers, "If-Match": '"v0"'},
        json={"new_value": "偏好墨绿色商品", "reason_code": "USER_CORRECTION", "confirmed": True},
    )
    assert revised.status_code == 201, revised.text
    revised_memory = revised.json()["data"]
    assert revised_memory["value"] == "偏好墨绿色商品"
    assert revised_memory["memory_id"] != memory_no

    deleted = await client.request(
        "DELETE",
        f"/api/v1/users/me/ai-memory-items/{revised_memory['memory_id']}",
        headers={
            **headers,
            "If-Match": '"v0"',
            "Idempotency-Key": f"memory-delete-{suffix}-001",
        },
        json={"reason_code": "USER_REQUEST", "confirmed": True},
    )
    assert deleted.status_code == 202, deleted.text
    delete_task = deleted.json()["data"]
    assert delete_task["status"] == "queued"

    disabled = await client.post(
        "/api/v1/users/me/ai-personalization/disable-all",
        headers={**headers, "Idempotency-Key": f"disable-all-{suffix}-001"},
        json={
            "confirmation": "DISABLE_ALL_AI_PERSONALIZATION",
            "reason_code": "USER_REQUEST",
        },
    )
    assert disabled.status_code == 202, disabled.text
    disable_task = disabled.json()["data"]

    async for session in mysql_session():
        consent = await session.scalar(
            select(UserAgentConsent).where(UserAgentConsent.consent_no == consent_no)
        )
        task = await session.scalar(
            select(AiMemoryCleanupTask).where(
                AiMemoryCleanupTask.task_no == disable_task["cleanup_task_id"]
            )
        )
        assert consent is not None and consent.consent_status == "revoked"
        assert task is not None
        task.task_status = "failed"
        task.failed_count = 1
        task.error_code = "CACHE_DELETE_FAILED"
        task.version += 1
        failed_version = task.version
        await session.commit()

    task_path = f"/api/v1/users/me/ai-cleanup-tasks/{disable_task['cleanup_task_id']}"
    foreign_task = await client.get(task_path, headers={"Authorization": f"Bearer {other_token}"})
    assert foreign_task.status_code == 404
    retried = await client.post(
        f"{task_path}/retries",
        headers={
            **headers,
            "If-Match": f'"v{failed_version}"',
            "Idempotency-Key": f"cleanup-retry-{suffix}-001",
        },
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["status"] == "queued"
    assert retried.json()["data"]["retry_count"] == 1

    for _ in range(20):
        await process_one()
        async for session in mysql_session():
            statuses = set(
                (
                    await session.scalars(
                        select(AiMemoryCleanupTask.task_status).where(
                            AiMemoryCleanupTask.task_no.in_(
                                (delete_task["cleanup_task_id"], disable_task["cleanup_task_id"])
                            )
                        )
                    )
                ).all()
            )
        if statuses == {"succeeded"}:
            break
    assert statuses == {"succeeded"}

    async for postgres in postgres_session():
        statuses = (
            await postgres.execute(
                text(
                    "SELECT memory_no,memory_status FROM memory.items "
                    "WHERE user_no=:user_no AND memory_no IN (:old_no,:new_no)"
                ),
                {"user_no": user_no, "old_no": memory_no, "new_no": revised_memory["memory_id"]},
            )
        ).all()
        assert dict(statuses)[memory_no] == "superseded"
        assert dict(statuses)[revised_memory["memory_id"]] == "deleted"
        ciphertext = await postgres.scalar(
            text("SELECT content_ciphertext FROM memory.items WHERE memory_no=:memory_no"),
            {"memory_no": revised_memory["memory_id"]},
        )
        assert ciphertext is None


def _identity(security: SecurityService, suffix: str, now: datetime) -> tuple[User, AuthSession]:
    user = User(
        user_no=new_prefixed_ulid("usr_"),
        username=f"privacy_{suffix}",
        username_normalized=f"privacy_{suffix}",
        nickname="Privacy User",
        user_status="active",
        locale="zh-CN",
        timezone="Asia/Shanghai",
        permission_version=1,
        registered_at=now,
    )
    auth_session = AuthSession(
        session_no=new_prefixed_ulid("ses_"),
        user_id=0,
        refresh_token_hash=security.keyed_hash("refresh-token", secrets.token_urlsafe()),
        token_family_no=new_prefixed_ulid("tfa_"),
        device_no=new_prefixed_ulid("dev_"),
        device_name="AI privacy integration",
        client_type="web",
        audience="user",
        csrf_token_hash=security.keyed_hash("csrf-token", secrets.token_urlsafe()),
        authenticated_at=now,
        authentication_methods=["password"],
        assurance_level="aal1",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        last_seen_at=now,
    )
    return user, auth_session
