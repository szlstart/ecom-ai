"""add refund shipments

Revision ID: f50a7b9c3d4e
Revises: e49f6a8b2c3d
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "f50a7b9c3d4e"
down_revision = "e49f6a8b2c3d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refund_shipments",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), nullable=False, server_default="0"),
        sa.Column("refund_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("carrier_code", sa.String(32), nullable=False),
        sa.Column("carrier_name", sa.String(64), nullable=False),
        sa.Column("tracking_no_ciphertext", mysql.VARBINARY(512), nullable=False),
        sa.Column("tracking_no_hash", mysql.BINARY(32), nullable=False),
        sa.Column("tracking_no_masked", sa.String(64), nullable=False),
        sa.Column("shipment_status", sa.String(32), nullable=False),
        sa.Column("shipped_at", sa.DateTime()),
        sa.Column("delivered_at", sa.DateTime()),
        sa.Column("received_at", sa.DateTime()),
        sa.Column("key_version", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["refund_id"], ["refund_applications.id"]),
        sa.UniqueConstraint("refund_id", name="uk_refund_shipments_refund"),
    )


def downgrade() -> None:
    op.drop_table("refund_shipments")
