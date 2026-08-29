"""add durable account deletion saga

Revision ID: ai49f2a3b4c5
Revises: ah38e1f2a3b4
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "ai49f2a3b4c5"
down_revision = "ah38e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_deletion_tasks",
        sa.Column("task_no", sa.String(length=40), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("user_no", sa.String(length=40), nullable=False),
        sa.Column("store_nos", mysql.JSON(), nullable=False),
        sa.Column("task_status", sa.String(length=24), nullable=False),
        sa.Column("current_phase", sa.String(length=32), nullable=False),
        sa.Column("inventory", mysql.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="12", nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
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
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_account_deletion_tasks"),
        sa.UniqueConstraint("task_no", name="uk_account_deletion_tasks_no"),
        sa.UniqueConstraint("user_no", name="uk_account_deletion_tasks_user"),
    )
    op.create_index(
        "idx_account_deletion_tasks_delivery",
        "account_deletion_tasks",
        ["task_status", "available_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_account_deletion_tasks_delivery", table_name="account_deletion_tasks"
    )
    op.drop_table("account_deletion_tasks")
