from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BINARY,
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, INTEGER, VARBINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import AppendOnlyMySQLModel, MutableMySQLModel, MySQLBase


class RefundApplication(MutableMySQLModel, MySQLBase):
    __tablename__ = "refund_applications"
    __table_args__ = (
        UniqueConstraint("refund_no", name="uk_refund_applications_no"),
        CheckConstraint(
            "refund_status IN ('submitted','merchant_review','approved','waiting_return',"
            "'returning','received','refunding','succeeded','rejected','cancelled','closed')",
            name="refund_application_status",
        ),
        CheckConstraint(
            "refund_type IN ('refund_only','return_and_refund')",
            name="refund_application_type",
        ),
        Index("idx_refund_applications_user_time", "user_id", "created_at", "id"),
        Index("idx_refund_applications_order_status", "order_id", "refund_status", "id"),
    )

    refund_no: Mapped[str] = mapped_column(String(40), nullable=False)
    order_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("orders.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    refund_type: Mapped[str] = mapped_column(String(24), nullable=False)
    refund_status: Mapped[str] = mapped_column(String(24), nullable=False, default="submitted")
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_detail: Mapped[str | None] = mapped_column(String(500))
    requested_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    approved_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    contact_phone_last4: Mapped[str | None] = mapped_column(String(4))
    eligibility_token_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    policy_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    decided_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    claimed_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class RefundItem(MutableMySQLModel, MySQLBase):
    __tablename__ = "refund_items"
    __table_args__ = (
        UniqueConstraint("refund_id", "order_item_id", name="uk_refund_items_refund_item"),
        Index("idx_refund_items_order_item_status", "order_item_id", "refund_status", "id"),
    )

    refund_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("refund_applications.id"), nullable=False
    )
    order_item_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("order_items.id"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    requested_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    succeeded_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False, default=0)
    refund_status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")


class RefundEvent(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "refund_events"
    __table_args__ = (
        UniqueConstraint("event_no", name="uk_refund_events_no"),
        Index("idx_refund_events_refund_time", "refund_id", "created_at", "id"),
    )

    event_no: Mapped[str] = mapped_column(String(40), nullable=False)
    refund_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("refund_applications.id"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    event_code: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(String(500))
    request_id: Mapped[str | None] = mapped_column(String(64))


class RefundAppeal(MutableMySQLModel, MySQLBase):
    __tablename__ = "refund_appeals"
    __table_args__ = (
        UniqueConstraint("appeal_no", name="uk_refund_appeals_no"),
        UniqueConstraint("refund_id", name="uk_refund_appeals_refund"),
    )

    appeal_no: Mapped[str] = mapped_column(String(40), nullable=False)
    refund_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("refund_applications.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    appeal_status: Mapped[str] = mapped_column(String(16), nullable=False, default="submitted")
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    claimed_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    reviewed_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False, default="USER_APPEAL")
    resolution_code: Mapped[str | None] = mapped_column(String(64))
    resolution_detail: Mapped[str | None] = mapped_column(String(2000))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class RefundAppealEvent(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "refund_appeal_events"
    __table_args__ = (
        UniqueConstraint("event_no", name="uk_refund_appeal_events_no"),
        Index("idx_refund_appeal_events_appeal_time", "appeal_id", "created_at", "id"),
    )

    event_no: Mapped[str] = mapped_column(String(40), nullable=False)
    appeal_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("refund_appeals.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    remark: Mapped[str | None] = mapped_column(String(1000))
    appeal_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))


class RefundPaymentRecord(MutableMySQLModel, MySQLBase):
    __tablename__ = "refund_payment_records"
    __table_args__ = (
        UniqueConstraint("refund_payment_no", name="uk_refund_payment_records_no"),
        UniqueConstraint("refund_id", name="uk_refund_payment_records_refund"),
        Index("idx_refund_payment_records_status", "payment_status", "updated_at", "id"),
    )

    refund_payment_no: Mapped[str] = mapped_column(String(40), nullable=False)
    refund_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("refund_applications.id"), nullable=False
    )
    payment_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("payments.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_refund_no: Mapped[str | None] = mapped_column(String(128))
    amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payment_status: Mapped[str] = mapped_column(String(16), nullable=False, default="created")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class RefundPaymentEvent(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "refund_payment_events"
    __table_args__ = (
        UniqueConstraint("event_no", name="uk_refund_payment_events_no"),
        UniqueConstraint(
            "refund_payment_id", "provider_event_id", name="uk_refund_payment_events_provider"
        ),
        Index("idx_refund_payment_events_record_time", "refund_payment_id", "created_at", "id"),
    )

    event_no: Mapped[str] = mapped_column(String(40), nullable=False)
    refund_payment_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("refund_payment_records.id"), nullable=False
    )
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_status: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    signature_valid: Mapped[bool] = mapped_column(nullable=False)


class RefundShipment(MutableMySQLModel, MySQLBase):
    __tablename__ = "refund_shipments"
    __table_args__ = (UniqueConstraint("refund_id", name="uk_refund_shipments_refund"),)

    refund_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("refund_applications.id"), nullable=False
    )
    carrier_code: Mapped[str] = mapped_column(String(32), nullable=False)
    carrier_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tracking_no_ciphertext: Mapped[bytes] = mapped_column(VARBINARY(512), nullable=False)
    tracking_no_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    tracking_no_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    shipment_status: Mapped[str] = mapped_column(String(32), nullable=False, default="submitted")
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    key_version: Mapped[int] = mapped_column(nullable=False, default=1)
