from datetime import datetime
from typing import Literal

from pydantic import Field

from app.api.schemas import StrictRequest


class HumanTicketView(StrictRequest):
    ticket_id: str
    conversation_id: str
    queue_type: Literal["store", "platform"]
    ticket_status: Literal["queued", "assigned", "active", "waiting_user", "resolved", "closed"]
    assigned_user_id: str | None
    resolution_summary: str | None = None
    queue_position: int | None = None
    estimated_response_at: datetime | None = None
    can_cancel: bool = False


class HumanHandoffRequest(StrictRequest):
    ticket_type: Literal["general", "order", "logistics", "refund", "complaint"] = "general"
    summary: str = Field(min_length=1, max_length=2000)
    message_refs: list[str] = Field(default_factory=list, max_length=10)


class HumanMessageCreateRequest(StrictRequest):
    text: str = Field(min_length=1, max_length=4000)


class HumanTicketResolutionRequest(StrictRequest):
    resolution_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    summary: str = Field(min_length=1, max_length=1000)
