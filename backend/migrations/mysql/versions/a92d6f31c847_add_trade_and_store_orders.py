"""add trade and store orders

Revision ID: a92d6f31c847
Revises: f4a8d12c9b31
Create Date: 2026-08-23 16:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "a92d6f31c847"
down_revision: str | Sequence[str] | None = "f4a8d12c9b31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _mutable_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
    ]


def _append_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
    ]


def upgrade() -> None:
    op.alter_column(
        "checkout_snapshots",
        "payload",
        new_column_name="snapshot_payload",
        existing_type=mysql.JSON(),
        existing_nullable=False,
    )
    op.alter_column(
        "checkout_snapshots",
        "invalidation_reason",
        new_column_name="invalid_reason",
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "checkout_sessions",
        "pricing_version",
        existing_type=mysql.INTEGER(unsigned=True),
        type_=sa.String(length=32),
        existing_nullable=False,
        server_default="pricing_v1",
    )
    op.execute("UPDATE checkout_sessions SET pricing_version = 'pricing_v1'")
    op.create_table(
        "trade_orders",
        sa.Column("trade_no", sa.String(32), nullable=False),
        sa.Column("checkout_session_id", mysql.BIGINT(unsigned=True)),
        sa.Column("checkout_no_snapshot", sa.String(40), nullable=False),
        sa.Column("checkout_snapshot_hash", mysql.BINARY(32), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("order_source", sa.String(16), nullable=False),
        sa.Column("trade_status", sa.String(32), nullable=False),
        sa.Column("goods_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("freight_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("payable_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("adjustment_amount", mysql.BIGINT(), server_default="0", nullable=False),
        sa.Column("paid_amount", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.Column(
            "refunded_amount", mysql.BIGINT(unsigned=True), server_default="0", nullable=False
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("order_count", sa.SmallInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("paid_at", sa.DateTime()),
        sa.Column("closed_at", sa.DateTime()),
        *_mutable_columns(),
        sa.CheckConstraint("order_source IN ('buy_now', 'cart')", name="trade_order_source"),
        sa.CheckConstraint(
            "trade_status IN ('pending_payment', 'paid', 'closed', "
            "'partially_refunded', 'refunded')",
            name="trade_order_status",
        ),
        sa.CheckConstraint(
            "payable_amount = goods_amount + freight_amount + adjustment_amount "
            "AND payable_amount >= 0",
            name="trade_order_amounts",
        ),
        sa.CheckConstraint(
            "refunded_amount <= paid_amount AND paid_amount <= payable_amount",
            name="trade_order_paid_refunded",
        ),
        sa.ForeignKeyConstraint(
            ["checkout_session_id"],
            ["checkout_sessions.id"],
            name="fk_trade_orders_checkout_session_id_checkout_sessions",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_trade_orders_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_trade_orders"),
        sa.UniqueConstraint("trade_no", name="uk_trade_orders_no"),
        sa.UniqueConstraint("checkout_session_id", name="uk_trade_orders_checkout"),
        sa.UniqueConstraint("checkout_no_snapshot", name="uk_trade_orders_checkout_snapshot"),
    )
    op.create_index("idx_trade_orders_user_time", "trade_orders", ["user_id", "created_at", "id"])
    op.create_index(
        "idx_trade_orders_status_expiry", "trade_orders", ["trade_status", "expires_at", "id"]
    )
    op.create_table(
        "orders",
        sa.Column("order_no", sa.String(32), nullable=False),
        sa.Column("trade_order_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("store_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("order_status", sa.String(32), nullable=False),
        sa.Column("payment_status", sa.String(32), nullable=False),
        sa.Column("fulfillment_status", sa.String(32), nullable=False),
        sa.Column("after_sale_status", sa.String(32), nullable=False),
        sa.Column("goods_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("freight_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("payable_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("adjustment_amount", mysql.BIGINT(), server_default="0", nullable=False),
        sa.Column("paid_amount", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.Column(
            "refunded_amount", mysql.BIGINT(unsigned=True), server_default="0", nullable=False
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("adjustment_reason_code", sa.String(64)),
        sa.Column("adjustment_reason", sa.String(500)),
        sa.Column("adjusted_by", mysql.BIGINT(unsigned=True)),
        sa.Column("adjusted_at", sa.DateTime()),
        sa.Column("buyer_remark", sa.String(500)),
        sa.Column("policy_snapshot", mysql.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("paid_at", sa.DateTime()),
        sa.Column("shipped_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("closed_at", sa.DateTime()),
        sa.Column("user_hidden_at", sa.DateTime()),
        sa.Column("undo_until", sa.DateTime()),
        *_mutable_columns(),
        sa.CheckConstraint(
            "order_status IN ('pending_payment', 'paid', 'pending_shipment', "
            "'shipped', 'completed', 'cancelled', 'closed')",
            name="order_status",
        ),
        sa.CheckConstraint(
            "payment_status IN ('unpaid', 'processing', 'paid', 'partially_refunded', 'refunded')",
            name="order_payment_status",
        ),
        sa.CheckConstraint(
            "fulfillment_status IN ('unfulfilled', 'partial', 'shipped', 'received')",
            name="order_fulfillment_status",
        ),
        sa.CheckConstraint(
            "after_sale_status IN ('none', 'in_progress', 'partial', 'completed')",
            name="order_after_sale_status",
        ),
        sa.CheckConstraint(
            "payable_amount = goods_amount + freight_amount + adjustment_amount "
            "AND payable_amount >= 0",
            name="order_amounts",
        ),
        sa.CheckConstraint(
            "refunded_amount <= paid_amount AND paid_amount <= payable_amount",
            name="order_paid_refunded",
        ),
        sa.ForeignKeyConstraint(
            ["trade_order_id"], ["trade_orders.id"], name="fk_orders_trade_order_id_trade_orders"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_orders_user_id_users"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_orders_store_id_stores"),
        sa.ForeignKeyConstraint(["adjusted_by"], ["users.id"], name="fk_orders_adjusted_by_users"),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.UniqueConstraint("order_no", name="uk_orders_no"),
    )
    op.create_index(
        "idx_orders_user_status_time", "orders", ["user_id", "order_status", "created_at", "id"]
    )
    op.create_index(
        "idx_orders_store_status_time", "orders", ["store_id", "order_status", "created_at", "id"]
    )
    op.create_index("idx_orders_trade", "orders", ["trade_order_id", "id"])
    op.create_table(
        "order_items",
        sa.Column("order_item_no", sa.String(40), nullable=False),
        sa.Column("order_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("product_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("sku_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("product_no", sa.String(40), nullable=False),
        sa.Column("sku_no", sa.String(40), nullable=False),
        sa.Column("product_name", sa.String(255), nullable=False),
        sa.Column("sku_name", sa.String(255), nullable=False),
        sa.Column("spec_snapshot", mysql.JSON(), nullable=False),
        sa.Column("image_object_key", sa.String(512)),
        sa.Column("quantity", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("unit_price_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("market_price_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("gross_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("payable_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("adjustment_amount", mysql.BIGINT(), server_default="0", nullable=False),
        sa.Column(
            "refunded_quantity", mysql.INTEGER(unsigned=True), server_default="0", nullable=False
        ),
        sa.Column(
            "refunded_amount", mysql.BIGINT(unsigned=True), server_default="0", nullable=False
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("review_status", sa.String(16), nullable=False),
        sa.Column("after_sale_status", sa.String(16), nullable=False),
        *_mutable_columns(),
        sa.CheckConstraint("quantity > 0", name="order_item_quantity"),
        sa.CheckConstraint("gross_amount = unit_price_amount * quantity", name="order_item_gross"),
        sa.CheckConstraint(
            "payable_amount = gross_amount + adjustment_amount AND payable_amount >= 0",
            name="order_item_payable",
        ),
        sa.CheckConstraint(
            "refunded_quantity <= quantity AND refunded_amount <= payable_amount",
            name="order_item_refunded",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending', 'reviewed', 'closed')", name="order_item_review"
        ),
        sa.CheckConstraint(
            "after_sale_status IN ('none', 'in_progress', 'completed')",
            name="order_item_after_sale",
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_order_items_order_id_orders"),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_order_items_product_id_products"
        ),
        sa.ForeignKeyConstraint(
            ["sku_id"], ["product_skus.id"], name="fk_order_items_sku_id_product_skus"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_items"),
        sa.UniqueConstraint("order_item_no", name="uk_order_items_no"),
    )
    op.create_index("idx_order_items_order", "order_items", ["order_id", "id"])
    op.create_index("idx_order_items_sku", "order_items", ["sku_id", "created_at", "id"])
    op.create_table(
        "order_addresses",
        sa.Column("order_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("source_address_no", sa.String(40)),
        sa.Column("recipient_name_ciphertext", mysql.VARBINARY(512), nullable=False),
        sa.Column("phone_ciphertext", mysql.VARBINARY(512), nullable=False),
        sa.Column("phone_last4", sa.String(4), nullable=False),
        sa.Column("country_code", sa.String(2), nullable=False),
        sa.Column("province_code", sa.String(32), nullable=False),
        sa.Column("city_code", sa.String(32), nullable=False),
        sa.Column("district_code", sa.String(32), nullable=False),
        sa.Column("address_ciphertext", mysql.VARBINARY(2048), nullable=False),
        sa.Column("postal_code", sa.String(16)),
        sa.Column("address_hash", mysql.BINARY(32), nullable=False),
        sa.Column("key_version", sa.SmallInteger(), nullable=False),
        *_append_columns(),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_order_addresses_order_id_orders"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_addresses"),
        sa.UniqueConstraint("order_id", name="uk_order_addresses_order"),
    )
    op.create_table(
        "order_status_logs",
        sa.Column("order_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("state_dimension", sa.String(32), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("event_code", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", mysql.BIGINT(unsigned=True)),
        sa.Column("reason", sa.String(500)),
        sa.Column("order_version", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("request_id", sa.String(64)),
        sa.Column("trace_id", sa.String(64)),
        *_append_columns(),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_order_status_logs_order_id_orders"
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name="fk_order_status_logs_actor_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_status_logs"),
    )
    op.create_index(
        "idx_order_status_logs_order", "order_status_logs", ["order_id", "created_at", "id"]
    )
    op.create_table(
        "order_operation_logs",
        sa.Column("operation_no", sa.String(40), nullable=False),
        sa.Column("order_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("operation_type", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", mysql.BIGINT(unsigned=True)),
        sa.Column("request_payload_hash", mysql.BINARY(32)),
        sa.Column("result_status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        *_append_columns(),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_order_operation_logs_order_id_orders"
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name="fk_order_operation_logs_actor_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_operation_logs"),
        sa.UniqueConstraint("operation_no", name="uk_order_operation_no"),
    )
    op.create_foreign_key(
        "fk_inventory_reservations_order_id_orders",
        "inventory_reservations",
        "orders",
        ["order_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_inventory_reservations_order_item_id_order_items",
        "inventory_reservations",
        "order_items",
        ["order_item_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_inventory_reservations_order_item_id_order_items",
        "inventory_reservations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_inventory_reservations_order_id_orders", "inventory_reservations", type_="foreignkey"
    )
    op.drop_table("order_operation_logs")
    op.drop_table("order_status_logs")
    op.drop_table("order_addresses")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.drop_table("trade_orders")
    op.alter_column(
        "checkout_snapshots",
        "invalid_reason",
        new_column_name="invalidation_reason",
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "checkout_snapshots",
        "snapshot_payload",
        new_column_name="payload",
        existing_type=mysql.JSON(),
        existing_nullable=False,
    )
    op.execute("UPDATE checkout_sessions SET pricing_version = '1'")
    op.alter_column(
        "checkout_sessions",
        "pricing_version",
        existing_type=sa.String(32),
        type_=mysql.INTEGER(unsigned=True),
        existing_nullable=False,
        server_default="1",
    )
