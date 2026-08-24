"""add payment attempts

Revision ID: b8e4c1a29d70
Revises: a92d6f31c847
Create Date: 2026-08-23 12:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "b8e4c1a29d70"
down_revision: str | Sequence[str] | None = "a92d6f31c847"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("payment_no", sa.String(32), nullable=False),
        sa.Column("trade_order_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("payment_method", sa.String(32), nullable=False),
        sa.Column("provider_trade_no", sa.String(128)),
        sa.Column("payment_status", sa.String(32), nullable=False),
        sa.Column("requested_amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("paid_amount", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.Column(
            "refunded_amount", mysql.BIGINT(unsigned=True), server_default="0", nullable=False
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("client_ip_hash", mysql.BINARY(32)),
        sa.Column("provider_request_id", sa.String(128)),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("paid_at", sa.DateTime()),
        sa.Column("closed_at", sa.DateTime()),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("failure_message", sa.String(500)),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.CheckConstraint(
            "payment_status IN ('created', 'pending', 'succeeded', 'failed', 'closed', "
            "'partially_refunded', 'refunded')",
            name="payment_status",
        ),
        sa.CheckConstraint(
            "refunded_amount <= paid_amount AND paid_amount <= requested_amount",
            name="payment_amounts",
        ),
        sa.ForeignKeyConstraint(
            ["trade_order_id"],
            ["trade_orders.id"],
            name="fk_payments_trade_order_id_trade_orders",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_payments_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_payments"),
        sa.UniqueConstraint("payment_no", name="uk_payments_no"),
        sa.UniqueConstraint("provider", "provider_trade_no", name="uk_payments_provider_trade"),
    )
    op.create_index(
        "idx_payments_trade_status",
        "payments",
        ["trade_order_id", "payment_status", "created_at"],
    )
    op.create_index("idx_payments_status_expiry", "payments", ["payment_status", "expires_at"])
    op.create_table(
        "payment_events",
        sa.Column("event_no", sa.String(40), nullable=False),
        sa.Column("payment_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_no", sa.String(64), nullable=False),
        sa.Column("provider_occurred_at", sa.DateTime()),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payments.id"], name="fk_payment_events_payment_id_payments"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment_events"),
        sa.UniqueConstraint("event_no", name="uk_payment_events_no"),
    )
    op.create_index(
        "idx_payment_events_payment",
        "payment_events",
        ["payment_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS `payment_events`"))
    op.execute(sa.text("DROP TABLE IF EXISTS `payments`"))
