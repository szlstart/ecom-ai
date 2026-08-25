import os
import secrets
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.api.dependencies import AuthContext
from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, TokenClaims, utc_now
from app.database.mysql import mysql_session
from app.modules.identity.models import AuthSession, User
from app.modules.knowledge.admin_service import KnowledgeAdminService
from app.modules.knowledge.models import ToolDefinition, ToolVersion
from app.modules.knowledge.publication_service import AiPublicationService
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.models import AdminApprovalRequest, AdminOperationLog, Permission
from app.workers.admin_approval_worker import AdminApprovalWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_tool_rollback_is_approved_idempotent_and_keeps_versions_immutable(
    client: AsyncClient,
) -> None:
    _ = client
    suffix = secrets.token_hex(6)
    now = utc_now()
    settings = get_settings()
    security = SecurityService(settings)
    async for session in mysql_session():
        user = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"tool_rollback_{suffix}",
            username_normalized=f"tool_rollback_{suffix}",
            nickname="Tool rollback test",
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
            device_name="Tool rollback integration",
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
        session.add(auth_session)
        tool = ToolDefinition(
            tool_code=f"test.rollback.{suffix}",
            server_code="catalog-mcp",
            risk_level="read",
            tool_status="active",
        )
        session.add(tool)
        await session.flush()
        safe_input = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"query": {"type": "string"}},
        }
        old_output = {"type": "object", "properties": {"old": {"type": "boolean"}}}
        current_output = {
            "type": "object",
            "properties": {"current": {"type": "boolean"}},
        }
        target = ToolVersion(
            tool_id=tool.id,
            version_no=1,
            version_status="retired",
            input_schema=safe_input,
            output_schema=old_output,
            evaluation_report={"passed": True, "report_id": "rollback-baseline"},
            published_at=now - timedelta(days=1),
        )
        current = ToolVersion(
            tool_id=tool.id,
            version_no=2,
            version_status="published",
            input_schema=safe_input,
            output_schema=current_output,
            evaluation_report={"passed": True, "report_id": "current-baseline"},
            published_at=now,
        )
        session.add_all([target, current])
        await session.commit()
        permission = Permission(
            permission_code="ai_tools:publish",
            resource="ai_tools",
            action="publish",
            risk_level="critical",
            allowed_scope_types=["platform"],
            delegation_policy="role_policy",
            requires_mfa=True,
            requires_recent_auth=True,
            approval_policy="dual_control",
            owner="ai_governance",
            description="publish tool versions",
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
        request_key = f"tool-rollback-{suffix}"
        publication = AiPublicationService(session, settings, security)
        assert "ai.tool.rollback.v1" in AdminApprovalWorker(
            session, settings, security
        ).executor.handlers
        approval = await publication.request_tool_rollback(
            access, tool.tool_code, target.version_no, request_key
        )
        replay = await publication.request_tool_rollback(
            access, tool.tool_code, target.version_no, request_key
        )
        assert replay.approval_request_id == approval.approval_request_id
        approval_row = await session.scalar(
            select(AdminApprovalRequest).where(
                AdminApprovalRequest.approval_request_no == approval.approval_request_id
            )
        )
        assert approval_row is not None
        assert approval_row.action_code == "ai.tool.rollback.v1"
        assert approval_row.required_approval_count == 2

        result = await KnowledgeAdminService(session).rollback_tool(
            access, tool.tool_code, target.version_no
        )
        assert result.published_version == 1
        refreshed_versions = list(
            (
                await session.scalars(
                    select(ToolVersion)
                    .where(ToolVersion.tool_id == tool.id)
                    .order_by(ToolVersion.version_no)
                )
            ).all()
        )
        assert [(item.version_no, item.version_status) for item in refreshed_versions] == [
            (1, "published"),
            (2, "retired"),
        ]
        assert refreshed_versions[0].input_schema == safe_input
        assert refreshed_versions[0].output_schema == old_output
        assert refreshed_versions[1].output_schema == current_output
        audit = await session.scalar(
            select(AdminOperationLog)
            .where(
                AdminOperationLog.action == "tool.version.rollback",
                AdminOperationLog.target_no == tool.tool_code,
            )
            .order_by(AdminOperationLog.id.desc())
        )
        assert audit is not None
        assert audit.before_snapshot == {"published_version": 2}
        assert audit.after_snapshot == {"published_version": 1}
