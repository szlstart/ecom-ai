from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, INTEGER
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import MutableMySQLModel, MySQLBase


class Cart(MutableMySQLModel, MySQLBase):
    __tablename__ = "carts"
    __table_args__ = (
        UniqueConstraint("cart_no", name="uk_carts_no"),
        UniqueConstraint("user_id", name="uk_carts_user"),
        CheckConstraint("cart_status = 'active'", name="cart_status_active"),
        Index("idx_carts_activity", "last_activity_at", "id"),
    )

    cart_no: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    cart_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    item_count: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default="0"
    )
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class CartItem(MutableMySQLModel, MySQLBase):
    __tablename__ = "cart_items"
    __table_args__ = (
        UniqueConstraint("cart_item_no", name="uk_cart_items_no"),
        UniqueConstraint("cart_id", "sku_id", name="uk_cart_items_cart_sku"),
        CheckConstraint("quantity BETWEEN 1 AND 99", name="cart_item_quantity_range"),
        Index("idx_cart_items_cart_selected", "cart_id", "is_selected", "id"),
    )

    cart_item_no: Mapped[str] = mapped_column(String(40), nullable=False)
    cart_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("carts.id"), nullable=False
    )
    sku_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("product_skus.id"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    is_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    added_price_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    sku_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    invalid_reason: Mapped[str | None] = mapped_column(String(64))
