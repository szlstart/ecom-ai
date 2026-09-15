"""allow typed non-refund Agent action approvals

Revision ID: aq27b0c1d2e3
Revises: ap16a9b0c1d2
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "aq27b0c1d2e3"
down_revision = "ap16a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "ai_tool_approvals",
        "draft_id",
        existing_type=mysql.BIGINT(unsigned=True),
        nullable=True,
    )
    op.add_column("ai_tool_approvals", sa.Column("action_payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM ai_tool_actions WHERE approval_id IN "
            "(SELECT id FROM ai_tool_approvals WHERE draft_id IS NULL)"
        )
    )
    op.execute(sa.text("DELETE FROM ai_tool_approvals WHERE draft_id IS NULL"))
    op.drop_column("ai_tool_approvals", "action_payload")
    op.alter_column(
        "ai_tool_approvals",
        "draft_id",
        existing_type=mysql.BIGINT(unsigned=True),
        nullable=False,
    )
