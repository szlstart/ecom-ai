"""add human service tickets

Revision ID: 9e4a1b3c6d8f
Revises: 8d3f0a2b5c7e
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "9e4a1b3c6d8f"
down_revision: str | Sequence[str] | None = "8d3f0a2b5c7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "human_service_tickets",
        sa.Column("ticket_no", sa.String(40), nullable=False),
        sa.Column("conversation_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("queue_type", sa.String(16), nullable=False),
        sa.Column("ticket_status", sa.String(16), nullable=False),
        sa.Column("assigned_user_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("active_key", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("handoff_summary", sa.Text(), nullable=True),
        sa.Column("resolution_summary", sa.Text(), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["assigned_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_human_service_tickets"),
        sa.UniqueConstraint("ticket_no", name="uk_human_service_tickets_no"),
        sa.UniqueConstraint(
            "conversation_id", "active_key", name="uk_human_service_tickets_active"
        ),
    )
    op.create_index(
        "idx_human_service_tickets_queue",
        "human_service_tickets",
        ["queue_type", "ticket_status", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("human_service_tickets")
