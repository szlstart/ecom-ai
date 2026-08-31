import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.modules.evaluation.runner import (
    CaseObservation,
    PairedObservation,
    evaluate,
    load_dataset,
    load_observations,
)


def test_cli_persists_insufficient_evidence_report(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "agent-report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/evaluate-agent.py"),
            str(ROOT / "eval/golden.json"),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(output.read_text(encoding="utf-8"))["release_gate"] == (
        "insufficient_evidence"
    )

    evidence_only = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/evaluate-agent.py"),
            str(ROOT / "eval/golden.json"),
            "--allow-missing-observations",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert evidence_only.returncode == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "case_count": 40,
        "dataset_id": "ecom-ai-release-holdout",
        "dataset_sha256": "baa725b2d44bf84bb3b2edb5919f43ec82d985d6192fac1435f08f70b78707cf",
        "dataset_version": "2026.08.25-v1",
        "reasons": ["observations_missing"],
        "release_gate": "insufficient_evidence",
    }


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
        PairedObservation(case.case_id, _observation(), _observation()) for case in dataset.cases
    )
    report = evaluate(dataset, rows, require_significant_gain=True)
    assert report["release_gate"] == "insufficient_evidence"
    assert "significant_quality_gain_not_proven" in report["reasons"]


def test_release_gate_requires_minimum_absolute_candidate_quality() -> None:
    dataset = load_dataset(ROOT / "eval/golden.json")
    rows = tuple(
        PairedObservation(
            case.case_id,
            _observation(passed=False),
            _observation(passed=index >= 3),
        )
        for index, case in enumerate(dataset.cases)
    )
    report = evaluate(dataset, rows)
    assert report["release_gate"] == "insufficient_evidence"
    assert "candidate_quality_below_minimum" in report["reasons"]


def test_release_gate_scores_final_answer_constraints_separately_from_route() -> None:
    dataset = load_dataset(ROOT / "eval/golden.json")
    bad_answer = CaseObservation(True, 0, 100.0, 0.01, True, True, False)
    rows = tuple(
        PairedObservation(case.case_id, _observation(), bad_answer) for case in dataset.cases
    )
    report = evaluate(dataset, rows)
    assert report["release_gate"] == "insufficient_evidence"
    assert "candidate_answer_accuracy_below_minimum" in report["reasons"]
    assert report["metrics"]["candidate_answer_accuracy"] == 0.0


def test_unknown_model_price_remains_null_in_release_report() -> None:
    dataset = load_dataset(ROOT / "eval/golden.json")
    unknown_cost = CaseObservation(True, 0, 100.0, None, True, True, True)
    rows = tuple(
        PairedObservation(case.case_id, unknown_cost, unknown_cost) for case in dataset.cases
    )

    report = evaluate(dataset, rows)

    assert report["metrics"]["cost_status"] == "unknown"
    assert report["metrics"]["baseline_average_cost_usd"] is None
    assert report["metrics"]["candidate_average_cost_usd"] is None
    assert report["metrics"]["cost_ratio"] is None


def test_observation_artifact_must_match_dataset_hash(tmp_path: Path) -> None:
    dataset = load_dataset(ROOT / "eval/golden.json")
    artifact = tmp_path / "observations.json"
    artifact.write_text(
        json.dumps({"dataset_sha256": hashlib.sha256(b"wrong").hexdigest(), "observations": []})
    )
    with pytest.raises(ValueError, match="immutable dataset hash"):
        load_observations(artifact, dataset)
