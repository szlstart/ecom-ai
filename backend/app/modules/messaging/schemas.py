from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from app.api.schemas import StrictRequest


class ConversationView(StrictRequest):
    conversation_id: str
    conversation_type: Literal["exclusive", "store"]
    conversation_status: Literal["active", "human_pending", "human_active", "closed"]
    store_id: str | None
    title: str
    is_fixed: bool
    fixed_rank: int | None
    last_message_preview: str | None
    last_message_at: datetime | None
    last_sequence_no: int
    unread_count: int
    version: int
    active_contexts: list[ConversationContextView] = Field(default_factory=list)


class ConversationList(StrictRequest):
    items: list[ConversationView]


class ConversationArchiveView(StrictRequest):
    conversation_id: str
    archived_at: datetime
    version: int


class ConversationDeletionView(StrictRequest):
    conversation_id: str
    deleted_at: datetime
    memory_cleared: bool


class TextMessageContent(StrictRequest):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=4000)


class ProductCardMessageContent(StrictRequest):
    type: Literal["product_card"] = "product_card"
    product_id: str = Field(pattern=r"^prd_[0-9A-Z]+$", max_length=40)
    sku_id: str | None = Field(default=None, pattern=r"^sku_[0-9A-Z]+$", max_length=40)


class OrderCardMessageContent(StrictRequest):
    type: Literal["order_card"] = "order_card"
    order_id: str = Field(pattern=r"^ord_[0-9A-Z]+$", max_length=40)


MessageContent = Annotated[
    TextMessageContent | ProductCardMessageContent | OrderCardMessageContent,
    Field(discriminator="type"),
]


class MessageCreateRequest(StrictRequest):
    client_message_id: str = Field(pattern=r"^cmsg_[0-9A-Z]+$", max_length=40)
    content: MessageContent


class MessageView(StrictRequest):
    message_id: str
    sequence_no: int
    sender_type: Literal["user", "agent", "human", "system", "tool"]
    message_type: str
    text: str | None
    message_status: str
    moderation_status: str
    content: dict[str, object] | None
    viewer_reaction: Literal["thumb_up", "thumb_down"] | None = None
    sent_at: datetime


class MessageList(StrictRequest):
    items: list[MessageView]
    previous_cursor: str | None = None


class ResolutionCheckResponseRequest(StrictRequest):
    client_message_id: str = Field(pattern=r"^cmsg_[0-9A-Z]+$", max_length=40)
    resolved: bool


class ConversationContextRequest(StrictRequest):
    resource_id: str = Field(min_length=4, max_length=64)
    resource_version: int | None = Field(default=None, ge=0)


class ConversationContextView(StrictRequest):
    context_id: str
    context_type: Literal["product", "order", "shipment", "refund", "store", "checkout_store_group"]
    resource_id: str
    resource_version: int | None
    context_version: int
    status: Literal["active", "inactive", "expired"]
    display_snapshot: dict[str, object]
    expires_at: datetime | None


class ConversationContextClearView(StrictRequest):
    conversation_id: str
    context_type: str
    cleared: bool
    version: int


class ReadCursorRequest(StrictRequest):
    last_read_message_id: str = Field(pattern=r"^msg_[0-9A-Z]+$", max_length=40)
    last_read_sequence_no: int = Field(gt=0)


class ReadCursorView(StrictRequest):
    conversation_id: str
    last_read_message_id: str
    last_read_sequence_no: int
    unread_count: int
    total_unread_count: int
    cursor_version: int
