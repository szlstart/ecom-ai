"""normalize conversation scope check name

Revision ID: m27b8c0d1e2f
Revises: l16a7b9c0d1e
"""

from collections.abc import Sequence

from alembic import op

revision: str = "m27b8c0d1e2f"
down_revision: str | Sequence[str] | None = "l16a7b9c0d1e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("conversation_store_scope", "conversations", type_="check")
    op.create_check_constraint(
        "ck_conversations_conversation_store_scope",
        "conversations",
        "(conversation_type = 'exclusive' AND store_id IS NULL) OR "
        "(conversation_type = 'store' AND store_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_conversations_conversation_store_scope", "conversations", type_="check"
    )
    op.create_check_constraint(
        "conversation_store_scope",
        "conversations",
        "(conversation_type = 'exclusive' AND store_id IS NULL) OR "
        "(conversation_type = 'store' AND store_id IS NOT NULL)",
    )
