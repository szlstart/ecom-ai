"""add immutable skill bindings and AI kill switches

Revision ID: p50e3f4a5b6c
Revises: o49d2e3f4a5b
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "p50e3f4a5b6c"
down_revision = "o49d2e3f4a5b"
branch_labels = None
depends_on = None


def _mutable() -> list[sa.Column[object]]:
    return [
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), nullable=False, server_default="0"),
    ]


def upgrade() -> None:
    op.drop_column("ai_tool_definitions", "output_schema")
    op.drop_column("ai_tool_definitions", "input_schema")
    op.add_column(
        "ai_skill_versions",
        sa.Column("evaluation_report", sa.JSON(), nullable=False, server_default=sa.text("('{}')")),
    )
    op.create_table(
        "ai_agent_skill_bindings",
        sa.Column("agent_version_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("skill_version_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("binding_status", sa.String(16), nullable=False),
        *_mutable(),
        sa.ForeignKeyConstraint(["agent_version_id"], ["ai_agent_versions.id"]),
        sa.ForeignKeyConstraint(["skill_version_id"], ["ai_skill_versions.id"]),
        sa.UniqueConstraint(
            "agent_version_id", "skill_version_id", name="uk_ai_agent_skill_binding"
        ),
    )
    op.create_table(
        "ai_skill_tool_bindings",
        sa.Column("skill_version_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("tool_version_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("permission_effect", sa.String(16), nullable=False),
        sa.Column("confirmation_policy", sa.String(24), nullable=False),
        sa.Column("call_budget", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("timeout_ms", mysql.INTEGER(unsigned=True), nullable=False),
        *_mutable(),
        sa.ForeignKeyConstraint(["skill_version_id"], ["ai_skill_versions.id"]),
        sa.ForeignKeyConstraint(["tool_version_id"], ["ai_tool_versions.id"]),
        sa.UniqueConstraint(
            "skill_version_id", "tool_version_id", name="uk_ai_skill_tool_binding"
        ),
        sa.CheckConstraint("permission_effect IN ('allow','deny')", name="skill_tool_effect"),
    )
    op.create_table(
        "ai_runtime_kill_switches",
        sa.Column("switch_no", sa.String(40), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_code", sa.String(128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("reason", sa.String(500)),
        sa.Column("changed_by", mysql.BIGINT(unsigned=True), nullable=False),
        *_mutable(),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"]),
        sa.UniqueConstraint("switch_no", name="uk_ai_runtime_kill_switch_no"),
        sa.UniqueConstraint("target_type", "target_code", name="uk_ai_runtime_kill_target"),
        sa.CheckConstraint(
            "target_type IN ('agent','skill','tool','mcp_server')", name="ai_kill_target_type"
        ),
    )


def downgrade() -> None:
    op.drop_table("ai_runtime_kill_switches")
    op.drop_table("ai_skill_tool_bindings")
    op.drop_table("ai_agent_skill_bindings")
    op.drop_column("ai_skill_versions", "evaluation_report")
    op.add_column("ai_tool_definitions", sa.Column("input_schema", sa.JSON(), nullable=True))
    op.add_column("ai_tool_definitions", sa.Column("output_schema", sa.JSON(), nullable=True))
