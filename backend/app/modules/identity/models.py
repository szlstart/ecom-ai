from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY, VARBINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import (
    AppendOnlyMySQLModel,
    MutableMySQLModel,
    MySQLBase,
    SoftDeleteMySQLModel,
)


class User(SoftDeleteMySQLModel, MySQLBase):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("user_no", name="uk_users_user_no"),
        UniqueConstraint("username_normalized", name="uk_users_username_normalized"),
        Index("idx_users_status_created_at", "user_status", "created_at", "id"),
        Index("idx_users_status_expiry", "user_status", "status_expires_at", "id"),
    )

    user_no: Mapped[str] = mapped_column(String(40), nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    username_normalized: Mapped[str] = mapped_column(String(64), nullable=False)
    nickname: Mapped[str] = mapped_column(String(64), nullable=False)
    avatar_object_key: Mapped[str | None] = mapped_column(String(512))
    user_status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    status_reason_code: Mapped[str | None] = mapped_column(String(64))
    status_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    current_status_record_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("user_status_records.id", use_alter=True)
    )
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="zh-CN")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    permission_version: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=1, server_default="1"
    )
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class UserCredential(MutableMySQLModel, MySQLBase):
    __tablename__ = "user_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "credential_type", name="uk_user_credentials_user_type"),
        UniqueConstraint(
            "credential_type", "active_identifier_hash", name="uk_credentials_active_identifier"
        ),
        Index("idx_credentials_identifier", "credential_type", "identifier_hash"),
    )

    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    credential_type: Mapped[str] = mapped_column(String(32), nullable=False)
    identifier_ciphertext: Mapped[bytes | None] = mapped_column(VARBINARY(512))
    identifier_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    active_identifier_hash: Mapped[bytes | None] = mapped_column(
        BINARY(32),
        Computed("CASE WHEN credential_status = 'active' THEN identifier_hash ELSE NULL END"),
    )
    secret_hash: Mapped[bytes | None] = mapped_column(VARBINARY(512))
    algorithm: Mapped[str | None] = mapped_column(String(32))
    key_version: Mapped[int | None] = mapped_column(SmallInteger)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    credential_version: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=1, server_default="1"
    )
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    credential_status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class AuthSession(MutableMySQLModel, MySQLBase):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("session_no", name="uk_auth_sessions_session_no"),
        UniqueConstraint("refresh_token_hash", name="uk_auth_sessions_refresh_token_hash"),
        Index("idx_auth_sessions_user_expires", "user_id", "expires_at"),
        Index("idx_auth_sessions_family", "token_family_no"),
    )

    session_no: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    refresh_token_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    token_family_no: Mapped[str] = mapped_column(String(40), nullable=False)
    parent_session_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("auth_sessions.id")
    )
    device_no: Mapped[str | None] = mapped_column(String(128))
    device_name: Mapped[str | None] = mapped_column(String(128))
    client_type: Mapped[str] = mapped_column(String(32), nullable=False)
    audience: Mapped[str] = mapped_column(String(16), nullable=False)
    csrf_token_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    ip_ciphertext: Mapped[bytes | None] = mapped_column(VARBINARY(256))
    user_agent_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    authenticated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    authentication_methods: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    assurance_level: Mapped[str] = mapped_column(String(16), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    revoke_reason: Mapped[str | None] = mapped_column(String(64))


class VerificationCode(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "verification_codes"
    __table_args__ = (
        UniqueConstraint("verification_no", name="uk_verification_codes_no"),
        Index("idx_verification_target_purpose", "target_hash", "purpose", "created_at"),
        Index("idx_verification_expires", "expires_at"),
    )

    verification_no: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    code_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=5)
    send_channel: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery_status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_reference_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    request_ip_hash: Mapped[bytes | None] = mapped_column(BINARY(32))


class PasswordResetRecord(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "password_reset_records"
    __table_args__ = (
        UniqueConstraint("reset_no", name="uk_password_reset_no"),
        UniqueConstraint("reset_token_hash", name="uk_password_reset_token_hash"),
    )

    reset_no: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    verification_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("verification_codes.id"), nullable=False
    )
    reset_token_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    credential_version_before: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    request_ip_hash: Mapped[bytes | None] = mapped_column(BINARY(32))


class UserAddress(SoftDeleteMySQLModel, MySQLBase):
    __tablename__ = "user_addresses"
    __table_args__ = (
        UniqueConstraint("address_no", name="uk_user_addresses_no"),
        UniqueConstraint("active_default_user_id", name="uk_user_addresses_active_default"),
        Index("idx_user_addresses_user", "user_id", "deleted_at", "id"),
    )

    address_no: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    recipient_name_ciphertext: Mapped[bytes] = mapped_column(VARBINARY(512), nullable=False)
    phone_ciphertext: Mapped[bytes] = mapped_column(VARBINARY(512), nullable=False)
    phone_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, default="CN")
    province_code: Mapped[str] = mapped_column(String(32), nullable=False)
    city_code: Mapped[str] = mapped_column(String(32), nullable=False)
    district_code: Mapped[str] = mapped_column(String(32), nullable=False)
    address_ciphertext: Mapped[bytes] = mapped_column(VARBINARY(2048), nullable=False)
    postal_code: Mapped[str | None] = mapped_column(String(16))
    label: Mapped[str | None] = mapped_column(String(32))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active_default_user_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        Computed("CASE WHEN is_default = 1 AND deleted_at IS NULL THEN user_id ELSE NULL END"),
    )
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)


