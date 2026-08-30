from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.modules.evaluation.processor import EvaluationProcessor
from app.modules.evaluation.service import (
    BASELINE_TYPE,
    BASELINE_VERSION,
    CANDIDATE_TYPE,
    CANDIDATE_VERSION,
    DATASET_CASE_COUNT,
    DATASET_MANIFEST,
    DATASET_VERSION,
)


@pytest.mark.asyncio
async def test_evaluation_processor_fails_closed_without_trusted_observations() -> None:
    row = SimpleNamespace(
        dataset_id="ecom-ai-release-holdout",
        dataset_version=DATASET_VERSION,
        dataset_hash=bytes.fromhex("00" * 32),
        baseline_type=BASELINE_TYPE,
        baseline_version=BASELINE_VERSION,
        candidate_type=CANDIDATE_TYPE,
        candidate_version=CANDIDATE_VERSION,
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


@pytest.mark.asyncio
async def test_evaluation_processor_runs_trusted_live_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(
        dataset_id=DATASET_MANIFEST.dataset_id,
        dataset_version=DATASET_VERSION,
        dataset_hash=bytes.fromhex(DATASET_MANIFEST.sha256),
        baseline_type=BASELINE_TYPE,
        baseline_version=BASELINE_VERSION,
        candidate_type=CANDIDATE_TYPE,
        candidate_version=CANDIDATE_VERSION,
        require_significant_gain=False,
        run_status="queued",
        release_gate=None,
        report=None,
        started_at=None,
        finished_at=None,
        error_code=None,
        version=0,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=row), commit=AsyncMock())
    observation = {
        "passed": True,
        "safety_violations": 0,
        "latency_ms": 100.0,
        "cost_usd": 0.001,
        "tool_correct": True,
        "citation_correct": True,
    }
    artifact = {
        "dataset_sha256": DATASET_MANIFEST.sha256,
        "collection_mode": "test_live_collection",
        "models": ["test-model"],
        "policy_sha256": {"baseline": "a", "candidate": "b"},
        "scorer_version": "test-v1",
        "observations": [
            {"id": case.case_id, "baseline": observation, "candidate": observation}
            for case in DATASET_MANIFEST.cases
        ],
    }
    monkeypatch.setattr(
        "app.modules.evaluation.processor.LiveModelObservationCollector.collect",
        AsyncMock(return_value=artifact),
    )
    settings = Settings(
        _env_file=None,
        agent_model_api_url="https://models.invalid/v1/chat/completions",
        agent_model_api_key="test-key",
        agent_model_name="moonshot-v1-8k",
    )

    processed = await EvaluationProcessor(session, settings).process_one()  # type: ignore[arg-type]

    assert processed is True
    assert row.run_status == "completed"
    assert row.release_gate == "pass"
    assert row.report["observation_collection"]["scorer_version"] == "test-v1"
    assert len(row.report["observation_collection"]["case_results"]) == DATASET_CASE_COUNT
    assert row.version == 2
    assert session.commit.await_count == 2
