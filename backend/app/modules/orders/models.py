from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY, INTEGER, VARBINARY
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.database.base import AppendOnlyMySQLModel, MutableMySQLModel, MySQLBase


class TradeOrder(MutableMySQLModel, MySQLBase):
    __tablename__ = "trade_orders"
    __table_args__ = (
        UniqueConstraint("trade_no", name="uk_trade_orders_no"),
        UniqueConstraint("checkout_session_id", name="uk_trade_orders_checkout"),
        UniqueConstraint("checkout_no_snapshot", name="uk_trade_orders_checkout_snapshot"),
        CheckConstraint("order_source IN ('buy_now', 'cart')", name="trade_order_source"),
        CheckConstraint(
            "trade_status IN ('pending_payment', 'paid', 'closed', "
            "'partially_refunded', 'refunded')",
            name="trade_order_status",
        ),
        CheckConstraint(
            "payable_amount = goods_amount + freight_amount + adjustment_amount "
            "AND payable_amount >= 0",
            name="trade_order_amounts",
        ),
        CheckConstraint(
            "refunded_amount <= paid_amount AND paid_amount <= payable_amount",
            name="trade_order_paid_refunded",
        ),
        Index("idx_trade_orders_user_time", "user_id", "created_at", "id"),
        Index("idx_trade_orders_status_expiry", "trade_status", "expires_at", "id"),
    )

    trade_no: Mapped[str] = mapped_column(String(32), nullable=False)
    checkout_session_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("checkout_sessions.id", ondelete="SET NULL")
    )
    checkout_no_snapshot: Mapped[str] = mapped_column(String(40), nullable=False)
    checkout_snapshot_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    order_source: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_status: Mapped[str] = mapped_column(String(32), nullable=False)
    goods_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    freight_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    payable_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    adjustment_amount: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    paid_amount: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    refunded_amount: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # Immutable checkout-time snapshot. Cancelled unpaid orders are replaced by
    # user-only navigation snapshots, so this must never be interpreted as the
    # number of currently materialized rows in `orders`.
    original_order_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    order_count = synonym("original_order_count")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class Order(MutableMySQLModel, MySQLBase):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("order_no", name="uk_orders_no"),
        CheckConstraint(
            "order_status IN ('pending_payment', 'paid', 'pending_shipment', 'shipped', "
            "'completed', 'cancelled', 'closed')",
            name="order_status",
        ),
        CheckConstraint(
            "payment_status IN ('unpaid', 'processing', 'paid', 'partially_refunded', 'refunded')",
            name="order_payment_status",
        ),
        CheckConstraint(
            "fulfillment_status IN ('unfulfilled', 'partial', 'shipped', 'received')",
            name="order_fulfillment_status",
        ),
        CheckConstraint(
            "after_sale_status IN ('none', 'in_progress', 'partial', 'completed')",
            name="order_after_sale_status",
        ),
        CheckConstraint(
            "payable_amount = goods_amount + freight_amount + adjustment_amount "
            "AND payable_amount >= 0",
            name="order_amounts",
        ),
        CheckConstraint(
            "refunded_amount <= paid_amount AND paid_amount <= payable_amount",
            name="order_paid_refunded",
        ),
        Index("idx_orders_user_status_time", "user_id", "order_status", "created_at", "id"),
        Index("idx_orders_store_status_time", "store_id", "order_status", "created_at", "id"),
        Index("idx_orders_trade", "trade_order_id", "id"),
    )

    order_no: Mapped[str] = mapped_column(String(32), nullable=False)
    trade_order_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("trade_orders.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    order_status: Mapped[str] = mapped_column(String(32), nullable=False)
    payment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    fulfillment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    after_sale_status: Mapped[str] = mapped_column(String(32), nullable=False)
    goods_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    freight_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    payable_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    adjustment_amount: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    paid_amount: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    refunded_amount: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    adjustment_reason_code: Mapped[str | None] = mapped_column(String(64))
    adjustment_reason: Mapped[str | None] = mapped_column(String(500))
    adjusted_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    adjusted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    buyer_remark: Mapped[str | None] = mapped_column(String(500))
    policy_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    user_hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    undo_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class CancelledOrderRecord(MutableMySQLModel, MySQLBase):
    """User-only navigation snapshot left after an unpaid order is cancelled."""

    __tablename__ = "cancelled_order_records"
    __table_args__ = (
        UniqueConstraint("order_no", name="uk_cancelled_order_records_no"),
        Index("idx_cancelled_order_records_user_time", "user_id", "created_at", "id"),
    )

    order_no: Mapped[str] = mapped_column(String(32), nullable=False)
    trade_no: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    cancellation_reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))
    cancelled_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    search_text: Mapped[str] = mapped_column(String(2000), nullable=False)
    snapshot_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)


