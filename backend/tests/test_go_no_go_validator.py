from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _module() -> ModuleType:
    path = ROOT / "scripts/validate-go-no-go.py"
    spec = importlib.util.spec_from_file_location("validate_go_no_go", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_template_fails_closed_until_real_evidence_is_signed() -> None:
    manifest = json.loads(
        (ROOT / "docs/acceptance/go-no-go.template.json").read_text(encoding="utf-8")
    )
    report = _module().validate(manifest)
    assert report["decision"] == "no_go"
    assert "release_id must identify the real release" in report["errors"]


def test_all_required_signed_gates_produce_go() -> None:
    manifest = json.loads(
        (ROOT / "docs/acceptance/go-no-go.template.json").read_text(encoding="utf-8")
    )
    manifest["release_id"] = "2026.08.25-rc1"
    manifest["commit_sha"] = "a" * 40
    for gate in manifest["gates"]:
        gate.update(
            status="pass",
            evidence=[f"artifacts/acceptance/2026.08.25-rc1/{gate['id']}/report.json"],
            missing_evidence=[],
            decided_by="release-reviewer",
            decided_at="2026-08-25T12:00:00Z",
        )
    report = _module().validate(manifest)
    assert report["decision"] == "go"
    assert report["errors"] == []


def test_missing_gate_or_unsigned_pass_remains_no_go() -> None:
    manifest = json.loads(
        (ROOT / "docs/acceptance/go-no-go.template.json").read_text(encoding="utf-8")
    )
    manifest["release_id"] = "2026.08.25-rc1"
    manifest["commit_sha"] = "b" * 40
    manifest["gates"] = manifest["gates"][:-1]
    manifest["gates"][0]["status"] = "pass"
    report = _module().validate(manifest)
    assert report["decision"] == "no_go"
    assert any("missing gates" in item for item in report["errors"])
    assert any("pass requires evidence" in item for item in report["errors"])


def test_unresolved_gate_requires_auditable_blocker_contract() -> None:
    manifest = json.loads(
        (ROOT / "docs/acceptance/go-no-go.template.json").read_text(encoding="utf-8")
    )
    manifest["release_id"] = "2026.08.25-rc1"
    manifest["commit_sha"] = "c" * 40
    gate = manifest["gates"][0]
    gate["missing_evidence"] = []
    gate["verification_commands"] = []
    gate["exit_conditions"] = []

    report = _module().validate(manifest)

    assert report["decision"] == "no_go"
    assert "release: insufficient_evidence requires missing_evidence" in report["errors"]
    assert any("release: verification_commands" in item for item in report["errors"])
    assert any("release: exit_conditions" in item for item in report["errors"])
