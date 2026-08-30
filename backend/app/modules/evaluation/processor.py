from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utc_now
from app.modules.evaluation.models import AiEvaluationRun
from app.modules.evaluation.service import DATASET_CASE_COUNT


class EvaluationProcessor:
    """Finish queued evaluations without inventing missing model observations.

    The management API currently registers an immutable dataset and target pair,
    but does not accept client-supplied observations. Until a trusted observation
    collector attaches complete paired evidence, the only safe automated outcome
    is ``insufficient_evidence``. This processor prevents jobs from remaining
    queued forever while keeping the release gate fail-closed.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def process_one(self) -> bool:
        row = await self.session.scalar(
            select(AiEvaluationRun)
            .where(AiEvaluationRun.run_status == "queued")
            .order_by(AiEvaluationRun.created_at, AiEvaluationRun.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return False

        now = utc_now()
        row.run_status = "completed"
        row.started_at = now
        row.finished_at = now
        row.release_gate = "insufficient_evidence"
        row.report = {
            "dataset_id": row.dataset_id,
            "dataset_version": row.dataset_version,
            "dataset_sha256": row.dataset_hash.hex(),
            "case_count": DATASET_CASE_COUNT,
            "release_gate": "insufficient_evidence",
            "reasons": ["trusted_observations_missing"],
            "execution_mode": "fail_closed_without_observations",
            "candidate_type": row.candidate_type,
            "candidate_version": row.candidate_version,
            "baseline_type": row.baseline_type,
            "baseline_version": row.baseline_version,
        }
        row.error_code = None
        row.version += 1
        await self.session.commit()
        return True