class CredentialChangeRecord(MutableMySQLModel, MySQLBase):
    __tablename__ = "credential_change_records"
    __table_args__ = (
        UniqueConstraint("change_no", name="uk_credential_change_no"),
        Index("idx_credential_changes_user_status", "user_id", "change_status", "expires_at"),
    )

    change_no: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    credential_type: Mapped[str] = mapped_column(String(16), nullable=False)
    old_credential_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("user_credentials.id")
    )
    credential_version_before: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    new_identifier_ciphertext: Mapped[bytes | None] = mapped_column(VARBINARY(512))
    new_identifier_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    old_verification_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("verification_codes.id")
    )
    new_verification_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("verification_codes.id")
    )
    change_status: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    request_ip_hash: Mapped[bytes | None] = mapped_column(BINARY(32))


class UserStatusRecord(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "user_status_records"
    __table_args__ = (
        UniqueConstraint("status_record_no", name="uk_user_status_record_no"),
        UniqueConstraint("idempotency_scope_key", name="uk_user_status_idempotency"),
        Index("idx_user_status_records_user", "user_id", "created_at", "id"),
        Index("idx_user_status_records_expiry", "to_status", "expires_at", "id"),
    )

    status_record_no: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    scope_type: Mapped[str | None] = mapped_column(String(16))
    scope_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    expected_user_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    result_user_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_scope_key: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)


class UserAgreementAcceptance(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "user_agreement_acceptances"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "document_type",
            "content_version_id",
            "acceptance_context",
            name="uk_agreement_acceptance_fact",
        ),
        Index("idx_agreement_acceptances_user_time", "user_id", "accepted_at", "id"),
        Index("idx_agreement_acceptances_version", "content_version_id", "accepted_at", "id"),
    )

    acceptance_no: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content_entry_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("platform_content_entries.id"), nullable=False
    )
    content_version_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("platform_content_versions.id"), nullable=False
    )
    document_version: Mapped[str] = mapped_column(String(40), nullable=False)
    content_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    acceptance_context: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False)
    region_code: Mapped[str] = mapped_column(String(16), nullable=False)
    ip_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    user_agent_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)


class AuthAttempt(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "auth_attempts"
    __table_args__ = (
        UniqueConstraint("attempt_no", name="uk_auth_attempt_no"),
        Index(
            "idx_auth_attempts_identifier_time", "identifier_monitoring_hash", "occurred_at", "id"
        ),
        Index("idx_auth_attempts_ip_time", "ip_hash", "occurred_at", "id"),
        Index("idx_auth_attempts_user_time", "user_id", "occurred_at", "id"),
        Index("idx_auth_attempts_result_time", "result", "occurred_at", "id"),
    )

    attempt_no: Mapped[str] = mapped_column(String(40), nullable=False)
    attempt_type: Mapped[str] = mapped_column(String(24), nullable=False)
    auth_method: Mapped[str] = mapped_column(String(24), nullable=False)
    user_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    identifier_monitoring_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    session_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("auth_sessions.id")
    )
    result: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    risk_policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    challenge_type: Mapped[str | None] = mapped_column(String(32))
    ip_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    device_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    user_agent_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
