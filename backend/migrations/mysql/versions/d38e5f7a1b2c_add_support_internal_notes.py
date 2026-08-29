"""add support internal notes

Revision ID: d38e5f7a1b2c
Revises: c27d4e6f9a1b
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "d38e5f7a1b2c"
down_revision = "c27d4e6f9a1b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "human_service_internal_notes",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), nullable=False, server_default="1"),
        sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("author_user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("note_text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["human_service_tickets.id"]),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"]),
        sa.Index("idx_human_service_notes_ticket_time", "ticket_id", "created_at", "id"),
    )


def downgrade() -> None:
    op.drop_table("human_service_internal_notes")
