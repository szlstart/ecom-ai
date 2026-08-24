"""add logistics shipments

Revision ID: e7a4c2d91b63
Revises: d3f8a6b21c40
Create Date: 2026-08-23 13:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "e7a4c2d91b63"
down_revision: str | Sequence[str] | None = "d3f8a6b21c40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps(*, mutable: bool) -> list[sa.Column[object]]:
    columns: list[sa.Column[object]] = [
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        )
    ]
    if mutable:
        columns.extend(
            [
                sa.Column(
                    "updated_at",
                    sa.DateTime(),
                    server_default=sa.text("utc_timestamp(6)"),
                    nullable=False,
                ),
                sa.Column(
                    "version",
                    mysql.BIGINT(unsigned=True),
                    server_default="0",
                    nullable=False,
                ),
            ]
        )
    return columns


def upgrade() -> None:
    op.create_table(
        "shipments",
        sa.Column("shipment_no", sa.String(40), nullable=False),
        sa.Column("order_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("store_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("carrier_code", sa.String(32), nullable=False),
        sa.Column("carrier_name", sa.String(64), nullable=False),
        sa.Column("tracking_no_ciphertext", mysql.VARBINARY(512), nullable=False),
        sa.Column("tracking_no_hash", mysql.BINARY(32), nullable=False),
        sa.Column("tracking_no_masked", sa.String(64), nullable=False),
        sa.Column("shipment_status", sa.String(32), nullable=False),
        sa.Column("provider_status", sa.String(64)),
        sa.Column("estimated_delivery_min_at", sa.DateTime()),
        sa.Column("estimated_delivery_max_at", sa.DateTime()),
        sa.Column("estimate_source", sa.String(32)),
        sa.Column("estimate_updated_at", sa.DateTime()),
        sa.Column("shipped_at", sa.DateTime()),
        sa.Column("delivered_at", sa.DateTime()),
        sa.Column("last_track_at", sa.DateTime()),
        sa.Column("voided_at", sa.DateTime()),
        sa.Column("void_reason_code", sa.String(64)),
        sa.Column("void_reason", sa.String(1000)),
        sa.Column("key_version", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        *_timestamps(mutable=True),
        sa.CheckConstraint(
            "shipment_status IN ('created', 'picked_up', 'in_transit', 'delivered', "
            "'exception', 'returned', 'closed', 'voided')",
            name="shipment_status",
        ),
        sa.CheckConstraint(
            "(estimated_delivery_min_at IS NULL AND estimated_delivery_max_at IS NULL) OR "
            "(estimated_delivery_min_at IS NOT NULL AND "
            "estimated_delivery_max_at IS NOT NULL AND "
            "estimated_delivery_min_at <= estimated_delivery_max_at)",
            name="shipment_delivery_estimate",
        ),
        sa.CheckConstraint(
            "estimate_source IS NULL OR estimate_source IN ('shipping_template', 'carrier')",
            name="shipment_estimate_source",
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_shipments_order_id_orders"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_shipments_store_id_stores"),
        sa.PrimaryKeyConstraint("id", name="pk_shipments"),
        sa.UniqueConstraint("shipment_no", name="uk_shipments_no"),
        sa.UniqueConstraint("carrier_code", "tracking_no_hash", name="uk_shipments_tracking"),
    )
    op.create_index("idx_shipments_order", "shipments", ["order_id", "created_at"])
    op.create_index("idx_shipments_status_sync", "shipments", ["shipment_status", "last_track_at"])

    op.create_table(
        "shipment_items",
        sa.Column("shipment_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("order_item_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("quantity", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        *_timestamps(mutable=False),
        sa.CheckConstraint("quantity > 0", name="shipment_item_quantity"),
        sa.ForeignKeyConstraint(
            ["shipment_id"], ["shipments.id"], name="fk_shipment_items_shipment_id_shipments"
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"],
            ["order_items.id"],
            name="fk_shipment_items_order_item_id_order_items",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_shipment_items"),
        sa.UniqueConstraint("shipment_id", "order_item_id", name="uk_shipment_items_item"),
    )
    op.create_index(
        "idx_shipment_items_order_item",
        "shipment_items",
        ["order_item_id", "shipment_id"],
    )

    op.create_table(
        "shipment_tracks",
        sa.Column("shipment_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("provider_event_id", sa.String(128)),
        sa.Column("track_status", sa.String(32), nullable=False),
        sa.Column("provider_status", sa.String(64)),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("location_text", sa.String(255)),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("payload_hash", mysql.BINARY(32), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        *_timestamps(mutable=False),
        sa.ForeignKeyConstraint(
            ["shipment_id"], ["shipments.id"], name="fk_shipment_tracks_shipment_id_shipments"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_shipment_tracks"),
        sa.UniqueConstraint("shipment_id", "provider_event_id", name="uk_shipment_tracks_event"),
        sa.UniqueConstraint(
            "shipment_id",
            "occurred_at",
            "payload_hash",
            name="uk_shipment_tracks_fallback",
        ),
    )
    op.create_index(
        "idx_shipment_tracks_timeline",
        "shipment_tracks",
        ["shipment_id", "occurred_at", "id"],
    )

    op.create_table(
        "logistics_sync_logs",
        sa.Column("shipment_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("sync_type", sa.String(16), nullable=False),
        sa.Column("sync_status", sa.String(16), nullable=False),
        sa.Column("provider_request_id", sa.String(128)),
        sa.Column("response_hash", mysql.BINARY(32)),
        sa.Column("track_count", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("attempt_count", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("duration_ms", mysql.INTEGER(unsigned=True), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("last_error", sa.String(1000)),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        *_timestamps(mutable=False),
        sa.CheckConstraint(
            "sync_type IN ('poll', 'webhook', 'reconcile')", name="logistics_sync_type"
        ),
        sa.CheckConstraint(
            "sync_status IN ('success', 'no_change', 'retry', 'failed')",
            name="logistics_sync_status",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name="fk_logistics_sync_logs_shipment_id_shipments",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_logistics_sync_logs"),
    )
    op.create_index(
        "idx_logistics_sync_retry",
        "logistics_sync_logs",
        ["sync_status", "next_retry_at", "id"],
    )
    op.create_index(
        "idx_logistics_sync_shipment",
        "logistics_sync_logs",
        ["shipment_id", "created_at"],
    )


def downgrade() -> None:
    # MySQL automatically retains FK-supporting indexes; drop tables directly
    # so downgrade remains safe after a partially-completed non-transactional DDL.
    for table in ("logistics_sync_logs", "shipment_tracks", "shipment_items", "shipments"):
        op.execute(sa.text(f"DROP TABLE IF EXISTS `{table}`"))
