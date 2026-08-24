from datetime import datetime
from typing import Literal

from pydantic import Field

from app.api.schemas import StrictRequest

EvaluationTargetType = Literal["agent", "skill", "model", "prompt", "tool", "multi_agent"]


class EvaluationRunCreate(StrictRequest):
    dataset_id: Literal["ecom-ai-release-holdout"]
    dataset_version: Literal["2026.08.25-v1"]
    baseline_type: EvaluationTargetType
    baseline_version: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    candidate_type: EvaluationTargetType
    candidate_version: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    require_significant_gain: bool = False


class EvaluationRunView(StrictRequest):
    evaluation_id: str
    dataset_id: str
    dataset_version: str
    dataset_sha256: str
    baseline_type: str
    baseline_version: str
    candidate_type: str
    candidate_version: str
    require_significant_gain: bool
    status: str
    release_gate: str | None
    report: dict[str, object] | None
    trace_id: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None


class EvaluationRunList(StrictRequest):
    items: list[EvaluationRunView]


class ObservabilitySummary(StrictRequest):
    window: Literal["process_lifetime"] = "process_lifetime"
    metrics: dict[str, object]
    trace_backend: str
    log_backend: str
    sensitive_content_included: Literal[False] = False
