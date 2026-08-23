from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, INTEGER
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import AppendOnlyMySQLModel, MutableMySQLModel, MySQLBase


class Inventory(MutableMySQLModel, MySQLBase):
    __tablename__ = "inventories"
    __table_args__ = (
        UniqueConstraint("sku_id", name="uk_inventories_sku"),
        CheckConstraint("reserved_quantity <= on_hand_quantity", name="inventory_reserved_on_hand"),
        CheckConstraint(
            "safety_stock_quantity <= on_hand_quantity", name="inventory_safety_on_hand"
        ),
        Index("idx_inventories_status", "inventory_status", "updated_at", "id"),
    )

    sku_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("product_skus.id"), nullable=False
    )
    on_hand_quantity: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default="0"
    )
    reserved_quantity: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default="0"
    )
    safety_stock_quantity: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default="0"
    )
    sold_quantity: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    inventory_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class InventoryReservation(MutableMySQLModel, MySQLBase):
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        UniqueConstraint("reservation_no", name="uk_inventory_reservations_no"),
        UniqueConstraint("order_item_id", name="uk_inventory_reservations_order_item"),
        UniqueConstraint("idempotency_key", name="uk_inventory_reservations_idempotency"),
        CheckConstraint("quantity > 0", name="inventory_reservation_quantity"),
        Index(
            "idx_inventory_reservations_expiry",
            "reservation_status",
            "expires_at",
            "id",
        ),
    )

    reservation_no: Mapped[str] = mapped_column(String(40), nullable=False)
    inventory_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("inventories.id"), nullable=False
    )
    sku_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("product_skus.id"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("orders.id"), nullable=False
    )
    order_item_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("order_items.id"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    reservation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    release_reason: Mapped[str | None] = mapped_column(String(64))


class InventoryLog(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "inventory_logs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uk_inventory_logs_idempotency"),
        CheckConstraint("on_hand_after >= 0", name="inventory_log_on_hand_nonnegative"),
        CheckConstraint("reserved_after >= 0", name="inventory_log_reserved_nonnegative"),
        Index("idx_inventory_logs_sku_time", "sku_id", "created_at", "id"),
        Index("idx_inventory_logs_reference", "reference_type", "reference_no"),
    )

    inventory_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("inventories.id"), nullable=False
    )
    sku_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("product_skus.id"), nullable=False
    )
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    on_hand_delta: Mapped[int] = mapped_column(nullable=False)
    reserved_delta: Mapped[int] = mapped_column(nullable=False)
    on_hand_before: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    on_hand_after: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    reserved_before: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    reserved_after: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    reference_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reference_no: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(String(500))
    inventory_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
