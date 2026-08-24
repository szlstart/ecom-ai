"""add after-sale refund applications and events

Revision ID: 7c2e9f1a4b6d
Revises: 6b91d4e2a7c5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "7c2e9f1a4b6d"
down_revision: str | Sequence[str] | None = "6b91d4e2a7c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refund_applications",
        sa.Column("refund_no", sa.String(40), nullable=False),
        sa.Column("order_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("store_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("refund_type", sa.String(24), nullable=False),
        sa.Column("refund_status", sa.String(24), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("reason_detail", sa.String(500), nullable=True),
        sa.Column("requested_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column(
            "approved_amount", mysql.BIGINT(unsigned=True), nullable=False, server_default="0"
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("contact_phone_last4", sa.String(4), nullable=True),
        sa.Column("eligibility_token_hash", mysql.BINARY(32), nullable=True),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_refund_applications"),
        sa.UniqueConstraint("refund_no", name="uk_refund_applications_no"),
        sa.CheckConstraint(
            "refund_type IN ('refund_only','return_and_refund')", name="refund_application_type"
        ),
        sa.CheckConstraint(
            "refund_status IN ('submitted','merchant_review','approved','waiting_return',"
            "'returning','received','refunding','succeeded','rejected','cancelled','closed')",
            name="refund_application_status",
        ),
    )
    op.create_index(
        "idx_refund_applications_user_time", "refund_applications", ["user_id", "created_at", "id"]
    )
    op.create_index(
        "idx_refund_applications_order_status",
        "refund_applications",
        ["order_id", "refund_status", "id"],
    )
    op.create_table(
        "refund_items",
        sa.Column("refund_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("order_item_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("quantity", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("requested_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column(
            "succeeded_amount", mysql.BIGINT(unsigned=True), nullable=False, server_default="0"
        ),
        sa.Column("refund_status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["refund_id"], ["refund_applications.id"]),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_refund_items"),
        sa.UniqueConstraint("refund_id", "order_item_id", name="uk_refund_items_refund_item"),
    )
    op.create_index(
        "idx_refund_items_order_item_status",
        "refund_items",
        ["order_item_id", "refund_status", "id"],
    )
    op.create_table(
        "refund_events",
        sa.Column("event_no", sa.String(40), nullable=False),
        sa.Column("refund_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("from_status", sa.String(24), nullable=True),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("event_code", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_user_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.ForeignKeyConstraint(["refund_id"], ["refund_applications.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_refund_events"),
        sa.UniqueConstraint("event_no", name="uk_refund_events_no"),
    )
    op.create_index(
        "idx_refund_events_refund_time", "refund_events", ["refund_id", "created_at", "id"]
    )
    op.create_table(
        "refund_appeals",
        sa.Column("appeal_no", sa.String(40), nullable=False),
        sa.Column("refund_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("appeal_status", sa.String(16), nullable=False, server_default="submitted"),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["refund_id"], ["refund_applications.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_refund_appeals"),
        sa.UniqueConstraint("appeal_no", name="uk_refund_appeals_no"),
        sa.UniqueConstraint("refund_id", name="uk_refund_appeals_refund"),
    )


def downgrade() -> None:
    op.drop_table("refund_appeals")
    op.drop_table("refund_events")
    op.drop_table("refund_items")
    op.drop_table("refund_applications")
