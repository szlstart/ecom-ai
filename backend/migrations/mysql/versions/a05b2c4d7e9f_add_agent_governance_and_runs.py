"""add agent definitions versions runs and tool audits

Revision ID: a05b2c4d7e9f
Revises: 9e4a1b3c6d8f
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "a05b2c4d7e9f"
down_revision: str | Sequence[str] | None = "9e4a1b3c6d8f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _mutable_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "ai_agent_definitions",
        sa.Column("agent_no", sa.String(40), nullable=False),
        sa.Column("agent_code", sa.String(64), nullable=False),
        sa.Column("agent_type", sa.String(16), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("agent_status", sa.String(16), nullable=False),
        *_mutable_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_ai_agent_definitions"),
        sa.UniqueConstraint("agent_no", name="uk_ai_agent_definitions_no"),
        sa.UniqueConstraint("agent_code", name="uq_ai_agent_definitions_agent_code"),
    )
    op.create_table(
        "ai_agent_versions",
        sa.Column("agent_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("version_no", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("version_status", sa.String(16), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("model_profile", sa.String(64), nullable=False),
        sa.Column("tool_allowlist", sa.JSON(), nullable=False),
        sa.Column("policy_config", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        *_mutable_columns(),
        sa.ForeignKeyConstraint(["agent_id"], ["ai_agent_definitions.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_ai_agent_versions"),
        sa.UniqueConstraint("agent_id", "version_no", name="uk_ai_agent_versions_number"),
    )
    op.create_index(
        "idx_agent_versions_status",
        "ai_agent_versions",
        ["agent_id", "version_status", "version_no"],
    )
    op.create_table(
        "ai_agent_runs",
        sa.Column("run_no", sa.String(40), nullable=False),
        sa.Column("conversation_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("trigger_message_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("response_message_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("agent_version_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("run_status", sa.String(16), nullable=False),
        sa.Column("current_phase", sa.String(32), nullable=False),
        sa.Column("public_output", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("context_no", sa.String(40), nullable=True),
        sa.Column("context_version", mysql.BIGINT(unsigned=True), nullable=True),
        *_mutable_columns(),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["trigger_message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["response_message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["agent_version_id"], ["ai_agent_versions.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_ai_agent_runs"),
        sa.UniqueConstraint("run_no", name="uk_ai_agent_runs_no"),
        sa.UniqueConstraint("trigger_message_id", name="uk_ai_agent_runs_trigger_message"),
    )
    op.create_index(
        "idx_agent_runs_conversation_time", "ai_agent_runs", ["conversation_id", "created_at", "id"]
    )
    op.create_table(
        "ai_agent_tool_audits",
        sa.Column("audit_no", sa.String(40), nullable=False),
        sa.Column("run_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("tool_code", sa.String(128), nullable=False),
        sa.Column("scope_snapshot", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["ai_agent_runs.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_ai_agent_tool_audits"),
        sa.UniqueConstraint("audit_no", name="uk_ai_agent_tool_audits_no"),
    )


def downgrade() -> None:
    op.drop_table("ai_agent_tool_audits")
    op.drop_table("ai_agent_runs")
    op.drop_table("ai_agent_versions")
    op.drop_table("ai_agent_definitions")
