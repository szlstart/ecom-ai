"""add payment callbacks

Revision ID: d3f8a6b21c40
Revises: b8e4c1a29d70
Create Date: 2026-08-23 13:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "d3f8a6b21c40"
down_revision: str | Sequence[str] | None = "b8e4c1a29d70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_callbacks",
        sa.Column("callback_no", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_event_id", sa.String(128)),
        sa.Column("payment_id", mysql.BIGINT(unsigned=True)),
        sa.Column("headers_hash", mysql.BINARY(32), nullable=False),
        sa.Column("payload_hash", mysql.BINARY(32), nullable=False),
        sa.Column("payload_redacted", mysql.JSON()),
        sa.Column("signature_status", sa.String(16), nullable=False),
        sa.Column("process_status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("processed_at", sa.DateTime()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("last_error", sa.String(1000)),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.CheckConstraint(
            "signature_status IN ('valid', 'invalid', 'error')",
            name="payment_callback_signature_status",
        ),
        sa.CheckConstraint(
            "process_status IN ('received', 'processed', 'duplicate', 'rejected', 'failed')",
            name="payment_callback_process_status",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payments.id"], name="fk_payment_callbacks_payment_id_payments"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment_callbacks"),
        sa.UniqueConstraint("callback_no", name="uk_payment_callbacks_no"),
        sa.UniqueConstraint(
            "provider", "provider_event_id", name="uk_payment_callbacks_provider_event"
        ),
    )
    op.create_index(
        "idx_payment_callbacks_status",
        "payment_callbacks",
        ["process_status", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_payment_callbacks_status", table_name="payment_callbacks")
    op.drop_table("payment_callbacks")
