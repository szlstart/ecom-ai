import os
import secrets
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.api.dependencies import AuthContext
from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, TokenClaims, utc_now
from app.database.mysql import mysql_session
from app.modules.agent_runtime.admin_service import AdminAgentRuntimeService
from app.modules.agent_runtime.models import AgentDefinition, AgentRun, AgentVersion
from app.modules.identity.models import AuthSession, User
from app.modules.messaging.models import Conversation, Message
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.models import AdminOperationLog, Permission
from app.modules.system.models import OutboxEvent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_admin_can_cancel_only_unexecuted_agent_run_with_audit_and_idempotency(
    client: AsyncClient,
) -> None:
    _ = client
    suffix = secrets.token_hex(6)
    now = utc_now()
    security = SecurityService(get_settings())
    async for session in mysql_session():
        user = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"agent_admin_{suffix}",
            username_normalized=f"agent_admin_{suffix}",
            nickname="Agent admin test",
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            permission_version=1,
            registered_at=now,
        )
        session.add(user)
        await session.flush()
        auth_session = AuthSession(
            session_no=new_prefixed_ulid("ses_"),
            user_id=user.id,
            refresh_token_hash=security.keyed_hash("refresh-token", secrets.token_urlsafe()),
            token_family_no=new_prefixed_ulid("tfa_"),
            device_no=new_prefixed_ulid("dev_"),
            device_name="Agent admin integration",
            client_type="web",
            audience="admin",
            csrf_token_hash=security.keyed_hash("csrf-token", secrets.token_urlsafe()),
            authenticated_at=now,
            authentication_methods=["password", "totp"],
            assurance_level="aal2",
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
        )
        definition = AgentDefinition(
            agent_no=new_prefixed_ulid("agt_"),
            agent_code=f"test_agent_{suffix}",
            agent_type="exclusive_support",
            scope_type="platform",
            display_name="Test agent",
            agent_status="active",
        )
        session.add_all([auth_session, definition])
        await session.flush()
        version = AgentVersion(
            agent_id=definition.id,
            version_no=1,
            version_status="published",
            system_prompt="test-only prompt that must not be returned",
            model_profile="deterministic-test",
            tool_allowlist=[],
            policy_config={},
            published_at=now,
        )
        conversation = Conversation(
            conversation_no=new_prefixed_ulid("cv_"),
            user_id=user.id,
            conversation_type="exclusive",
            is_fixed=True,
            conversation_status="active",
            last_sequence_no=1,
            last_message_at=now,
        )
        session.add_all([version, conversation])
        await session.flush()
        trigger = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=1,
            sender_type="user",
            sender_id=user.id,
            message_type="text",
            text_content="private prompt content",
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
            agent_version_id=version.id,
            run_status="queued",
            current_phase="queued",
            public_output="private generated content",
            trace_id=new_prefixed_ulid("trc_"),
            context_snapshot=[{"context_type": "order", "resource_id": "ord_private"}],
        )
        session.add(run)
        await session.commit()

        permission = Permission(
            permission_code="ai_runtime:kill",
            resource="ai_runtime",
            action="kill",
            risk_level="critical",
            allowed_scope_types=["platform"],
            delegation_policy="role_policy",
            requires_mfa=True,
            requires_recent_auth=True,
            approval_policy="none",
            owner="agent_runtime",
            description="cancel unexecuted agent runs",
            permission_status="active",
        )
        access = AdminAccess(
            context=AuthContext(
                user=user,
                session=auth_session,
                claims=TokenClaims(
                    subject=user.user_no,
                    session_id=auth_session.session_no,
                    audience="admin",
                    permission_version=1,
                    expires_at=auth_session.expires_at,
                ),
            ),
            permission=permission,
            scopes=(("platform", 0),),
        )
        service = AdminAgentRuntimeService(session)
        detail = await service.get(access, run.run_no)
        assert detail.status == "queued"
        assert detail.context_ref_count == 1
        assert detail.available_actions == ["cancel"]
        assert not hasattr(detail, "output")

        key = f"agent-run-cancel-{suffix}"
        expected_version = run.version
        cancelled = await service.cancel(
            access, run.run_no, "测试安全取消", expected_version, key
        )
        replay = await service.cancel(
            access, run.run_no, "测试安全取消", expected_version, key
        )
        assert replay == cancelled
        assert cancelled.status == "cancelled"
        assert cancelled.error_code == "ADMIN_CANCELLED"
        assert cancelled.available_actions == []
        event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "agent.run.cancelled.v1",
                OutboxEvent.aggregate_no == run.run_no,
            )
        )
        audit = await session.scalar(
            select(AdminOperationLog).where(
                AdminOperationLog.action == "agent.run.cancel",
                AdminOperationLog.target_no == run.run_no,
            )
        )
        assert event is not None
        assert event.payload["reason_code"] == "ADMIN_CANCELLED"
        assert audit is not None and audit.reason == "测试安全取消"

        trigger2 = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=2,
            sender_type="user",
            sender_id=user.id,
            message_type="text",
            text_content="already running",
            message_status="sent",
            moderation_status="passed",
            sent_at=now,
        )
        session.add(trigger2)
        await session.flush()
        running = AgentRun(
            run_no=new_prefixed_ulid("run_"),
            conversation_id=conversation.id,
            trigger_message_id=trigger2.id,
            agent_version_id=version.id,
            run_status="running",
            current_phase="tool_execution",
            trace_id=new_prefixed_ulid("trc_"),
            context_snapshot=[],
        )
        session.add(running)
        await session.commit()
        running_no = running.run_no
        with pytest.raises(ApplicationError) as conflict:
            await service.cancel(
                access,
                running_no,
                "不得强杀运行中事务",
                running.version,
                f"agent-running-cancel-{suffix}",
            )
        assert conflict.value.code == "AGENT_RUN_CANCEL_NOT_ALLOWED"
        await session.rollback()
        unchanged = await session.scalar(
            select(AgentRun).where(AgentRun.run_no == running_no)
        )
        assert unchanged is not None and unchanged.run_status == "running"
