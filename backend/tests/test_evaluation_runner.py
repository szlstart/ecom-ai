import hashlib
import json
from pathlib import Path

import pytest

from app.modules.evaluation.runner import (
    CaseObservation,
    PairedObservation,
    evaluate,
    load_dataset,
    load_observations,
)

ROOT = Path(__file__).resolve().parents[2]


def _observation(passed: bool = True, safety: int = 0) -> CaseObservation:
    return CaseObservation(passed, safety, 100.0, 0.01, True, True)


def test_release_holdout_is_versioned_and_has_required_coverage() -> None:
    dataset = load_dataset(ROOT / "eval/golden.json")
    assert dataset.dataset_id == "ecom-ai-release-holdout"
    assert len(dataset.cases) >= 30
    assert len(dataset.sha256) == 64


def test_missing_observations_never_fabricate_a_pass() -> None:
    dataset = load_dataset(ROOT / "eval/golden.json")
    report = evaluate(dataset, None)
    assert report["release_gate"] == "insufficient_evidence"
    assert report["reasons"] == ["observations_missing"]


def test_security_violation_is_a_hard_failure() -> None:
    dataset = load_dataset(ROOT / "eval/golden.json")
    rows = tuple(
        PairedObservation(
            case.case_id,
            _observation(),
            _observation(safety=1 if case.case_id == "security-001" else 0),
        )
        for case in dataset.cases
    )
    report = evaluate(dataset, rows)
    assert report["release_gate"] == "fail"
    assert "candidate_safety_violation" in report["reasons"]


def test_multi_agent_requires_pre_registered_significant_gain() -> None:
    dataset = load_dataset(ROOT / "eval/golden.json")
    rows = tuple(
        PairedObservation(case.case_id, _observation(), _observation())
        for case in dataset.cases
    )
    report = evaluate(dataset, rows, require_significant_gain=True)
    assert report["release_gate"] == "insufficient_evidence"
    assert "significant_quality_gain_not_proven" in report["reasons"]


def test_observation_artifact_must_match_dataset_hash(tmp_path: Path) -> None:
    dataset = load_dataset(ROOT / "eval/golden.json")
    artifact = tmp_path / "observations.json"
    artifact.write_text(
        json.dumps({"dataset_sha256": hashlib.sha256(b"wrong").hexdigest(), "observations": []})
    )
    with pytest.raises(ValueError, match="immutable dataset hash"):
        load_observations(artifact, dataset)
