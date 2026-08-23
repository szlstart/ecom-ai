from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, BINARY, INTEGER
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import AppendOnlyMySQLModel, MutableMySQLModel, MySQLBase


class CheckoutSession(MutableMySQLModel, MySQLBase):
    __tablename__ = "checkout_sessions"
    __table_args__ = (
        UniqueConstraint("checkout_no", name="uk_checkout_sessions_no"),
        CheckConstraint("source_type IN ('buy_now', 'cart')", name="checkout_source_type"),
        CheckConstraint(
            "checkout_status IN ('active', 'submitted', 'expired', 'cancelled')",
            name="checkout_status",
        ),
        Index("idx_checkout_user_status", "user_id", "checkout_status", "created_at", "id"),
        Index("idx_checkout_expiry", "checkout_status", "expires_at", "id"),
    )

    checkout_no: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    checkout_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    selected_address_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("user_addresses.id")
    )
    goods_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    freight_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    payable_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    pricing_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pricing_v1", server_default="pricing_v1"
    )
    snapshot_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class CheckoutSnapshot(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "checkout_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "checkout_session_id", "snapshot_version", name="uk_checkout_snapshot_version"
        ),
        UniqueConstraint("checkout_session_id", "snapshot_hash", name="uk_checkout_snapshot_hash"),
        Index("idx_checkout_snapshot_session", "checkout_session_id", "created_at", "id"),
    )

    checkout_session_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("checkout_sessions.id"), nullable=False
    )
    snapshot_version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=1, server_default="1"
    )
    snapshot_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    snapshot_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    invalid_reason: Mapped[str | None] = mapped_column(String(64))
