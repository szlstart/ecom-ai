"""add permanent user cart

Revision ID: e3b7c2d91a40
Revises: c7e91f4a2d6b
Create Date: 2026-08-23 11:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "e3b7c2d91a40"
down_revision: str | Sequence[str] | None = "c7e91f4a2d6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "carts",
        sa.Column("cart_no", sa.String(length=40), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("cart_status", sa.String(length=16), nullable=False),
        sa.Column("item_count", mysql.INTEGER(unsigned=True), server_default="0", nullable=False),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.CheckConstraint("cart_status = 'active'", name="cart_status_active"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_carts_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_carts"),
        sa.UniqueConstraint("cart_no", name="uk_carts_no"),
        sa.UniqueConstraint("user_id", name="uk_carts_user"),
    )
    op.create_index("idx_carts_activity", "carts", ["last_activity_at", "id"], unique=False)
    op.create_table(
        "cart_items",
        sa.Column("cart_item_no", sa.String(length=40), nullable=False),
        sa.Column("cart_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("sku_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("quantity", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("is_selected", sa.Boolean(), nullable=False),
        sa.Column("added_price_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("sku_version", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("invalid_reason", sa.String(length=64), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.CheckConstraint("quantity BETWEEN 1 AND 99", name="cart_item_quantity_range"),
        sa.ForeignKeyConstraint(["cart_id"], ["carts.id"], name="fk_cart_items_cart_id_carts"),
        sa.ForeignKeyConstraint(
            ["sku_id"], ["product_skus.id"], name="fk_cart_items_sku_id_product_skus"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cart_items"),
        sa.UniqueConstraint("cart_item_no", name="uk_cart_items_no"),
        sa.UniqueConstraint("cart_id", "sku_id", name="uk_cart_items_cart_sku"),
    )
    op.create_index(
        "idx_cart_items_cart_selected",
        "cart_items",
        ["cart_id", "is_selected", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_cart_items_cart_selected", table_name="cart_items")
    op.drop_table("cart_items")
    op.drop_index("idx_carts_activity", table_name="carts")
    op.drop_table("carts")
