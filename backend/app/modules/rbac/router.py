from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.identity.schemas import MessageResult
from app.modules.rbac.dependencies import (
    AdminAccess,
    require_admin_permission,
    require_any_admin_permission,
)
from app.modules.rbac.schemas import (
    AdminDashboardSummary,
    AdminUserList,
    AdminUserSummary,
    ApprovalDecisionRequest,
    ApprovalView,
    AuditLogView,
    PasswordResetRequirementRequest,
    ReasonRequest,
    RoleCreateRequest,
    RoleGrantCreateRequest,
    RoleGrantEventView,
    RoleGrantView,
    RolePermissionsReplaceRequest,
    RoleSummary,
    RoleUpdateRequest,
    SensitiveFields,
    SensitiveGrantCreateRequest,
    SensitiveGrantResult,
    SessionRevocationRequest,
    UserStatusChangeRequest,
    UserStatusEventView,
)
from app.modules.rbac.service_dependencies import RbacServiceDependency

router = APIRouter(prefix="/admin", tags=["administration"])


@router.get(
    "/dashboard",
    response_model=Envelope[AdminDashboardSummary],
    operation_id="AdminDashboard_Get",
)
async def get_dashboard(
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("dashboard:read")],
) -> Envelope[AdminDashboardSummary]:
    _no_store(response)
    return Envelope(data=await service.dashboard(access))


@router.get(
    "/users",
    response_model=Envelope[AdminUserList],
    operation_id="AdminUser_List",
)
async def list_users(
    response: Response,
    service: RbacServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:read")],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=40)] = None,
) -> Envelope[AdminUserList]:
    _no_store(response)
    return Envelope(data=await service.list_users(limit, cursor))


