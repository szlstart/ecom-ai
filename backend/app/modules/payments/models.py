from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, BINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import AppendOnlyMySQLModel, MutableMySQLModel, MySQLBase


class Payment(MutableMySQLModel, MySQLBase):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("payment_no", name="uk_payments_no"),
        UniqueConstraint("provider", "provider_trade_no", name="uk_payments_provider_trade"),
        CheckConstraint(
            "payment_status IN ('created', 'pending', 'succeeded', 'failed', 'closed', "
            "'partially_refunded', 'refunded')",
            name="payment_status",
        ),
        CheckConstraint(
            "refunded_amount <= paid_amount AND paid_amount <= requested_amount",
            name="payment_amounts",
        ),
        Index("idx_payments_trade_status", "trade_order_id", "payment_status", "created_at"),
        Index("idx_payments_status_expiry", "payment_status", "expires_at"),
    )

    payment_no: Mapped[str] = mapped_column(String(32), nullable=False)
    trade_order_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("trade_orders.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_trade_no: Mapped[str | None] = mapped_column(String(128))
    payment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    paid_amount: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    refunded_amount: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    client_ip_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(String(500))


class PaymentEvent(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint("event_no", name="uk_payment_events_no"),
        Index("idx_payment_events_payment", "payment_id", "created_at", "id"),
    )

    event_no: Mapped[str] = mapped_column(String(40), nullable=False)
    payment_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("payments.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_no: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    trace_id: Mapped[str | None] = mapped_column(String(64))
