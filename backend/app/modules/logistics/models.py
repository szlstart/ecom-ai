from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY, INTEGER, VARBINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import AppendOnlyMySQLModel, MutableMySQLModel, MySQLBase


class Shipment(MutableMySQLModel, MySQLBase):
    __tablename__ = "shipments"
    __table_args__ = (
        UniqueConstraint("shipment_no", name="uk_shipments_no"),
        UniqueConstraint("carrier_code", "tracking_no_hash", name="uk_shipments_tracking"),
        CheckConstraint(
            "shipment_status IN ('created', 'picked_up', 'in_transit', 'delivered', "
            "'exception', 'returned', 'closed', 'voided')",
            name="shipment_status",
        ),
        CheckConstraint(
            "(estimated_delivery_min_at IS NULL AND estimated_delivery_max_at IS NULL) OR "
            "(estimated_delivery_min_at IS NOT NULL AND estimated_delivery_max_at IS NOT NULL "
            "AND estimated_delivery_min_at <= estimated_delivery_max_at)",
            name="shipment_delivery_estimate",
        ),
        CheckConstraint(
            "estimate_source IS NULL OR estimate_source IN ('shipping_template', 'carrier')",
            name="shipment_estimate_source",
        ),
        Index("idx_shipments_order", "order_id", "created_at"),
        Index("idx_shipments_status_sync", "shipment_status", "last_track_at"),
    )

    shipment_no: Mapped[str] = mapped_column(String(40), nullable=False)
    order_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("orders.id"), nullable=False
    )
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    carrier_code: Mapped[str] = mapped_column(String(32), nullable=False)
    carrier_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tracking_no_ciphertext: Mapped[bytes] = mapped_column(VARBINARY(512), nullable=False)
    tracking_no_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    tracking_no_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    shipment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_status: Mapped[str | None] = mapped_column(String(64))
    estimated_delivery_min_at: Mapped[datetime | None] = mapped_column(DateTime())
    estimated_delivery_max_at: Mapped[datetime | None] = mapped_column(DateTime())
    estimate_source: Mapped[str | None] = mapped_column(String(32))
    estimate_updated_at: Mapped[datetime | None] = mapped_column(DateTime())
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime())
    last_track_at: Mapped[datetime | None] = mapped_column(DateTime())
    voided_at: Mapped[datetime | None] = mapped_column(DateTime())
    void_reason_code: Mapped[str | None] = mapped_column(String(64))
    void_reason: Mapped[str | None] = mapped_column(String(1000))
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)


class ShipmentItem(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "shipment_items"
    __table_args__ = (
        UniqueConstraint("shipment_id", "order_item_id", name="uk_shipment_items_item"),
        CheckConstraint("quantity > 0", name="shipment_item_quantity"),
        Index("idx_shipment_items_order_item", "order_item_id", "shipment_id"),
    )

    shipment_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("shipments.id"), nullable=False
    )
    order_item_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("order_items.id"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)


class ShipmentTrack(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "shipment_tracks"
    __table_args__ = (
        UniqueConstraint("shipment_id", "provider_event_id", name="uk_shipment_tracks_event"),
        UniqueConstraint(
            "shipment_id", "occurred_at", "payload_hash", name="uk_shipment_tracks_fallback"
        ),
        Index("idx_shipment_tracks_timeline", "shipment_id", "occurred_at", "id"),
    )

    shipment_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("shipments.id"), nullable=False
    )
    provider_event_id: Mapped[str | None] = mapped_column(String(128))
    track_status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_status: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    location_text: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    payload_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)


class LogisticsSyncLog(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "logistics_sync_logs"
    __table_args__ = (
        CheckConstraint(
            "sync_type IN ('poll', 'webhook', 'reconcile')",
            name="logistics_sync_type",
        ),
        CheckConstraint(
            "sync_status IN ('success', 'no_change', 'retry', 'failed')",
            name="logistics_sync_status",
        ),
        Index("idx_logistics_sync_retry", "sync_status", "next_retry_at", "id"),
        Index("idx_logistics_sync_shipment", "shipment_id", "created_at"),
    )

    shipment_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("shipments.id"), nullable=False
    )
    sync_type: Mapped[str] = mapped_column(String(16), nullable=False)
    sync_status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    response_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    track_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime())
    error_code: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    trace_id: Mapped[str | None] = mapped_column(String(64))
