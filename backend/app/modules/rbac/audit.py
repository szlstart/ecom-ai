from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.id_generator import new_prefixed_ulid
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.models import AdminOperationLog


def record_admin_operation(
    session: AsyncSession,
    access: AdminAccess,
    *,
    action: str,
    target_type: str,
    target_no: str,
    reason: str | None = None,
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
    scope_type: str | None = None,
    scope_id: int | None = None,
) -> None:
    resolved_scope_type, resolved_scope_id = _audit_scope(access, scope_type, scope_id)
    request_id = request_id_context.get() or new_prefixed_ulid("req_")
    session.add(
        AdminOperationLog(
            operation_no=new_prefixed_ulid("aol_"),
            operator_user_id=access.context.user.id,
            scope_type=resolved_scope_type,
            scope_id=resolved_scope_id,
            permission_code=access.permission.permission_code,
            action=action,
            target_type=target_type,
            target_no=target_no,
            before_snapshot=before,
            after_snapshot=after,
            result_status="succeeded",
            reason=reason,
            request_id=request_id,
            trace_id=request_id,
            ip_hash=None,
        )
    )


def _audit_scope(
    access: AdminAccess, scope_type: str | None, scope_id: int | None
) -> tuple[str, int]:
    if scope_type is not None and scope_id is not None:
        return scope_type, scope_id
    if ("platform", 0) in access.scopes:
        return "platform", 0
    if access.scopes:
        return access.scopes[0]
    return "platform", 0
