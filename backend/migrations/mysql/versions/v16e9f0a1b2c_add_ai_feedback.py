"""add governed AI message feedback

Revision ID: v16e9f0a1b2c
Revises: u05d8e9f0a1b
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "v16e9f0a1b2c"
down_revision = "u05d8e9f0a1b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_feedback",
        sa.Column("feedback_no", sa.String(40), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("conversation_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("message_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("ai_run_no", sa.String(40), nullable=True),
        sa.Column("feedback_type", sa.String(32), nullable=False),
        sa.Column(
            "reaction_slot",
            mysql.TINYINT(unsigned=True),
            sa.Computed(
                "CASE WHEN feedback_type IN ('thumb_up','thumb_down') THEN 1 ELSE NULL END"
            ),
            nullable=True,
        ),
        sa.Column(
            "active_reaction_key",
            sa.BINARY(32),
            sa.Computed(
                "CASE WHEN reaction_slot = 1 AND feedback_status = 'submitted' "
                "THEN UNHEX(SHA2(CONCAT(user_id, ':', message_id, ':', reaction_slot), 256)) "
                "ELSE NULL END"
            ),
            nullable=True,
        ),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("comment", sa.String(2000), nullable=True),
        sa.Column("content_hash", sa.BINARY(32), nullable=False),
        sa.Column(
            "detail_dedup_key",
            sa.BINARY(32),
            sa.Computed(
                "CASE WHEN feedback_type IN ('report','correction') "
                "THEN UNHEX(SHA2(CONCAT(user_id, ':', message_id, ':', feedback_type, ':', "
                "HEX(content_hash)), 256)) ELSE NULL END"
            ),
            nullable=True,
        ),
        sa.Column("feedback_status", sa.String(16), server_default="submitted", nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_code", sa.String(64), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("UTC_TIMESTAMP(6)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("UTC_TIMESTAMP(6)"), nullable=False),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.CheckConstraint(
            "feedback_type IN ('thumb_up','thumb_down','report','correction')",
            name="ai_feedback_type",
        ),
        sa.CheckConstraint(
            "feedback_status IN ('submitted','withdrawn','reviewed','resolved','dismissed')",
            name="ai_feedback_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_ai_feedback"),
        sa.UniqueConstraint("feedback_no", name="uk_ai_feedback_no"),
        sa.UniqueConstraint("active_reaction_key", name="uk_ai_feedback_active_reaction"),
        sa.UniqueConstraint("detail_dedup_key", name="uk_ai_feedback_detail_dedup"),
    )
    op.create_index("idx_ai_feedback_user_time", "ai_feedback", ["user_id", "created_at", "id"])
    op.create_index("idx_ai_feedback_message", "ai_feedback", ["message_id", "created_at", "id"])


def downgrade() -> None:
    op.drop_table("ai_feedback")
