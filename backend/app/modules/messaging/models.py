from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY, VARBINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import (
    AppendOnlyMySQLModel,
    MutableMySQLModel,
    MySQLBase,
    SoftDeleteMySQLModel,
)


class Conversation(SoftDeleteMySQLModel, MySQLBase):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("conversation_no", name="uk_conversations_no"),
        UniqueConstraint("exclusive_user_key", name="uk_conversations_exclusive_user"),
        UniqueConstraint("store_user_key", "store_id", name="uk_conversations_store_user"),
        CheckConstraint(
            "(conversation_type = 'exclusive' AND store_id IS NULL) OR "
            "(conversation_type = 'store' AND store_id IS NOT NULL)",
            name="conversation_store_scope",
        ),
        Index("idx_conversations_user_updated", "user_id", "updated_at", "id"),
        Index(
            "idx_conversations_user_visibility_updated",
            "user_id",
            "user_hidden_at",
            "updated_at",
            "id",
        ),
        Index(
            "idx_conversations_store_status", "store_id", "conversation_status", "last_message_at"
        ),
    )

    conversation_no: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    store_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("stores.id"))
    conversation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    is_fixed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclusive_user_key: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        Computed(
            "CASE WHEN conversation_type = 'exclusive' "
            "AND deleted_at IS NULL THEN user_id ELSE NULL END"
        ),
    )
    store_user_key: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        Computed(
            "CASE WHEN conversation_type = 'store' AND deleted_at IS NULL "
            "THEN user_id ELSE NULL END"
        ),
    )
    conversation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    last_sequence_no: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_message_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "messages.id",
            name="fk_conversations_last_message_id_messages",
            use_alter=True,
        ),
    )
    user_hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    human_ticket_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "human_service_tickets.id",
            name="fk_conversations_human_ticket_id_human_service_tickets",
            use_alter=True,
        ),
    )


class ConversationStatusLog(MutableMySQLModel, MySQLBase):
    __tablename__ = "conversation_status_logs"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "event_type",
            "conversation_version",
            name="uk_conversation_status_event_version",
        ),
        Index("idx_conversation_status_logs_conversation", "conversation_id", "created_at", "id"),
    )

    conversation_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("conversations.id"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    ticket_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    reason: Mapped[str | None] = mapped_column(String(500))
    conversation_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))


class Message(MutableMySQLModel, MySQLBase):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("message_no", name="uk_messages_no"),
        UniqueConstraint("conversation_id", "sequence_no", name="uk_messages_sequence"),
        UniqueConstraint("conversation_id", "client_message_no", name="uk_messages_client_no"),
        Index("idx_messages_conversation_timeline", "conversation_id", "sequence_no"),
        Index("idx_messages_ai_run", "ai_run_no"),
    )

    message_no: Mapped[str] = mapped_column(String(40), nullable=False)
    conversation_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("conversations.id"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    client_message_no: Mapped[str | None] = mapped_column(String(40))
    sender_type: Mapped[str] = mapped_column(String(16), nullable=False)
    sender_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    text_content: Mapped[str | None] = mapped_column(Text)
    content_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)
    agent_version_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    reply_to_message_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("messages.id")
    )
    ai_run_no: Mapped[str | None] = mapped_column(String(40))
    message_status: Mapped[str] = mapped_column(String(16), nullable=False, default="sent")
    moderation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="passed")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    recalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class MessageRead(MutableMySQLModel, MySQLBase):
    __tablename__ = "message_reads"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "reader_type", "reader_id", name="uk_message_reads_reader"
        ),
    )

    conversation_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("conversations.id"), nullable=False
    )
    reader_type: Mapped[str] = mapped_column(String(16), nullable=False)
    reader_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    last_read_message_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("messages.id"), nullable=False
    )
    last_read_sequence_no: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    last_read_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class HumanServiceTicket(MutableMySQLModel, MySQLBase):
    __tablename__ = "human_service_tickets"
    __table_args__ = (
        UniqueConstraint("ticket_no", name="uk_human_service_tickets_no"),
        UniqueConstraint("conversation_id", "active_key", name="uk_human_service_tickets_active"),
        Index(
            "idx_human_service_tickets_queue",
            "queue_code",
            "ticket_status",
            "priority",
            "created_at",
            "id",
        ),
        Index(
            "idx_human_service_tickets_assignee",
            "current_assignee_user_id",
            "ticket_status",
            "updated_at",
        ),
    )

    ticket_no: Mapped[str] = mapped_column(String(40), nullable=False)
    conversation_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("conversations.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    store_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("stores.id"))
    queue_type: Mapped[str] = mapped_column(String(16), nullable=False)
    queue_code: Mapped[str] = mapped_column(String(64), nullable=False)
    ticket_type: Mapped[str] = mapped_column(String(32), nullable=False, default="general")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    ticket_status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    current_assignee_user_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id")
    )
    active_key: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    handoff_summary: Mapped[str] = mapped_column(Text, nullable=False)
    handoff_message_refs: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    handoff_policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    waiting_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    sla_remaining_seconds: Mapped[int | None] = mapped_column(Integer)
    waiting_reason_code: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    resolution_code: Mapped[str | None] = mapped_column(String(64))
    resolution_summary: Mapped[str | None] = mapped_column(Text)
    resolution_note: Mapped[str | None] = mapped_column(Text)


