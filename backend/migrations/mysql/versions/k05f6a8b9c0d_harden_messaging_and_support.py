"""harden messaging and human support state

Revision ID: k05f6a8b9c0d
Revises: j94e5f7a8b9c
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "k05f6a8b9c0d"
down_revision: str | Sequence[str] | None = "j94e5f7a8b9c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("last_message_id", mysql.BIGINT(unsigned=True)))
    op.add_column("conversations", sa.Column("user_hidden_at", sa.DateTime()))
    op.add_column("conversations", sa.Column("human_ticket_id", mysql.BIGINT(unsigned=True)))
    op.create_index(
        "idx_conversations_store_status",
        "conversations",
        ["store_id", "conversation_status", "last_message_at"],
    )

    op.alter_column(
        "conversation_status_logs",
        "reason_code",
        new_column_name="event_type",
        existing_type=sa.String(64),
        existing_nullable=False,
    )
    op.add_column(
        "conversation_status_logs",
        sa.Column("actor_type", sa.String(16), server_default="system", nullable=False),
    )
    op.add_column("conversation_status_logs", sa.Column("actor_id", mysql.BIGINT(unsigned=True)))
    op.add_column("conversation_status_logs", sa.Column("ticket_id", mysql.BIGINT(unsigned=True)))
    op.add_column("conversation_status_logs", sa.Column("reason", sa.String(500)))
    op.add_column(
        "conversation_status_logs",
        sa.Column(
            "conversation_version",
            mysql.BIGINT(unsigned=True),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column("conversation_status_logs", sa.Column("trace_id", sa.String(64)))
    op.create_unique_constraint(
        "uk_conversation_status_event_version",
        "conversation_status_logs",
        ["conversation_id", "event_type", "conversation_version"],
    )
    op.create_index(
        "idx_conversation_status_logs_conversation",
        "conversation_status_logs",
        ["conversation_id", "created_at", "id"],
    )

    for column in (
        sa.Column("agent_version_id", mysql.BIGINT(unsigned=True)),
        sa.Column("reply_to_message_id", mysql.BIGINT(unsigned=True)),
        sa.Column("ai_run_no", sa.String(40)),
        sa.Column("recalled_at", sa.DateTime()),
    ):
        op.add_column("messages", column)
    op.create_foreign_key(
        "fk_messages_reply_to_message_id_messages",
        "messages",
        "messages",
        ["reply_to_message_id"],
        ["id"],
    )
    op.create_index("idx_messages_ai_run", "messages", ["ai_run_no"])

    op.add_column("human_service_tickets", sa.Column("user_id", mysql.BIGINT(unsigned=True)))
    op.add_column("human_service_tickets", sa.Column("store_id", mysql.BIGINT(unsigned=True)))
    op.add_column("human_service_tickets", sa.Column("queue_code", sa.String(64)))
    op.add_column(
        "human_service_tickets",
        sa.Column("ticket_type", sa.String(32), server_default="general", nullable=False),
    )
    op.add_column(
        "human_service_tickets",
        sa.Column("priority", sa.String(16), server_default="normal", nullable=False),
    )
    op.add_column("human_service_tickets", sa.Column("handoff_message_refs", sa.JSON()))
    op.add_column("human_service_tickets", sa.Column("handoff_policy_version", sa.String(40)))
    op.add_column(
        "human_service_tickets",
        sa.Column("source", sa.String(16), server_default="user", nullable=False),
    )
    op.add_column("human_service_tickets", sa.Column("sla_due_at", sa.DateTime()))
    op.add_column("human_service_tickets", sa.Column("waiting_started_at", sa.DateTime()))
    op.add_column("human_service_tickets", sa.Column("sla_remaining_seconds", sa.Integer()))
    op.add_column("human_service_tickets", sa.Column("waiting_reason_code", sa.String(64)))
    op.add_column("human_service_tickets", sa.Column("resolved_at", sa.DateTime()))
    op.add_column("human_service_tickets", sa.Column("closed_at", sa.DateTime()))
    op.add_column("human_service_tickets", sa.Column("resolution_code", sa.String(64)))
    op.add_column("human_service_tickets", sa.Column("resolution_note", sa.Text()))
    op.execute(
        "UPDATE human_service_tickets t JOIN conversations c ON c.id=t.conversation_id "
        "SET t.user_id=c.user_id, t.store_id=c.store_id, "
        "t.queue_code=CASE WHEN t.queue_type='platform' THEN 'platform.general' "
        "ELSE CONCAT('store.', c.store_id, '.general') END, "
        "t.handoff_summary=COALESCE(t.handoff_summary, '用户请求人工服务'), "
        "t.handoff_message_refs=JSON_ARRAY(), t.handoff_policy_version='handoff-v1'"
    )
    op.alter_column(
        "human_service_tickets",
        "assigned_user_id",
        new_column_name="current_assignee_user_id",
        existing_type=mysql.BIGINT(unsigned=True),
    )
    for name, type_ in (
        ("user_id", mysql.BIGINT(unsigned=True)),
        ("queue_code", sa.String(64)),
        ("handoff_summary", sa.Text()),
        ("handoff_message_refs", sa.JSON()),
        ("handoff_policy_version", sa.String(40)),
    ):
        op.alter_column("human_service_tickets", name, existing_type=type_, nullable=False)
    op.create_foreign_key(
        "fk_human_service_tickets_user_id_users",
        "human_service_tickets",
        "users",
        ["user_id"],
        ["id"],
    )
    op.drop_index("idx_human_service_tickets_queue", table_name="human_service_tickets")
    op.create_index(
        "idx_human_service_tickets_queue",
        "human_service_tickets",
        ["queue_code", "ticket_status", "created_at", "id"],
    )
    op.create_index(
        "idx_human_service_tickets_assignee",
        "human_service_tickets",
        ["current_assignee_user_id", "ticket_status", "updated_at"],
    )

    _replace_internal_note_plaintext_storage()
    _create_assignment_table()
    _create_ticket_event_table()


def _replace_internal_note_plaintext_storage() -> None:
    count = int(
        op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM human_service_internal_notes")) or 0
    )
    if count:
        raise RuntimeError(
            "human_service_internal_notes contains legacy plaintext; migrate it with the "
            "application encryption key before applying k05f6a8b9c0d"
        )
    op.add_column("human_service_internal_notes", sa.Column("note_no", sa.String(40)))
    op.add_column(
        "human_service_internal_notes", sa.Column("store_id", mysql.BIGINT(unsigned=True))
    )
    op.add_column("human_service_internal_notes", sa.Column("note_type", sa.String(16)))
    op.add_column(
        "human_service_internal_notes", sa.Column("content_ciphertext", mysql.VARBINARY(4096))
    )
    op.add_column("human_service_internal_notes", sa.Column("content_hash", mysql.BINARY(32)))
    op.add_column("human_service_internal_notes", sa.Column("visibility_scope", sa.String(32)))
    op.add_column("human_service_internal_notes", sa.Column("key_version", sa.SmallInteger()))
    op.add_column("human_service_internal_notes", sa.Column("request_id", sa.String(64)))
    op.add_column("human_service_internal_notes", sa.Column("trace_id", sa.String(64)))
    op.drop_column("human_service_internal_notes", "note_text")
    for name, type_ in (
        ("note_no", sa.String(40)),
        ("note_type", sa.String(16)),
        ("content_ciphertext", mysql.VARBINARY(4096)),
        ("content_hash", mysql.BINARY(32)),
        ("visibility_scope", sa.String(32)),
        ("key_version", sa.SmallInteger()),
        ("request_id", sa.String(64)),
        ("trace_id", sa.String(64)),
    ):
        op.alter_column("human_service_internal_notes", name, existing_type=type_, nullable=False)
    op.create_unique_constraint(
        "uk_human_service_internal_notes_note_no", "human_service_internal_notes", ["note_no"]
    )


def _create_assignment_table() -> None:
    op.create_table(
        "human_service_assignments",
        sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("assignee_user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("assignment_type", sa.String(16), nullable=False),
        sa.Column("assigned_by_type", sa.String(16), nullable=False),
        sa.Column("assigned_by_id", mysql.BIGINT(unsigned=True)),
        sa.Column("assignment_status", sa.String(16), nullable=False),
        sa.Column(
            "active_ticket_key",
            mysql.BIGINT(unsigned=True),
            sa.Computed(
                "CASE WHEN assignment_status IN ('assigned', 'accepted') "
                "THEN ticket_id ELSE NULL END"
            ),
        ),
        sa.Column("assigned_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime()),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("end_reason", sa.String(64)),
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["human_service_tickets.id"]),
        sa.ForeignKeyConstraint(["assignee_user_id"], ["users.id"]),
        sa.UniqueConstraint("active_ticket_key", name="uk_human_service_assignments_active"),
        sa.Index("idx_human_service_assignments_ticket", "ticket_id", "assigned_at", "id"),
    )


def _create_ticket_event_table() -> None:
    op.create_table(
        "human_service_ticket_events",
        sa.Column("event_no", sa.String(40), nullable=False),
        sa.Column("ticket_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_user_id", mysql.BIGINT(unsigned=True)),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("reason", sa.String(1000)),
        sa.Column("sla_due_at_before", sa.DateTime()),
        sa.Column("sla_due_at_after", sa.DateTime()),
        sa.Column("ticket_version", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.ForeignKeyConstraint(["ticket_id"], ["human_service_tickets.id"]),
        sa.UniqueConstraint("event_no", name="uk_human_service_ticket_events_no"),
        sa.Index("idx_human_ticket_events_ticket", "ticket_id", "created_at", "id"),
    )


def downgrade() -> None:
    op.drop_table("human_service_ticket_events")
    op.drop_table("human_service_assignments")
    raise RuntimeError(
        "k05f6a8b9c0d removes plaintext internal-note storage and requires "
        "a reviewed restore migration"
    )
