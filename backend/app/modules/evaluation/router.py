from typing import Annotated

from fastapi import APIRouter

from app.api.dependencies import DatabaseSession
from app.api.schemas import Envelope
from app.core.observability import metrics
from app.modules.evaluation.schemas import (
    EvaluationRunCreate,
    EvaluationRunList,
    EvaluationRunView,
    ObservabilitySummary,
)
from app.modules.evaluation.service import EvaluationService
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission

router = APIRouter(prefix="/admin/ai/evaluations", tags=["admin-ai-evaluations"])
observability_router = APIRouter(prefix="/admin/observability", tags=["admin-observability"])


@router.get("", response_model=Envelope[EvaluationRunList], operation_id="AdminAiEvaluation_List")
async def list_evaluations(
    session: DatabaseSession,
    _access: Annotated[AdminAccess, require_admin_permission("ai_evaluations:read")],
) -> Envelope[EvaluationRunList]:
    return Envelope(data=await EvaluationService(session).list())


@router.post(
    "",
    response_model=Envelope[EvaluationRunView],
    status_code=202,
    operation_id="AdminAiEvaluation_Run",
)
async def create_evaluation(
    payload: EvaluationRunCreate,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_evaluations:run")],
) -> Envelope[EvaluationRunView]:
    return Envelope(data=await EvaluationService(session).create(access, payload))


@observability_router.get(
    "", response_model=Envelope[ObservabilitySummary], operation_id="AdminObservability_Query"
)
async def observability_summary(
    _access: Annotated[AdminAccess, require_admin_permission("observability:read")],
) -> Envelope[ObservabilitySummary]:
    return Envelope(
        data=ObservabilitySummary(
            metrics=metrics.snapshot(),
            trace_backend="tempo",
            log_backend="loki",
        )
    )
