from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY, MEDIUMBLOB, VARBINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import (
    AppendOnlyMySQLModel,
    MutableMySQLModel,
    MySQLBase,
    SoftDeleteMySQLModel,
)


class Role(SoftDeleteMySQLModel, MySQLBase):
    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("role_no", name="uk_roles_no"),
        UniqueConstraint("scope_type", "role_code", name="uk_roles_scope_code"),
    )

    role_no: Mapped[str] = mapped_column(String(40), nullable=False)
    role_code: Mapped[str] = mapped_column(String(64), nullable=False)
    role_name: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    role_type: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    role_status: Mapped[str] = mapped_column(String(16), nullable=False)


class Permission(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "permissions"

    permission_code: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    resource: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    allowed_scope_types: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    delegation_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    requires_mfa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requires_recent_auth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    owner: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    permission_status: Mapped[str] = mapped_column(String(16), nullable=False)


class UserRole(MutableMySQLModel, MySQLBase):
    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("grant_no", name="uk_user_roles_grant_no"),
        UniqueConstraint("active_grant_key", name="uk_user_roles_active_grant"),
        Index("idx_user_roles_scope", "scope_type", "scope_id", "role_id", "grant_status"),
        Index("idx_user_roles_expiry", "grant_status", "expires_at", "id"),
    )

    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    role_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("roles.id"), nullable=False
    )
    grant_no: Mapped[str] = mapped_column(String(40), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False, default=0)
    grant_status: Mapped[str] = mapped_column(String(16), nullable=False)
    active_grant_key: Mapped[bytes | None] = mapped_column(BINARY(32))
    granted_by: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    revoked_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    grant_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    revoke_reason: Mapped[str | None] = mapped_column(String(500))


class RolePermission(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uk_role_permissions_pair"),
    )

    role_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("roles.id"), nullable=False
    )
    permission_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("permissions.id"), nullable=False
    )
    condition_config: Mapped[dict[str, object] | None] = mapped_column(JSON)
    granted_by: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )


class AdminOperationLog(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "admin_operation_logs"
    __table_args__ = (
        Index("idx_admin_logs_operator_time", "operator_user_id", "created_at", "id"),
        Index("idx_admin_logs_target", "target_type", "target_no", "created_at"),
    )

    operation_no: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    operator_user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    permission_code: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_no: Mapped[str] = mapped_column(String(64), nullable=False)
    before_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSON)
    after_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSON)
    result_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_hash: Mapped[bytes | None] = mapped_column(BINARY(32))


class AdminMfaAuthenticator(MutableMySQLModel, MySQLBase):
    __tablename__ = "admin_mfa_authenticators"

    authenticator_no: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    authenticator_type: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_id_hash: Mapped[bytes | None] = mapped_column(BINARY(32), unique=True)
    public_key: Mapped[bytes | None] = mapped_column(VARBINARY(2048))
    sign_count: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    secret_ciphertext: Mapped[bytes | None] = mapped_column(VARBINARY(1024))
    recovery_codes_hashes: Mapped[list[dict[str, object]] | None] = mapped_column(JSON)
    key_version: Mapped[int | None] = mapped_column(SmallInteger)
    authenticator_status: Mapped[str] = mapped_column(String(16), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class AdminApprovalRequest(MutableMySQLModel, MySQLBase):
    __tablename__ = "admin_approval_requests"
    __table_args__ = (
        UniqueConstraint("approval_request_no", name="uk_admin_approval_request_no"),
        UniqueConstraint(
            "initiator_user_id",
            "action_code",
            "idempotency_key",
            name="uk_admin_approval_idempotency",
        ),
        Index("idx_admin_approvals_status_expiry", "request_status", "expires_at", "id"),
        Index("idx_admin_approvals_target", "target_type", "target_no", "created_at", "id"),
        Index(
            "idx_admin_approvals_scope",
            "scope_type",
            "scope_id",
            "request_status",
            "created_at",
            "id",
        ),
    )

    approval_request_no: Mapped[str] = mapped_column(String(40), nullable=False)
    approval_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action_code: Mapped[str] = mapped_column(String(64), nullable=False)
    initiator_user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    required_permission_code: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_no: Mapped[str] = mapped_column(String(64), nullable=False)
    command_schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    command_payload_ciphertext: Mapped[bytes] = mapped_column(MEDIUMBLOB, nullable=False)
    command_arguments_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    display_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    resource_versions: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    approval_policy_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    required_approval_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    approved_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    request_status: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_no: Mapped[str | None] = mapped_column(String(40))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    result_resource_type: Mapped[str | None] = mapped_column(String(64))
    result_resource_no: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class AdminApprovalDecision(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "admin_approval_decisions"
    __table_args__ = (
        UniqueConstraint(
            "approval_request_id", "approver_user_id", name="uk_admin_approval_decision_approver"
        ),
    )

    decision_no: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    approval_request_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("admin_approval_requests.id"), nullable=False
    )
    approver_user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    permission_code_snapshot: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    assurance_level: Mapped[str] = mapped_column(String(16), nullable=False)
    authenticated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    decision_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)


class AdminApprovalEvent(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "admin_approval_events"
    __table_args__ = (
        Index("idx_admin_approval_events_request", "approval_request_id", "created_at", "id"),
    )

    event_no: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    approval_request_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("admin_approval_requests.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    snapshot_redacted: Mapped[dict[str, object] | None] = mapped_column(JSON)
    request_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)


class UserRoleEvent(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "user_role_events"
    __table_args__ = (Index("idx_user_role_events_grant", "grant_id", "created_at", "id"),)

    event_no: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    grant_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("user_roles.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    grant_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    permission_version_after: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)


class AdminSensitiveAccessGrant(MutableMySQLModel, MySQLBase):
    __tablename__ = "admin_sensitive_access_grants"
    __table_args__ = (
        Index(
            "idx_sensitive_grants_admin_session",
            "admin_user_id",
            "auth_session_id",
            "grant_status",
            "expires_at",
        ),
        Index(
            "idx_sensitive_grants_target", "target_type", "target_no", "grant_status", "expires_at"
        ),
    )

    grant_no: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    admin_user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    auth_session_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("auth_sessions.id"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_no: Mapped[str] = mapped_column(String(64), nullable=False)
    field_set: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    purpose_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    grant_status: Mapped[str] = mapped_column(String(16), nullable=False)
    assurance_level: Mapped[str] = mapped_column(String(16), nullable=False)
    authenticated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    revoked_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    revoke_reason: Mapped[str | None] = mapped_column(String(500))
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
