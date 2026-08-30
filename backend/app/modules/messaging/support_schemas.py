from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.api.schemas import StrictRequest
from app.modules.messaging.schemas import ConversationContextView, MessageView

TicketStatus = Literal["queued", "assigned", "active", "waiting_user", "resolved", "closed"]


class SupportConversationItem(StrictRequest):
    conversation_id: str
    conversation_type: Literal["exclusive", "store"]
    participant_type: Literal["user", "merchant"]
    participant_id: str
    participant_name: str
    store_id: str | None
    conversation_status: Literal["active", "human_pending", "human_active", "closed"]
    last_message_preview: str | None
    last_message_at: datetime | None
    unread_count: int = Field(ge=0)
    requires_human: bool
    active_ticket_id: str | None
    active_ticket_status: TicketStatus | None
    assigned_user_id: str | None


class SupportConversationList(StrictRequest):
    items: list[SupportConversationItem]


class SupportTicketItem(StrictRequest):
    ticket_id: str
    conversation_id: str
    queue_type: Literal["store", "platform"]
    queue_code: str
    ticket_type: str
    priority: Literal["low", "normal", "high", "urgent"]
    ticket_status: TicketStatus
    assigned_user_id: str | None
    handoff_summary: str
    sla_due_at: datetime | None
    waiting_reason_code: str | None
    unread_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    version: int


class SupportTicketList(StrictRequest):
    items: list[SupportTicketItem]


class SupportTicketView(SupportTicketItem):
    handoff_message_refs: list[dict[str, object]]
    handoff_policy_version: str
    resolution_summary: str | None


class SupportUserSummary(StrictRequest):
    user_id: str
    nickname: str
    account_status: str


class SupportTicketEventView(StrictRequest):
    event_id: str
    event_type: str
    from_status: str | None
    to_status: str
    reason_code: str | None
    reason: str | None
    occurred_at: datetime


class SupportWorkspaceView(StrictRequest):
    ticket: SupportTicketView
    user: SupportUserSummary
    referenced_messages: list[MessageView]
    business_contexts: list[ConversationContextView]
    events: list[SupportTicketEventView]


class SupportInternalNoteView(StrictRequest):
    note_id: str
    author_user_id: str
    note_type: str
    text: str
    visibility_scope: str
    created_at: datetime


class SupportInternalNoteList(StrictRequest):
    items: list[SupportInternalNoteView]


class SupportReadCursorView(StrictRequest):
    conversation_id: str
    last_read_message_id: str
    last_read_sequence_no: int
    unread_count: int
    cursor_version: int


class SupportMessageRequest(StrictRequest):
    client_message_id: str = Field(pattern=r"^cmsg_[0-9A-Z]+$", max_length=40)
    text: str = Field(min_length=1, max_length=4000)


class SupportInternalNoteRequest(StrictRequest):
    text: str = Field(min_length=1, max_length=4000)
    note_type: Literal["handling", "transfer", "risk", "resolution"] = "handling"
    visibility_scope: Literal["current_queue", "supervisors", "platform_escalation"] = (
        "current_queue"
    )


class SupportWaitRequest(StrictRequest):
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=1, max_length=1000)


class SupportTransferRequest(StrictRequest):
    assigned_user_id: str = Field(min_length=3, max_length=40)
    reason: str = Field(min_length=2, max_length=500)


class SupportResolveRequest(StrictRequest):
    resolution_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    summary: str = Field(min_length=1, max_length=1000)
    internal_note: str | None = Field(default=None, max_length=1000)
