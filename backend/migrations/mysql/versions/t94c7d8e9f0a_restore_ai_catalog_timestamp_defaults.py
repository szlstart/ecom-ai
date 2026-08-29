"""restore AI catalog timestamp defaults

Revision ID: t94c7d8e9f0a
Revises: s83b6c7d8e9f
"""

from alembic import op

revision = "t94c7d8e9f0a"
down_revision = "s83b6c7d8e9f"
branch_labels = None
depends_on = None

TABLES = (
    "ai_skill_definitions",
    "ai_skill_versions",
    "ai_tool_definitions",
    "ai_tool_versions",
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(
            f"""ALTER TABLE {table}
            MODIFY created_at DATETIME NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
            MODIFY updated_at DATETIME NOT NULL DEFAULT (UTC_TIMESTAMP(6))"""
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(
            f"""ALTER TABLE {table}
            MODIFY created_at DATETIME NOT NULL,
            MODIFY updated_at DATETIME NOT NULL"""
        )
