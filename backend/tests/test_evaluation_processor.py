from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.modules.evaluation.processor import EvaluationProcessor
from app.modules.evaluation.service import DATASET_CASE_COUNT, DATASET_VERSION


@pytest.mark.asyncio
async def test_evaluation_processor_fails_closed_without_trusted_observations() -> None:
    row = SimpleNamespace(
        dataset_id="ecom-ai-release-holdout",
        dataset_version=DATASET_VERSION,
        dataset_hash=bytes.fromhex("00" * 32),
        baseline_type="agent",
        baseline_version="baseline-v1",
        candidate_type="agent",
        candidate_version="candidate-v2",
        run_status="queued",
        release_gate=None,
        report=None,
        started_at=None,
        finished_at=None,
        error_code="STALE",
        version=0,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=row), commit=AsyncMock())

    processed = await EvaluationProcessor(session).process_one()  # type: ignore[arg-type]

    assert processed is True
    assert row.run_status == "completed"
    assert row.release_gate == "insufficient_evidence"
    assert row.report["case_count"] == DATASET_CASE_COUNT
    assert row.report["reasons"] == ["trusted_observations_missing"]
    assert row.report["execution_mode"] == "fail_closed_without_observations"
    assert row.error_code is None
    assert row.version == 1
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_evaluation_processor_is_idle_without_queued_run() -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=None), commit=AsyncMock())

    processed = await EvaluationProcessor(session).process_one()  # type: ignore[arg-type]

    assert processed is False
    session.commit.assert_not_awaited()
