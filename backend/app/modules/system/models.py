from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, BINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import AppendOnlyMySQLModel, MySQLBase


class IdempotencyRecord(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("scope_key", "idempotency_key", name="uk_idempotency_scope_key"),
    )

    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    response_status: Mapped[int | None] = mapped_column()
    response_body: Mapped[dict[str, object] | None] = mapped_column(JSON)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_no: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class OutboxEvent(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("event_no", name="uk_outbox_events_no"),
        Index("idx_outbox_events_delivery", "event_status", "available_at", "id"),
        Index("idx_outbox_events_aggregate", "aggregate_type", "aggregate_no", "id"),
    )

    event_no: Mapped[str] = mapped_column(String(40), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_no: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    event_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
