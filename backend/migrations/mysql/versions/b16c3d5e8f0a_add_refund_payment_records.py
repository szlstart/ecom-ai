"""add refund payment execution records

Revision ID: b16c3d5e8f0a
Revises: a05b2c4d7e9f
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "b16c3d5e8f0a"
down_revision: str | Sequence[str] | None = "a05b2c4d7e9f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refund_payment_records",
        sa.Column("refund_payment_no", sa.String(40), nullable=False),
        sa.Column("refund_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("payment_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_refund_no", sa.String(128), nullable=True),
        sa.Column("amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("payment_status", sa.String(16), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["refund_id"], ["refund_applications.id"]),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_refund_payment_records"),
        sa.UniqueConstraint("refund_payment_no", name="uk_refund_payment_records_no"),
        sa.UniqueConstraint("refund_id", name="uk_refund_payment_records_refund"),
    )
    op.create_index(
        "idx_refund_payment_records_status",
        "refund_payment_records",
        ["payment_status", "updated_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("refund_payment_records")
