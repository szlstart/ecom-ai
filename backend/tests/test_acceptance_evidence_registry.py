from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_acceptance_audit_resolves_exact_test_and_domain_evidence(tmp_path: Path) -> None:
    report_path = tmp_path / "traceability-audit.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/acceptance-audit.py"),
            "--strict",
            "--output",
            str(report_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["decision"] == "pass"
    assert report["summary"]["finding_count"] == 0
    assert report["summary"]["registered_test_families"] >= 77
    assert report["summary"]["collected_test_selectors"] >= 231
    assert report["summary"]["domain_aggregates_with_evidence"] == 14
