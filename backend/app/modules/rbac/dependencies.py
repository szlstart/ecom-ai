from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends

from app.api.dependencies import AdminContext, AuthContext, DatabaseSession
from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.modules.rbac.models import Permission
from app.modules.rbac.repository import RbacRepository


@dataclass(frozen=True)
class AdminAccess:
    context: AuthContext
    permission: Permission
    scopes: tuple[tuple[str, int], ...]

    def require_scope(self, scope_type: str, scope_id: int) -> None:
        if ("platform", 0) in self.scopes:
            return
        if (scope_type, scope_id) not in self.scopes:
            raise ApplicationError(
                status=404,
                code="RESOURCE_NOT_FOUND",
                title="Resource not found",
                detail="未找到该资源。",
            )


def require_admin_permission(permission_code: str) -> object:
    return require_any_admin_permission(permission_code)


def require_any_admin_permission(*permission_codes: str) -> object:
    if not permission_codes:
        raise ValueError("At least one permission code is required")

    async def dependency(
        context: AdminContext,
        session: DatabaseSession,
    ) -> AdminAccess:
        rows = await RbacRepository(session).permissions_for_user(context.user.id, utc_now())
        selected_code = next(
            (
                code
                for code in permission_codes
                if any(row[0].permission_code == code for row in rows)
            ),
            None,
        )
        matching = [row for row in rows if row[0].permission_code == selected_code]
        if not matching:
            raise ApplicationError(
                status=403,
                code="AUTH_PERMISSION_DENIED",
                title="Permission denied",
                detail=f"当前管理身份缺少所需权限: {'、'.join(permission_codes)}。",
            )
        permission = matching[0][0]
        if permission.requires_mfa and context.session.assurance_level not in {"aal2", "aal3"}:
            raise ApplicationError(
                status=403,
                code="AUTH_MFA_REQUIRED",
                title="MFA required",
                detail="该操作需要多因素认证。",
            )
        settings = get_settings()
        if (
            permission.requires_recent_auth
            and context.session.authenticated_at
            < utc_now() - timedelta(seconds=settings.admin_recent_auth_seconds)
        ):
            raise ApplicationError(
                status=428,
                code="AUTH_RECENT_AUTH_REQUIRED",
                title="Recent authentication required",
                detail="该操作需要重新进行安全验证。",
            )
        scopes = tuple(sorted({(grant.scope_type, grant.scope_id) for _, grant, _ in matching}))
        return AdminAccess(context=context, permission=permission, scopes=scopes)

    return Depends(dependency)
