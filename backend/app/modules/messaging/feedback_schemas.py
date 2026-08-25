from datetime import datetime
from typing import Literal

from pydantic import Field

from app.api.schemas import StrictRequest


class AiReactionRequest(StrictRequest):
    reaction: Literal["thumb_up", "thumb_down"]


class AiFeedbackDetailRequest(StrictRequest):
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    comment: str = Field(min_length=2, max_length=2000)


class AiFeedbackView(StrictRequest):
    feedback_id: str | None
    message_id: str
    feedback_type: Literal["thumb_up", "thumb_down", "report", "correction"] | None
    status: Literal["submitted", "withdrawn", "reviewed", "resolved", "dismissed"] | None
    created_at: datetime | None
