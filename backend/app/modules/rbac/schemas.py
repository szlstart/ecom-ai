from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.api.schemas import StrictRequest
from app.modules.identity.schemas import ClientDescriptor, SessionBootstrap


class AdminLoginRequest(StrictRequest):
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    client: ClientDescriptor


class AdminMfaChallenge(StrictRequest):
    challenge_id: str
    allowed_methods: list[Literal["totp", "recovery_code"]]
    expires_at: datetime


class AdminMfaVerificationRequest(StrictRequest):
    challenge_id: str = Field(min_length=32, max_length=256)
    method: Literal["totp", "recovery_code"]
    code: str = Field(min_length=6, max_length=64)


class AdminReauthenticationRequest(StrictRequest):
    password: str = Field(min_length=1, max_length=128)
    method: Literal["totp", "recovery_code"]
    code: str = Field(min_length=6, max_length=64)


class AdminBootstrap(StrictRequest):
    session: SessionBootstrap
    permission_codes: list[str]
    scopes: list[dict[str, str | int]]


class ReauthenticationResult(StrictRequest):
    reauth_expires_at: datetime
    assurance_level: str


class AdminMe(StrictRequest):
    user_id: str
    username: str
    nickname: str
    assurance_level: str
    authenticated_at: datetime
    permission_version: int
    permission_codes: list[str]
    scopes: list[dict[str, str | int]]


class NavigationItem(StrictRequest):
    code: str
    title: str
    route: str
    required_permission: str


class AdminNavigation(StrictRequest):
    items: list[NavigationItem]
    scopes: list[dict[str, str | int]]


class AdminDashboardSummary(StrictRequest):
    generated_at: datetime
    scopes: list[dict[str, str | int]]
    active_user_count: int | None
    pending_approval_count: int
    unavailable_sections: list[str]


class AdminUserSummary(StrictRequest):
    user_id: str
    username: str
    nickname: str
    account_status: str
    registered_at: datetime
    last_login_at: datetime | None
    permission_version: int
    version: int


class AdminUserList(StrictRequest):
    items: list[AdminUserSummary]
    next_cursor: str | None


class UserStatusEventView(StrictRequest):
    status_event_id: str
    from_status: str
    to_status: str
    reason_code: str
    reason: str
    effective_at: datetime
    expires_at: datetime | None
    actor_type: str


class UserStatusChangeRequest(StrictRequest):
    action: Literal["suspend", "resume"]
    reason_code: str = Field(min_length=2, max_length=64)
    reason: str = Field(min_length=2, max_length=500)
    expires_at: datetime | None = None


class SessionRevocationRequest(StrictRequest):
    scope: Literal["all"] = "all"
    reason: str = Field(min_length=2, max_length=500)


class PasswordResetRequirementRequest(StrictRequest):
    reason: str = Field(min_length=2, max_length=500)


class RoleSummary(StrictRequest):
    role_id: str
    role_code: str
    role_name: str
    scope_type: str
    role_type: str
    description: str | None
    status: str
    version: int


class RoleCreateRequest(StrictRequest):
    role_code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    role_name: str = Field(min_length=2, max_length=64)
    scope_type: Literal["platform", "store"]
    description: str | None = Field(default=None, max_length=500)


class RoleUpdateRequest(StrictRequest):
    role_name: str | None = Field(default=None, min_length=2, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    status: Literal["active", "disabled"] | None = None


class RolePermissionsReplaceRequest(StrictRequest):
    permission_codes: list[str] = Field(max_length=200)
    reason: str = Field(min_length=2, max_length=500)


class RoleGrantView(StrictRequest):
    grant_id: str
    role_id: str
    role_name: str
    scope_type: str
    scope_id: int
    status: str
    granted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    reason: str
    version: int


class RoleGrantEventView(StrictRequest):
    event_id: str
    grant_id: str
    event_type: str
    actor_user_id: str | None
    reason: str
    grant_snapshot: dict[str, object]
    permission_version_after: int
    created_at: datetime


class RoleGrantCreateRequest(StrictRequest):
    role_id: str
    scope_type: Literal["platform", "store"]
    scope_id: int = Field(ge=0)
    expires_at: datetime | None = None
    reason: str = Field(min_length=2, max_length=500)


class ReasonRequest(StrictRequest):
    reason: str = Field(min_length=2, max_length=1000)


class SensitiveGrantCreateRequest(StrictRequest):
    fields: list[Literal["email", "phone"]] = Field(min_length=1, max_length=2)
    purpose_code: str = Field(min_length=2, max_length=64)
    reason: str = Field(min_length=5, max_length=1000)
    ttl_seconds: int = Field(default=300, ge=60, le=600)


class SensitiveGrantResult(StrictRequest):
    grant_id: str
    expires_at: datetime
    version: int


class SensitiveFields(StrictRequest):
    user_id: str
    values: dict[str, str]
    watermark: str


class ApprovalDecisionRequest(StrictRequest):
    decision: Literal["approve", "reject"]
    reason_code: str = Field(min_length=2, max_length=64)
    reason: str = Field(min_length=2, max_length=1000)


class ApprovalView(StrictRequest):
    approval_request_id: str
    approval_type: str
    action_code: str
    target_type: str
    target_id: str
    display_snapshot: dict[str, object]
    resource_versions: dict[str, object]
    required_approval_count: int
    approved_count: int
    status: str
    expires_at: datetime
    version: int


class AuditLogView(StrictRequest):
    operation_id: str
    operator_user_id: str
    permission_code: str
    action: str
    target_type: str
    target_id: str
    result_status: str
    reason: str | None
    created_at: datetime
