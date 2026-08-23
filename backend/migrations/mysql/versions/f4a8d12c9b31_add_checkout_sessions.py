"""add checkout sessions and immutable snapshots

Revision ID: f4a8d12c9b31
Revises: e3b7c2d91a40
Create Date: 2026-08-23 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "f4a8d12c9b31"
down_revision: str | Sequence[str] | None = "e3b7c2d91a40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "checkout_sessions",
        sa.Column("checkout_no", sa.String(length=40), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("checkout_status", sa.String(length=16), nullable=False),
        sa.Column("selected_address_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("goods_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("freight_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("payable_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "pricing_version", mysql.INTEGER(unsigned=True), server_default="1", nullable=False
        ),
        sa.Column("snapshot_hash", mysql.BINARY(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.CheckConstraint("source_type IN ('buy_now', 'cart')", name="checkout_source_type"),
        sa.CheckConstraint(
            "checkout_status IN ('active', 'submitted', 'expired', 'cancelled')",
            name="checkout_status",
        ),
        sa.ForeignKeyConstraint(
            ["selected_address_id"],
            ["user_addresses.id"],
            name="fk_checkout_sessions_selected_address_id_user_addresses",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_checkout_sessions_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_checkout_sessions"),
        sa.UniqueConstraint("checkout_no", name="uk_checkout_sessions_no"),
    )
    op.create_index(
        "idx_checkout_user_status",
        "checkout_sessions",
        ["user_id", "checkout_status", "created_at", "id"],
    )
    op.create_index(
        "idx_checkout_expiry", "checkout_sessions", ["checkout_status", "expires_at", "id"]
    )
    op.create_table(
        "checkout_snapshots",
        sa.Column("checkout_session_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("snapshot_version", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column(
            "schema_version", mysql.INTEGER(unsigned=True), server_default="1", nullable=False
        ),
        sa.Column("payload", mysql.JSON(), nullable=False),
        sa.Column("snapshot_hash", mysql.BINARY(length=32), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(), nullable=True),
        sa.Column("invalidation_reason", sa.String(length=64), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["checkout_session_id"],
            ["checkout_sessions.id"],
            name="fk_checkout_snapshots_checkout_session_id_checkout_sessions",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_checkout_snapshots"),
        sa.UniqueConstraint(
            "checkout_session_id", "snapshot_version", name="uk_checkout_snapshot_version"
        ),
        sa.UniqueConstraint(
            "checkout_session_id", "snapshot_hash", name="uk_checkout_snapshot_hash"
        ),
    )
    op.create_index(
        "idx_checkout_snapshot_session",
        "checkout_snapshots",
        ["checkout_session_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_checkout_snapshot_session", table_name="checkout_snapshots")
    op.drop_table("checkout_snapshots")
    op.drop_index("idx_checkout_expiry", table_name="checkout_sessions")
    op.drop_index("idx_checkout_user_status", table_name="checkout_sessions")
    op.drop_table("checkout_sessions")
