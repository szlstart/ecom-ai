"""add user-visible AI memory cleanup tasks

Revision ID: w27f0a1b2c3d
Revises: v16e9f0a1b2c
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "w27f0a1b2c3d"
down_revision = "v16e9f0a1b2c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_memory_cleanup_tasks",
        sa.Column("task_no", sa.String(40), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("command_type", sa.String(32), nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_no", sa.String(64), nullable=False),
        sa.Column("source_resource_type", sa.String(32), nullable=False),
        sa.Column("source_resource_no", sa.String(64), nullable=False),
        sa.Column("task_status", sa.String(20), server_default="queued", nullable=False),
        sa.Column("total_count", mysql.INTEGER(unsigned=True), server_default="0", nullable=False),
        sa.Column("processed_count", mysql.INTEGER(unsigned=True), server_default="0", nullable=False),
        sa.Column("failed_count", mysql.INTEGER(unsigned=True), server_default="0", nullable=False),
        sa.Column("retry_count", mysql.INTEGER(unsigned=True), server_default="0", nullable=False),
        sa.Column("max_retries", mysql.INTEGER(unsigned=True), server_default="3", nullable=False),
        sa.Column("idempotency_key_hash", sa.BINARY(32), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("UTC_TIMESTAMP(6)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("UTC_TIMESTAMP(6)"), nullable=False),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.CheckConstraint(
            "task_status IN ('queued','running','succeeded','partial_failed','failed')",
            name="ai_memory_cleanup_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_ai_memory_cleanup_tasks"),
        sa.UniqueConstraint("task_no", name="uk_ai_memory_cleanup_tasks_no"),
        sa.UniqueConstraint(
            "user_id", "idempotency_key_hash", name="uk_ai_memory_cleanup_user_key"
        ),
    )
    op.create_index(
        "idx_ai_cleanup_user_time", "ai_memory_cleanup_tasks", ["user_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_table("ai_memory_cleanup_tasks")
