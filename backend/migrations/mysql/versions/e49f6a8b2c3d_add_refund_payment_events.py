"""add refund payment events

Revision ID: e49f6a8b2c3d
Revises: d38e5f7a1b2c
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "e49f6a8b2c3d"
down_revision = "d38e5f7a1b2c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refund_payment_events",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column("event_no", sa.String(40), nullable=False),
        sa.Column("refund_payment_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("provider_event_id", sa.String(128), nullable=False),
        sa.Column("provider_status", sa.String(32), nullable=False),
        sa.Column("amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["refund_payment_id"], ["refund_payment_records.id"]),
        sa.UniqueConstraint("event_no", name="uk_refund_payment_events_no"),
        sa.UniqueConstraint(
            "refund_payment_id", "provider_event_id", name="uk_refund_payment_events_provider"
        ),
        sa.Index("idx_refund_payment_events_record_time", "refund_payment_id", "created_at", "id"),
    )


def downgrade() -> None:
    op.drop_table("refund_payment_events")
