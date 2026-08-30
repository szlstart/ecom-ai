from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.modules.evaluation.models import AiEvaluationRun
from app.modules.evaluation.runner import load_dataset
from app.modules.evaluation.schemas import (
    EvaluationRunCreate,
    EvaluationRunList,
    EvaluationRunView,
)
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.system.models import OutboxEvent

DATASET_PATH = Path(__file__).resolve().parent / "data" / "release-holdout-v2.json"
DATASET_MANIFEST = load_dataset(DATASET_PATH)
DATASET_SHA256 = DATASET_MANIFEST.sha256
DATASET_CASE_COUNT = len(DATASET_MANIFEST.cases)
DATASET_VERSION = DATASET_MANIFEST.version
BASELINE_TYPE = "prompt"
BASELINE_VERSION = "ecom-safe-router-v1"
CANDIDATE_TYPE = "prompt"
CANDIDATE_VERSION = "ecom-safe-router-v2"


class EvaluationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self) -> EvaluationRunList:
        rows = list(
            (
                await self.session.scalars(
                    select(AiEvaluationRun)
                    .order_by(AiEvaluationRun.created_at.desc(), AiEvaluationRun.id.desc())
                    .limit(100)
                )
            ).all()
        )
        return EvaluationRunList(items=[_view(row) for row in rows])

    async def create(self, access: AdminAccess, payload: EvaluationRunCreate) -> EvaluationRunView:
        access.require_scope("platform", 0)
        if payload.dataset_version != DATASET_VERSION:
            raise ValueError("evaluation dataset version is not active")
        if (
            payload.baseline_type,
            payload.baseline_version,
            payload.candidate_type,
            payload.candidate_version,
        ) != (BASELINE_TYPE, BASELINE_VERSION, CANDIDATE_TYPE, CANDIDATE_VERSION):
            from app.core.exceptions import ApplicationError

            raise ApplicationError(
                status=422,
                code="AI_EVALUATION_TARGET_UNSUPPORTED",
                title="Unsupported evaluation target",
                detail="当前只允许评估已登记的生产基线与候选策略。",
            )
        evaluation_no = new_prefixed_ulid("evr_")
        trace_id = request_id_context.get() or new_prefixed_ulid("req_")
        row = AiEvaluationRun(
            evaluation_run_no=evaluation_no,
            dataset_id=payload.dataset_id,
            dataset_version=payload.dataset_version,
            dataset_hash=bytes.fromhex(DATASET_SHA256),
            baseline_type=payload.baseline_type,
            baseline_version=payload.baseline_version,
            candidate_type=payload.candidate_type,
            candidate_version=payload.candidate_version,
            require_significant_gain=payload.require_significant_gain,
            run_status="queued",
            requested_by=access.context.user.id,
            trace_id=trace_id,
        )
        self.session.add(row)
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="ai.evaluation.requested.v1",
                aggregate_type="ai_evaluation_run",
                aggregate_no=evaluation_no,
                aggregate_version=0,
                payload={
                    "evaluation_id": evaluation_no,
                    "dataset_id": payload.dataset_id,
                    "dataset_version": payload.dataset_version,
                    "dataset_sha256": DATASET_SHA256,
                },
                event_status="pending",
                available_at=utc_now(),
                trace_id=trace_id,
            )
        )
        record_admin_operation(
            self.session,
            access,
            action="ai.evaluation.run",
            target_type="ai_evaluation_run",
            target_no=evaluation_no,
            after={
                "dataset_id": payload.dataset_id,
                "candidate_type": payload.candidate_type,
                "candidate_version": payload.candidate_version,
                "status": "queued",
            },
            scope_type="platform",
            scope_id=0,
        )
        await self.session.commit()
        await self.session.refresh(row)
        return _view(row)


def _view(row: AiEvaluationRun) -> EvaluationRunView:
    return EvaluationRunView(
        evaluation_id=row.evaluation_run_no,
        dataset_id=row.dataset_id,
        dataset_version=row.dataset_version,
        dataset_sha256=row.dataset_hash.hex(),
        baseline_type=row.baseline_type,
        baseline_version=row.baseline_version,
        candidate_type=row.candidate_type,
        candidate_version=row.candidate_version,
        require_significant_gain=row.require_significant_gain,
        status=row.run_status,
        release_gate=row.release_gate,
        report=row.report,
        trace_id=row.trace_id,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code,
    )
