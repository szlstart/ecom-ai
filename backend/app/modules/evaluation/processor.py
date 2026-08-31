from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import utc_now
from app.modules.evaluation.collector import LiveModelObservationCollector
from app.modules.evaluation.models import AiEvaluationRun
from app.modules.evaluation.runner import evaluate, parse_observations
from app.modules.evaluation.service import (
    BASELINE_TYPE,
    BASELINE_VERSION,
    CANDIDATE_TYPE,
    CANDIDATE_VERSION,
    DATASET_CASE_COUNT,
    DATASET_MANIFEST,
    DATASET_PATH,
)


class EvaluationProcessor:
    """Finish queued evaluations without inventing missing model observations.

    The management API currently registers an immutable dataset and target pair,
    but does not accept client-supplied observations. Until a trusted observation
    collector attaches complete paired evidence, the only safe automated outcome
    is ``insufficient_evidence``. This processor prevents jobs from remaining
    queued forever while keeping the release gate fail-closed.
    """

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings

    async def process_one(self) -> bool:
        now = utc_now()
        stale_before = now - timedelta(hours=2)
        row = await self.session.scalar(
            select(AiEvaluationRun)
            .where(
                or_(
                    AiEvaluationRun.run_status == "queued",
                    and_(
                        AiEvaluationRun.run_status == "running",
                        AiEvaluationRun.started_at < stale_before,
                    ),
                )
            )
            .order_by(
                case((AiEvaluationRun.run_status == "queued", 0), else_=1),
                AiEvaluationRun.created_at,
                AiEvaluationRun.id,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return False

        if row.run_status == "running":
            row.run_status = "completed"
            row.finished_at = now
            row.release_gate = "insufficient_evidence"
            row.report = self._insufficient_report(row, "evaluation_worker_interrupted")
            row.report["execution_mode"] = "fail_closed_interrupted_worker"
            row.error_code = "EVALUATION_WORKER_INTERRUPTED"
            row.version += 1
            await self.session.commit()
            return True

        supported_pair = (
            row.baseline_type,
            row.baseline_version,
            row.candidate_type,
            row.candidate_version,
        ) == (BASELINE_TYPE, BASELINE_VERSION, CANDIDATE_TYPE, CANDIDATE_VERSION)
        if self.settings is None or not supported_pair:
            row.run_status = "completed"
            row.started_at = now
            row.finished_at = now
            row.release_gate = "insufficient_evidence"
            row.report = self._insufficient_report(
                row,
                "trusted_observations_missing" if supported_pair else "target_pair_unsupported",
            )
            row.error_code = None
            row.version += 1
            await self.session.commit()
            return True

        row.run_status = "running"
        row.started_at = now
        row.version += 1
        await self.session.commit()
        try:
            artifact = await LiveModelObservationCollector(self.settings).collect(DATASET_PATH)
            observations = parse_observations(artifact, DATASET_MANIFEST)
            report = evaluate(
                DATASET_MANIFEST,
                observations,
                require_significant_gain=row.require_significant_gain,
            )
            artifact_bytes = json.dumps(
                artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            report["observation_sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
            report["observation_collection"] = {
                "mode": artifact["collection_mode"],
                "models": artifact["models"],
                "policy_sha256": artifact["policy_sha256"],
                "scorer_version": artifact["scorer_version"],
                "case_results": _public_case_results(artifact),
            }
            row.run_status = "completed"
            row.finished_at = utc_now()
            row.release_gate = str(report["release_gate"])
            row.report = report
            row.error_code = None
        except Exception as exc:
            row.run_status = "completed"
            row.finished_at = utc_now()
            row.release_gate = "insufficient_evidence"
            reason = _collector_failure_reason(exc)
            row.report = self._insufficient_report(row, reason)
            row.error_code = reason.upper()[:64]
        row.version += 1
        await self.session.commit()
        return True

    @staticmethod
    def _insufficient_report(row: AiEvaluationRun, reason: str) -> dict[str, object]:
        return {
            "dataset_id": row.dataset_id,
            "dataset_version": row.dataset_version,
            "dataset_sha256": row.dataset_hash.hex(),
            "case_count": DATASET_CASE_COUNT,
            "release_gate": "insufficient_evidence",
            "reasons": [reason],
            "execution_mode": "fail_closed_without_observations",
            "candidate_type": row.candidate_type,
            "candidate_version": row.candidate_version,
            "baseline_type": row.baseline_type,
            "baseline_version": row.baseline_version,
        }


def _collector_failure_reason(exc: Exception) -> str:
    message = str(exc)
    if "MODEL_PROVIDER_NOT_CONFIGURED" in message:
        return "model_provider_not_configured"
    if "MODEL_EVALUATION_PRICE_NOT_REGISTERED" in message:
        return "model_pricing_not_registered"
    if "MODEL_EVALUATION_PROVIDER_FAILED" in message:
        return "model_provider_unavailable"
    if isinstance(exc, ValueError):
        return "observation_validation_failed"
    return "evaluation_collection_failed"


def _public_case_results(artifact: dict[str, object]) -> list[dict[str, object]]:
    raw_rows = artifact.get("observations")
    if not isinstance(raw_rows, list):
        raise ValueError("observation rows are missing")
    results: list[dict[str, object]] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise ValueError("observation row is invalid")
        baseline = raw.get("baseline")
        candidate = raw.get("candidate")
        if not isinstance(baseline, dict) or not isinstance(candidate, dict):
            raise ValueError("paired observation is invalid")
        results.append(
            {
                "id": raw.get("id"),
                "baseline_passed": baseline.get("passed"),
                "candidate_passed": candidate.get("passed"),
                "candidate_safety_violations": candidate.get("safety_violations"),
                "candidate_tool_correct": candidate.get("tool_correct"),
                "candidate_citation_correct": candidate.get("citation_correct"),
                "candidate_answer_correct": candidate.get("answer_correct"),
            }
        )
    return results
