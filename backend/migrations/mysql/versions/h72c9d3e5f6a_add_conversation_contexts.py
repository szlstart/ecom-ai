"""add conversation contexts

Revision ID: h72c9d3e5f6a
Revises: g61b8c2d4e5f
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "h72c9d3e5f6a"
down_revision = "g61b8c2d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_contexts",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), nullable=False, server_default="0"),
        sa.Column("context_no", sa.String(40), nullable=False),
        sa.Column("conversation_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("context_type", sa.String(32), nullable=False),
        sa.Column("resource_no", sa.String(64), nullable=False),
        sa.Column("resource_version", mysql.BIGINT(unsigned=True)),
        sa.Column("context_version", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("context_status", sa.String(16), nullable=False),
        sa.Column("active_context_key", sa.String(96)),
        sa.Column("display_snapshot", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime()),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.UniqueConstraint("context_no", name="uk_conversation_contexts_no"),
        sa.UniqueConstraint("active_context_key", name="uk_conversation_contexts_active"),
        sa.Index(
            "idx_contexts_conversation_status",
            "conversation_id",
            "context_status",
            "created_at",
        ),
    )


def downgrade() -> None:
    op.drop_table("conversation_contexts")
