from datetime import datetime
from typing import Literal

from pydantic import Field

from app.api.schemas import StrictRequest

AgentRunStatus = Literal["queued", "running", "waiting", "completed", "failed", "cancelled"]
AgentConsentStatus = Literal["active", "paused", "revoked"]


class AgentRunView(StrictRequest):
    run_id: str
    conversation_id: str
    status: AgentRunStatus
    current_phase: str
    output: str | None
    error_code: str | None
    degraded_reason: str | None
    created_at: datetime
    updated_at: datetime


class AgentConsentGrantRequest(StrictRequest):
    consent_type: Literal["personalization", "order_read", "after_sale_write"]
    scope_type: Literal["user", "conversation", "store"]
    scope_id: str | None = Field(default=None, max_length=64)
    policy_version: str = Field(min_length=1, max_length=40)
    expires_at: datetime | None = None


class AgentConsentView(StrictRequest):
    consent_id: str
    consent_type: str
    scope_type: str
    scope_id: str | None
    policy_version: str
    status: AgentConsentStatus
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    version: int


class AgentConsentList(StrictRequest):
    items: list[AgentConsentView]
