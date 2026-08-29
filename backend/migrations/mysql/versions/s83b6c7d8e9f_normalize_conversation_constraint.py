"""normalize the conversation scope constraint name

Revision ID: s83b6c7d8e9f
Revises: r72a5b6c7d8e
"""

import sqlalchemy as sa
from alembic import op

revision = "s83b6c7d8e9f"
down_revision = "r72a5b6c7d8e"
branch_labels = None
depends_on = None

SCOPE_EXPRESSION = (
    "(conversation_type = 'exclusive' AND store_id IS NULL) OR "
    "(conversation_type = 'store' AND store_id IS NOT NULL)"
)


def upgrade() -> None:
    # MySQL may select the composite store-time index as the supporting index
    # for the store_id foreign key. The downgrade-only guard below keeps old
    # l16's reversible chain valid; remove it when moving forward again.
    guard_exists = op.get_bind().scalar(
        sa.text(
            "SELECT 1 FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() "
            "AND table_name = 'human_service_internal_notes' "
            "AND index_name = 'idx_human_service_notes_store_fk_guard' LIMIT 1"
        )
    )
    if guard_exists:
        op.drop_index(
            "idx_human_service_notes_store_fk_guard",
            table_name="human_service_internal_notes",
        )
    op.drop_constraint(
        op.f("ck_conversations_ck_conversations_conversation_store_scope"),
        "conversations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_conversations_conversation_store_scope"),
        "conversations",
        SCOPE_EXPRESSION,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_conversations_conversation_store_scope"),
        "conversations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_conversations_ck_conversations_conversation_store_scope"),
        "conversations",
        SCOPE_EXPRESSION,
    )
    op.create_index(
        "idx_human_service_notes_store_fk_guard",
        "human_service_internal_notes",
        ["store_id"],
    )
