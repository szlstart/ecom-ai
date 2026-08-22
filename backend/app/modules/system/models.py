from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, BINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import AppendOnlyMySQLModel, MutableMySQLModel, MySQLBase


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


class AdminBatchJob(MutableMySQLModel, MySQLBase):
    __tablename__ = "admin_batch_jobs"
    __table_args__ = (
        UniqueConstraint("job_no", name="uk_admin_batch_jobs_no"),
        UniqueConstraint(
            "execution_backend", "execution_job_no", name="uk_admin_batch_jobs_execution"
        ),
        Index("idx_admin_batch_jobs_requester", "requested_by", "created_at", "id"),
        Index("idx_admin_batch_jobs_status", "job_status", "created_at", "id"),
        Index(
            "idx_admin_batch_jobs_scope",
            "scope_type",
            "scope_id",
            "job_type",
            "created_at",
            "id",
        ),
    )

    job_no: Mapped[str] = mapped_column(String(40), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_by: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    permission_code: Mapped[str] = mapped_column(String(128), nullable=False)
    input_file_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("file_objects.id")
    )
    request_config: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    request_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    job_status: Mapped[str] = mapped_column(String(16), nullable=False, default="created")
    execution_backend: Mapped[str | None] = mapped_column(String(32))
    execution_job_no: Mapped[str | None] = mapped_column(String(40))
    execution_status_version: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    total_count: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    success_count: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    failure_count: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    cancel_requested_by: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id")
    )
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
    result_file_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("file_objects.id")
    )
    error_file_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("file_objects.id")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_summary: Mapped[str | None] = mapped_column(String(1000))
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)


class AdminBatchJobItem(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "admin_batch_job_items"
    __table_args__ = (
        UniqueConstraint("job_id", "item_key", name="uk_admin_batch_job_items_key"),
        Index("idx_batch_job_items_status", "job_id", "item_status", "id"),
    )

    job_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("admin_batch_jobs.id"), nullable=False
    )
    item_key: Mapped[str] = mapped_column(String(128), nullable=False)
    item_status: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_no: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    input_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
