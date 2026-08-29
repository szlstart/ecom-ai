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


class AdminAgentRunView(StrictRequest):
    run_id: str
    status: AgentRunStatus
    current_phase: str
    agent_code: str
    agent_version_no: int
    conversation_type: Literal["exclusive", "store"]
    trace_id: str
    context_ref_count: int
    error_code: str | None
    degraded_reason: str | None
    available_actions: list[Literal["cancel"]]
    created_at: datetime
    updated_at: datetime
    version: int


class AdminAgentRunCancelRequest(StrictRequest):
    reason: str = Field(min_length=3, max_length=500)


class ModelProviderHealthView(StrictRequest):
    status: Literal["unconfigured", "available", "degraded", "unavailable"]
    provider: str
    configured_model: str | None
    model_available: bool
    available_models: list[str]
    chat_completions: bool
    structured_output: bool
    streaming: bool
    usage_reporting: bool
    checked_at: datetime
    latency_ms: int
    cache_hit: bool
    error_code: str | None


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


class AgentApprovalDecisionRequest(StrictRequest):
    decision: Literal["approve", "reject"]


class AgentApprovalView(StrictRequest):
    approval_id: str
    run_id: str
    conversation_id: str
    action_type: Literal["refund_submit"]
    approval_status: Literal["pending", "approved", "rejected", "expired", "consumed"]
    decision: Literal["approve", "reject"] | None
    draft: dict[str, object]
    expires_at: datetime
    decided_at: datetime | None
    version: int
