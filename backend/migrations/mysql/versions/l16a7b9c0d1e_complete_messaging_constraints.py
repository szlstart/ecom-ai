"""complete messaging constraints and queue indexes

Revision ID: l16a7b9c0d1e
Revises: k05f6a8b9c0d
"""

from collections.abc import Sequence

from alembic import op

revision: str = "l16a7b9c0d1e"
down_revision: str | Sequence[str] | None = "k05f6a8b9c0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "idx_conversations_user_visibility_updated",
        "conversations",
        ["user_id", "user_hidden_at", "updated_at", "id"],
    )
    op.create_check_constraint(
        "conversation_store_scope",
        "conversations",
        "(conversation_type = 'exclusive' AND store_id IS NULL) OR "
        "(conversation_type = 'store' AND store_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_conversations_store_id_stores",
        "conversations",
        "stores",
        ["store_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_conversations_last_message_id_messages",
        "conversations",
        "messages",
        ["last_message_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_conversations_human_ticket_id_human_service_tickets",
        "conversations",
        "human_service_tickets",
        ["human_ticket_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_human_service_tickets_store_id_stores",
        "human_service_tickets",
        "stores",
        ["store_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_human_service_internal_notes_store_id_stores",
        "human_service_internal_notes",
        "stores",
        ["store_id"],
        ["id"],
    )
    op.drop_index("idx_human_service_tickets_queue", table_name="human_service_tickets")
    op.create_index(
        "idx_human_service_tickets_queue",
        "human_service_tickets",
        ["queue_code", "ticket_status", "priority", "created_at", "id"],
    )
    op.create_index(
        "idx_human_service_notes_store_time",
        "human_service_internal_notes",
        ["store_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_human_service_notes_store_time", table_name="human_service_internal_notes")
    op.drop_constraint(
        "fk_human_service_internal_notes_store_id_stores",
        "human_service_internal_notes",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_human_service_tickets_store_id_stores",
        "human_service_tickets",
        type_="foreignkey",
    )
    op.drop_index("idx_human_service_tickets_queue", table_name="human_service_tickets")
    op.create_index(
        "idx_human_service_tickets_queue",
        "human_service_tickets",
        ["queue_code", "ticket_status", "created_at", "id"],
    )
    op.drop_constraint(
        "fk_conversations_human_ticket_id_human_service_tickets",
        "conversations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_conversations_last_message_id_messages", "conversations", type_="foreignkey"
    )
    op.drop_constraint("fk_conversations_store_id_stores", "conversations", type_="foreignkey")
    op.drop_constraint("conversation_store_scope", "conversations", type_="check")
    op.drop_index("idx_conversations_user_visibility_updated", table_name="conversations")