class HumanServiceAssignment(MutableMySQLModel, MySQLBase):
    __tablename__ = "human_service_assignments"
    __table_args__ = (
        UniqueConstraint("active_ticket_key", name="uk_human_service_assignments_active"),
        Index("idx_human_service_assignments_ticket", "ticket_id", "assigned_at", "id"),
    )

    ticket_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("human_service_tickets.id"), nullable=False
    )
    assignee_user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    assignment_type: Mapped[str] = mapped_column(String(16), nullable=False)
    assigned_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    assigned_by_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    assignment_status: Mapped[str] = mapped_column(String(16), nullable=False)
    active_ticket_key: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        Computed(
            "CASE WHEN assignment_status IN ('assigned', 'accepted') THEN ticket_id ELSE NULL END"
        ),
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    end_reason: Mapped[str | None] = mapped_column(String(64))


class HumanServiceInternalNote(MutableMySQLModel, MySQLBase):
    __tablename__ = "human_service_internal_notes"
    __table_args__ = (
        Index("uk_human_service_internal_notes_note_no", "note_no", unique=True),
        Index("idx_human_service_notes_ticket_time", "ticket_id", "created_at", "id"),
        Index("idx_human_service_notes_store_time", "store_id", "created_at", "id"),
    )

    ticket_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("human_service_tickets.id"), nullable=False
    )
    author_user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    note_no: Mapped[str] = mapped_column(String(40), nullable=False)
    store_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("stores.id"))
    note_type: Mapped[str] = mapped_column(String(16), nullable=False)
    content_ciphertext: Mapped[bytes] = mapped_column(VARBINARY(4096), nullable=False)
    content_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    visibility_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)


class HumanServiceTicketEvent(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "human_service_ticket_events"
    __table_args__ = (
        UniqueConstraint("event_no", name="uk_human_service_ticket_events_no"),
        Index("idx_human_ticket_events_ticket", "ticket_id", "created_at", "id"),
    )

    event_no: Mapped[str] = mapped_column(String(40), nullable=False)
    ticket_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("human_service_tickets.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(1000))
    sla_due_at_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    sla_due_at_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ticket_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)


class ConversationContext(MutableMySQLModel, MySQLBase):
    __tablename__ = "conversation_contexts"
    __table_args__ = (
        UniqueConstraint("context_no", name="uk_conversation_contexts_no"),
        UniqueConstraint("active_context_key", name="uk_conversation_contexts_active"),
        Index(
            "idx_contexts_conversation_status", "conversation_id", "context_status", "created_at"
        ),
    )

    context_no: Mapped[str] = mapped_column(String(40), nullable=False)
    conversation_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("conversations.id"), nullable=False
    )
    context_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_no: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_version: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    context_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    context_status: Mapped[str] = mapped_column(String(16), nullable=False)
    active_context_key: Mapped[str | None] = mapped_column(String(96))
    display_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
