from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY, INTEGER, VARBINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import MutableMySQLModel, MySQLBase


class FileUploadSession(MutableMySQLModel, MySQLBase):
    __tablename__ = "file_upload_sessions"
    __table_args__ = (
        UniqueConstraint("upload_no", name="uk_file_upload_sessions_no"),
        UniqueConstraint(
            "reserved_bucket", "reserved_object_key", name="uk_upload_reserved_object"
        ),
        UniqueConstraint(
            "provider", "active_provider_upload_id", name="uk_upload_active_provider_id"
        ),
        Index(
            "idx_upload_sessions_user_status",
            "uploader_user_id",
            "upload_status",
            "created_at",
            "id",
        ),
        Index("idx_upload_sessions_expiry", "upload_status", "expires_at", "id"),
    )

    upload_no: Mapped[str] = mapped_column(String(40), nullable=False)
    uploader_user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    target_owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_owner_no: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    reserved_bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    reserved_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    upload_type: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_upload_id: Mapped[str | None] = mapped_column(String(256))
    active_provider_upload_id: Mapped[str | None] = mapped_column(
        String(256),
        Computed(
            "CASE WHEN upload_status IN ('created','uploading','uploaded') "
            "THEN provider_upload_id ELSE NULL END"
        ),
    )
    expected_mime_types: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    max_size_bytes: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    expected_sha256: Mapped[bytes | None] = mapped_column(BINARY(32))
    part_manifest: Mapped[list[dict[str, object]] | None] = mapped_column(JSON)
    upload_status: Mapped[str] = mapped_column(String(16), nullable=False, default="created")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    aborted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class FileObject(MutableMySQLModel, MySQLBase):
    __tablename__ = "file_objects"
    __table_args__ = (
        UniqueConstraint("file_no", name="uk_file_objects_no"),
        UniqueConstraint("bucket", "object_key", name="uk_file_objects_storage_key"),
        UniqueConstraint(
            "parent_file_id", "variant", "processor_version", name="uk_file_derived_variant"
        ),
        Index("idx_file_objects_owner", "owner_type", "owner_no", "file_status", "id"),
        Index("idx_file_objects_lifecycle", "file_status", "expires_at", "id"),
        Index("idx_file_objects_hash", "sha256", "size_bytes"),
    )

    file_no: Mapped[str] = mapped_column(String(40), nullable=False)
    bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_no: Mapped[str] = mapped_column(String(64), nullable=False)
    upload_session_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("file_upload_sessions.id")
    )
    parent_file_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("file_objects.id")
    )
    variant: Mapped[str] = mapped_column(String(32), nullable=False, default="original")
    processor_version: Mapped[str | None] = mapped_column(String(32))
    original_filename_ciphertext: Mapped[bytes | None] = mapped_column(VARBINARY(1024))
    declared_mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    detected_mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    sha256: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    provider_checksum: Mapped[str | None] = mapped_column(String(128))
    width: Mapped[int | None] = mapped_column(INTEGER(unsigned=True))
    height: Mapped[int | None] = mapped_column(INTEGER(unsigned=True))
    duration_ms: Mapped[int | None] = mapped_column(INTEGER(unsigned=True))
    page_count: Mapped[int | None] = mapped_column(INTEGER(unsigned=True))
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="private")
    sensitivity_level: Mapped[str] = mapped_column(String(4), nullable=False)
    scan_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    file_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending_upload")
    storage_version_id: Mapped[str | None] = mapped_column(String(128))
    encryption_key_version: Mapped[int | None] = mapped_column(SmallInteger)
    reference_count: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default="0"
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
