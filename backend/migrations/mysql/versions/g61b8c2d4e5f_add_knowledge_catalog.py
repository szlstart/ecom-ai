"""add knowledge and tool catalog

Revision ID: g61b8c2d4e5f
Revises: f50a7b9c3d4e
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "g61b8c2d4e5f"
down_revision = "f50a7b9c3d4e"
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
    op.create_table(
        "ai_skill_definitions",
        sa.Column("skill_no", sa.String(40), nullable=False),
        sa.Column("skill_code", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("skill_status", sa.String(16), nullable=False),
        *_mutable(),
        sa.UniqueConstraint("skill_no", name="uk_ai_skill_definitions_no"),
        sa.UniqueConstraint("skill_code", name="uq_ai_skill_definitions_code"),
    )
    op.create_table(
        "ai_skill_versions",
        sa.Column("skill_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("version_no", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("version_status", sa.String(16), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime()),
        *_mutable(),
        sa.ForeignKeyConstraint(["skill_id"], ["ai_skill_definitions.id"]),
        sa.UniqueConstraint("skill_id", "version_no", name="uk_ai_skill_versions_number"),
    )
    op.create_table(
        "ai_tool_definitions",
        sa.Column("tool_code", sa.String(128), nullable=False),
        sa.Column("server_code", sa.String(64), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=False),
        sa.Column("tool_status", sa.String(16), nullable=False),
        *_mutable(),
        sa.UniqueConstraint("tool_code", name="uk_ai_tool_definitions_code"),
    )
    op.create_table(
        "ai_tool_versions",
        sa.Column("tool_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("version_no", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("version_status", sa.String(16), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=False),
        sa.Column("evaluation_report", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime()),
        *_mutable(),
        sa.ForeignKeyConstraint(["tool_id"], ["ai_tool_definitions.id"]),
        sa.UniqueConstraint("tool_id", "version_no", name="uk_ai_tool_versions_number"),
    )
    op.create_index(
        "idx_ai_tool_versions_status",
        "ai_tool_versions",
        ["tool_id", "version_status", "version_no"],
    )
    op.create_table(
        "knowledge_documents",
        sa.Column("document_no", sa.String(40), nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_no", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("safe_text", sa.Text(), nullable=False),
        sa.Column("document_status", sa.String(16), nullable=False),
        sa.Column("content_version", sa.String(40), nullable=False),
        *_mutable(),
        sa.UniqueConstraint("document_no", name="uk_knowledge_documents_no"),
        sa.Index("idx_knowledge_documents_scope", "scope_type", "scope_no", "document_status"),
    )


def downgrade() -> None:
    for table in (
        "knowledge_documents",
        "ai_tool_versions",
        "ai_tool_definitions",
        "ai_skill_versions",
        "ai_skill_definitions",
    ):
        op.execute(sa.text(f"DROP TABLE IF EXISTS `{table}`"))
