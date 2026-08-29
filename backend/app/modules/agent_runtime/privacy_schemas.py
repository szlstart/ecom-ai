from datetime import datetime
from typing import Literal

from pydantic import Field

from app.api.schemas import StrictRequest


class AiMemoryView(StrictRequest):
    memory_id: str
    namespace: Literal["exclusive", "store"]
    store_id: str | None
    memory_type: str
    memory_key: str
    value: str
    source_type: str
    consent_id: str | None
    status: Literal["candidate", "active", "superseded", "revoked", "expired", "deleted"]
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int


class AiMemoryList(StrictRequest):
    items: list[AiMemoryView]


class AiMemoryRevisionRequest(StrictRequest):
    new_value: str = Field(min_length=1, max_length=500)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    confirmed: Literal[True]


class AiMemoryActivationRequest(StrictRequest):
    confirmed: Literal[True]


class AiMemoryDeleteRequest(StrictRequest):
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    confirmed: Literal[True]


class AiPersonalizationDisableRequest(StrictRequest):
    confirmation: Literal["DISABLE_ALL_AI_PERSONALIZATION"]
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")


class AiCleanupTaskView(StrictRequest):
    cleanup_task_id: str
    command_type: str
    scope_type: str
    scope_id: str
    source_resource_type: str
    source_resource_id: str
    status: Literal["queued", "running", "succeeded", "partial_failed", "failed"]
    total_count: int
    processed_count: int
    failed_count: int
    retry_count: int
    max_retries: int
    error_code: str | None
    can_retry: bool
    created_at: datetime
    updated_at: datetime
    version: int
