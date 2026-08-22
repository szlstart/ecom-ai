from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import MutableMySQLModel, MySQLBase


class PlatformContentEntry(MutableMySQLModel, MySQLBase):
    __tablename__ = "platform_content_entries"
    __table_args__ = (UniqueConstraint("content_key", name="uk_content_entries_key"),)

    content_no: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    content_key: Mapped[str] = mapped_column(String(128), nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class PlatformContentVersion(MutableMySQLModel, MySQLBase):
    __tablename__ = "platform_content_versions"
    __table_args__ = (
        UniqueConstraint("entry_id", "document_version", name="uk_content_version_entry_version"),
        Index(
            "idx_content_version_published",
            "entry_id",
            "locale",
            "region_code",
            "publish_status",
            "effective_at",
        ),
    )

    content_version_no: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    entry_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("platform_content_entries.id"), nullable=False
    )
    document_version: Mapped[str] = mapped_column(String(40), nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False)
    region_code: Mapped[str] = mapped_column(String(16), nullable=False)
    safe_content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    metadata_json: Mapped[dict[str, object] | None] = mapped_column(JSON)
    publish_status: Mapped[str] = mapped_column(String(16), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
