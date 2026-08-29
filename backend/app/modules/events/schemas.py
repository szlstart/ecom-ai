from datetime import datetime
from typing import Literal

from pydantic import Field

from app.api.schemas import StrictRequest


class DeadLetterView(StrictRequest):
    dead_letter_id: str
    source_type: str
    source_id: str
    event_type: str
    schema_version: int
    scope_type: str
    scope_id: int
    payload_hash: str
    payload_keys: list[str]
    failure_count: int
    first_failed_at: datetime
    last_failed_at: datetime
    last_error_code: str
    last_error: str
    status: Literal["open", "replaying", "resolved", "ignored"]
    replay_count: int
    last_replay_at: datetime | None
    original_trace_id: str | None
    replay_trace_id: str | None
    available_actions: list[Literal["preview_replay", "ignore"]]
    version: int


class DeadLetterList(StrictRequest):
    items: list[DeadLetterView]


class DeadLetterReplayPreview(StrictRequest):
    dead_letter: DeadLetterView
    replayable: bool
    blockers: list[str]
    source_status: str | None
    immutable_payload_hash: str
    impact_summary: list[str]
    required_approval_count: int
    preview_token: str
    expires_at: datetime


class DeadLetterReplayRequest(StrictRequest):
    preview_token: str = Field(min_length=40, max_length=4096)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=5, max_length=1000)


class DeadLetterIgnoreRequest(StrictRequest):
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=5, max_length=1000)