@router.get(
    "/users/{user_id}",
    response_model=Envelope[AdminUserSummary],
    operation_id="AdminUser_Get",
)
async def get_user(
    user_id: str,
    response: Response,
    service: RbacServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:read")],
) -> Envelope[AdminUserSummary]:
    _no_store(response)
    item = await service.get_user(user_id)
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.get(
    "/users/{user_id}/status-events",
    response_model=Envelope[list[UserStatusEventView]],
    operation_id="AdminUserStatusEvent_List",
)
async def list_user_status_events(
    user_id: str,
    response: Response,
    service: RbacServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:read")],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[list[UserStatusEventView]]:
    _no_store(response)
    return Envelope(data=await service.list_user_status_events(user_id, limit))


@router.post(
    "/users/{user_id}/status-changes",
    response_model=Envelope[AdminUserSummary],
    operation_id="AdminUser_ChangeStatus",
)
async def change_user_status(
    user_id: str,
    payload: UserStatusChangeRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("users:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminUserSummary]:
    item = await service.change_user_status(
        access,
        user_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.post(
    "/users/{user_id}/session-revocations",
    response_model=Envelope[MessageResult],
    operation_id="AdminUserSession_Revoke",
)
async def revoke_user_sessions(
    user_id: str,
    payload: SessionRevocationRequest,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[
        AdminAccess,
        require_admin_permission("users:sessions_revoke"),
    ],
) -> Envelope[MessageResult]:
    await service.revoke_user_sessions(
        access,
        user_id,
        payload.reason,
        idempotency_key,
    )
    return Envelope(data=MessageResult(message="目标账号的登录会话已撤销。"))


@router.post(
    "/users/{user_id}/password-reset-requirements",
    response_model=Envelope[MessageResult],
    operation_id="AdminUserPasswordReset_Require",
)
async def require_password_reset(
    user_id: str,
    payload: PasswordResetRequirementRequest,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[
        AdminAccess,
        require_admin_permission("users:force_password_reset"),
    ],
) -> Envelope[MessageResult]:
    await service.require_password_reset(
        access,
        user_id,
        payload.reason,
        idempotency_key,
    )
    return Envelope(data=MessageResult(message="目标账号下次登录前必须完成密码重置。"))


@router.get(
    "/roles",
    response_model=Envelope[list[RoleSummary]],
    operation_id="AdminRole_List",
)
async def list_roles(
    service: RbacServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("rbac:read")],
) -> Envelope[list[RoleSummary]]:
    return Envelope(data=await service.list_roles())


@router.post(
    "/roles",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[RoleSummary],
    operation_id="AdminRole_Create",
)
async def create_role(
    payload: RoleCreateRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("rbac:manage")],
) -> Envelope[RoleSummary]:
    item = await service.create_role(access, payload, idempotency_key)
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.get(
    "/roles/{role_id}",
    response_model=Envelope[RoleSummary],
    operation_id="AdminRole_Get",
)
async def get_role(
    role_id: str,
    response: Response,
    service: RbacServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("rbac:read")],
) -> Envelope[RoleSummary]:
    item = await service.get_role(role_id)
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.patch(
    "/roles/{role_id}",
    response_model=Envelope[RoleSummary],
    operation_id="AdminRole_Update",
)
async def update_role(
    role_id: str,
    payload: RoleUpdateRequest,
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("rbac:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[RoleSummary]:
    item = await service.update_role(access, role_id, payload, _expected_version(if_match))
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.put(
    "/roles/{role_id}/permissions",
    response_model=Envelope[RoleSummary],
    operation_id="AdminRolePermission_Replace",
)
async def replace_role_permissions(
    role_id: str,
    payload: RolePermissionsReplaceRequest,
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("rbac:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[RoleSummary]:
    item = await service.replace_role_permissions(
        access,
        role_id,
        payload,
        _expected_version(if_match),
    )
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.get(
    "/users/{user_id}/role-grants",
    response_model=Envelope[list[RoleGrantView]],
    operation_id="AdminRoleGrant_List",
)
async def list_role_grants(
    user_id: str,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("rbac:read")],
) -> Envelope[list[RoleGrantView]]:
    return Envelope(data=await service.list_role_grants(access, user_id))


@router.get(
    "/users/{user_id}/role-grant-events",
    response_model=Envelope[list[RoleGrantEventView]],
    operation_id="AdminRoleGrantEvent_List",
)
async def list_role_grant_events(
    user_id: str,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("rbac:read")],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[list[RoleGrantEventView]]:
    return Envelope(data=await service.list_role_grant_events(access, user_id, limit))


@router.post(
    "/users/{user_id}/role-grants",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[RoleGrantView],
    operation_id="AdminRoleGrant_Create",
)
async def create_role_grant(
    user_id: str,
    payload: RoleGrantCreateRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("rbac:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[RoleGrantView]:
    item = await service.create_role_grant(
        access,
        user_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.post(
    "/users/{user_id}/role-grants/{grant_id}/revocations",
    response_model=Envelope[MessageResult],
    operation_id="AdminRoleGrant_Revoke",
)
async def revoke_role_grant(
    user_id: str,
    grant_id: str,
    payload: ReasonRequest,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("rbac:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[MessageResult]:
    await service.revoke_role_grant(
        access,
        user_id,
        grant_id,
        payload.reason,
        _expected_version(if_match),
        idempotency_key,
    )
    return Envelope(data=MessageResult(message="角色授权已撤销。"))


@router.post(
    "/users/{user_id}/sensitive-field-access-grants",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[SensitiveGrantResult],
    operation_id="AdminSensitiveGrant_Create",
)
async def create_sensitive_grant(
    user_id: str,
    payload: SensitiveGrantCreateRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[
        AdminAccess,
        require_admin_permission("users:read_sensitive"),
    ],
) -> Envelope[SensitiveGrantResult]:
    _no_store(response)
    item = await service.create_sensitive_grant(
        access,
        user_id,
        payload,
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.post(
    "/sensitive-field-access-grants/{grant_id}/revocations",
    response_model=Envelope[MessageResult],
    operation_id="AdminSensitiveGrant_Revoke",
)
async def revoke_sensitive_grant(
    grant_id: str,
    payload: ReasonRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[
        AdminAccess,
        require_any_admin_permission("users:read_sensitive", "users:manage"),
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[MessageResult]:
    version = await service.revoke_sensitive_grant(
        access,
        grant_id,
        payload.reason,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(version)
    return Envelope(data=MessageResult(message="敏感字段访问凭据已撤销。"))


@router.get(
    "/users/{user_id}/sensitive-fields",
    response_model=Envelope[SensitiveFields],
    operation_id="AdminSensitiveFields_Get",
)
async def get_sensitive_fields(
    user_id: str,
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[
        AdminAccess,
        require_admin_permission("users:read_sensitive"),
    ],
    grant_id: Annotated[str, Header(alias="X-Sensitive-Access-Grant")],
) -> Envelope[SensitiveFields]:
    _no_store(response)
    return Envelope(data=await service.consume_sensitive_grant(access, user_id, grant_id))


@router.get(
    "/approval-requests",
    response_model=Envelope[list[ApprovalView]],
    operation_id="AdminApproval_List",
)
async def list_approvals(
    service: RbacServiceDependency,
    access: Annotated[
        AdminAccess,
        require_admin_permission("admin_approvals:read"),
    ],
) -> Envelope[list[ApprovalView]]:
    return Envelope(data=await service.list_approvals(access))


@router.get(
    "/approval-requests/{approval_request_id}",
    response_model=Envelope[ApprovalView],
    operation_id="AdminApproval_Get",
)
async def get_approval(
    approval_request_id: str,
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[
        AdminAccess,
        require_admin_permission("admin_approvals:read"),
    ],
) -> Envelope[ApprovalView]:
    item = await service.get_approval(access, approval_request_id)
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.post(
    "/approval-requests/{approval_request_id}/decisions",
    response_model=Envelope[ApprovalView],
    operation_id="AdminApproval_Decide",
)
async def decide_approval(
    approval_request_id: str,
    payload: ApprovalDecisionRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[
        AdminAccess,
        require_admin_permission("admin_approvals:decide"),
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[ApprovalView]:
    item = await service.decide_approval(
        access,
        approval_request_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.get(
    "/audit-logs",
    response_model=Envelope[list[AuditLogView]],
    operation_id="AdminAudit_List",
)
async def list_audit_logs(
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("audit:read")],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[list[AuditLogView]]:
    _no_store(response)
    return Envelope(data=await service.list_audit_logs(access, limit))
