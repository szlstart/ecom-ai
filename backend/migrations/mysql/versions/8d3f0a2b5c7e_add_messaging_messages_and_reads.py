"""add messaging messages and read cursors

Revision ID: 8d3f0a2b5c7e
Revises: 7c2e9f1a4b6d
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "8d3f0a2b5c7e"
down_revision: str | Sequence[str] | None = "7c2e9f1a4b6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "store_user_key",
            mysql.BIGINT(unsigned=True),
            sa.Computed(
                "CASE WHEN conversation_type = 'store' AND deleted_at IS NULL "
                "THEN user_id ELSE NULL END"
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "last_sequence_no", mysql.BIGINT(unsigned=True), server_default="0", nullable=False
        ),
    )
    op.create_unique_constraint(
        "uk_conversations_store_user", "conversations", ["store_user_key", "store_id"]
    )
    op.create_table(
        "messages",
        sa.Column("message_no", sa.String(40), nullable=False),
        sa.Column("conversation_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("sequence_no", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("client_message_no", sa.String(40), nullable=True),
        sa.Column("sender_type", sa.String(16), nullable=False),
        sa.Column("sender_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=True),
        sa.Column("content_payload", sa.JSON(), nullable=True),
        sa.Column("message_status", sa.String(16), nullable=False),
        sa.Column("moderation_status", sa.String(16), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["sender_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.UniqueConstraint("message_no", name="uk_messages_no"),
        sa.UniqueConstraint("conversation_id", "sequence_no", name="uk_messages_sequence"),
        sa.UniqueConstraint("conversation_id", "client_message_no", name="uk_messages_client_no"),
    )
    op.create_index(
        "idx_messages_conversation_timeline", "messages", ["conversation_id", "sequence_no"]
    )
    op.create_table(
        "message_reads",
        sa.Column("conversation_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("reader_type", sa.String(16), nullable=False),
        sa.Column("reader_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("last_read_message_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("last_read_sequence_no", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("last_read_at", sa.DateTime(), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["last_read_message_id"], ["messages.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_message_reads"),
        sa.UniqueConstraint(
            "conversation_id", "reader_type", "reader_id", name="uk_message_reads_reader"
        ),
    )


def downgrade() -> None:
    op.drop_table("message_reads")
    op.drop_table("messages")
    op.drop_constraint("uk_conversations_store_user", "conversations", type_="unique")
    op.drop_column("conversations", "last_sequence_no")
    op.drop_column("conversations", "store_user_key")
