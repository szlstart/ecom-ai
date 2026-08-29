"""add durable multi-agent delegation ledger

Revision ID: q61f4a5b6c7d
Revises: p50e3f4a5b6c
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "q61f4a5b6c7d"
down_revision = "p50e3f4a5b6c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_agent_delegations",
        sa.Column("delegation_no", sa.String(40), nullable=False),
        sa.Column("run_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("subtask_key", sa.String(128), nullable=False),
        sa.Column("specialist_code", sa.String(64), nullable=False),
        sa.Column("specialist_version", sa.String(64), nullable=False),
        sa.Column("fingerprint", mysql.BINARY(32), nullable=False),
        sa.Column("depth", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("delegation_status", sa.String(16), nullable=False),
        sa.Column("objective_hash", mysql.BINARY(32), nullable=False),
        sa.Column("scope_snapshot", sa.JSON(), nullable=False),
        sa.Column("resource_refs", sa.JSON(), nullable=False),
        sa.Column("dependency_nos", sa.JSON(), nullable=False),
        sa.Column("allowed_tools_snapshot", sa.JSON(), nullable=False),
        sa.Column("budget_snapshot", sa.JSON(), nullable=False),
        sa.Column("result_snapshot", sa.JSON()),
        sa.Column(
            "tokens_used", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"
        ),
        sa.Column("tool_calls", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("model_calls", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64)),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("span_id", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column(
            "id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True
        ),
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
        sa.ForeignKeyConstraint(["run_id"], ["ai_agent_runs.id"]),
        sa.UniqueConstraint("delegation_no", name="uk_ai_agent_delegations_no"),
        sa.UniqueConstraint("fingerprint", name="uk_ai_agent_delegations_fingerprint"),
        sa.UniqueConstraint(
            "run_id",
            "subtask_key",
            "specialist_version",
            name="uk_ai_agent_delegations_run_subtask_version",
        ),
        sa.CheckConstraint("depth = 1", name="agent_delegation_depth"),
        sa.CheckConstraint(
            "delegation_status IN "
            "('queued','running','succeeded','partial','failed','denied','unknown','cancelled')",
            name="agent_delegation_status",
        ),
    )
    op.create_index(
        "idx_ai_agent_delegations_run_status",
        "ai_agent_delegations",
        ["run_id", "delegation_status", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_ai_agent_delegations_run_status", table_name="ai_agent_delegations")
    op.drop_table("ai_agent_delegations")