class OrderItem(MutableMySQLModel, MySQLBase):
    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint("order_item_no", name="uk_order_items_no"),
        CheckConstraint("quantity > 0", name="order_item_quantity"),
        CheckConstraint("gross_amount = unit_price_amount * quantity", name="order_item_gross"),
        CheckConstraint(
            "payable_amount = gross_amount + adjustment_amount AND payable_amount >= 0",
            name="order_item_payable",
        ),
        CheckConstraint(
            "refunded_quantity <= quantity AND refunded_amount <= payable_amount",
            name="order_item_refunded",
        ),
        CheckConstraint(
            "review_status IN ('pending', 'reviewed', 'closed')", name="order_item_review"
        ),
        CheckConstraint(
            "after_sale_status IN ('none', 'in_progress', 'completed')",
            name="order_item_after_sale",
        ),
        Index("idx_order_items_order", "order_id", "id"),
        Index("idx_order_items_sku", "sku_id", "created_at", "id"),
        Index("idx_order_items_product", "product_id", "id"),
    )

    order_item_no: Mapped[str] = mapped_column(String(40), nullable=False)
    order_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("orders.id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("products.id"), nullable=False
    )
    sku_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("product_skus.id"), nullable=False
    )
    product_no: Mapped[str] = mapped_column(String(40), nullable=False)
    sku_no: Mapped[str] = mapped_column(String(40), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sku_name: Mapped[str] = mapped_column(String(255), nullable=False)
    spec_snapshot: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False)
    image_object_key: Mapped[str | None] = mapped_column(String(512))
    quantity: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    unit_price_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    market_price_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    gross_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    payable_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    adjustment_amount: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    refunded_quantity: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default="0"
    )
    refunded_amount: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False)
    after_sale_status: Mapped[str] = mapped_column(String(16), nullable=False)


class OrderAddress(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "order_addresses"
    __table_args__ = (UniqueConstraint("order_id", name="uk_order_addresses_order"),)

    order_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("orders.id"), nullable=False
    )
    source_address_no: Mapped[str | None] = mapped_column(String(40))
    recipient_name_ciphertext: Mapped[bytes] = mapped_column(VARBINARY(512), nullable=False)
    phone_ciphertext: Mapped[bytes] = mapped_column(VARBINARY(512), nullable=False)
    phone_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    province_code: Mapped[str] = mapped_column(String(32), nullable=False)
    city_code: Mapped[str] = mapped_column(String(32), nullable=False)
    district_code: Mapped[str] = mapped_column(String(32), nullable=False)
    address_ciphertext: Mapped[bytes] = mapped_column(VARBINARY(2048), nullable=False)
    postal_code: Mapped[str | None] = mapped_column(String(16))
    address_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class OrderStatusLog(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "order_status_logs"
    __table_args__ = (Index("idx_order_status_logs_order", "order_id", "created_at", "id"),)

    order_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("orders.id"), nullable=False
    )
    state_dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    event_code: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(String(500))
    order_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))


class OrderOperationLog(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "order_operation_logs"
    __table_args__ = (UniqueConstraint("operation_no", name="uk_order_operation_no"),)

    operation_no: Mapped[str] = mapped_column(String(40), nullable=False)
    order_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("orders.id"), nullable=False
    )
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    request_payload_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    result_status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
