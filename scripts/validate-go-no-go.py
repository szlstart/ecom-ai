#!/usr/bin/env python3
"""Validate a signed release decision and derive a fail-closed outcome."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REQUIRED_GATES = {
    "release",
    "quality",
    "data",
    "security",
    "operations",
    "deployment",
    "business",
    "post_release",
}
STATUSES = {"pass", "fail", "pending", "insufficient_evidence"}
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def validate(manifest: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("schema_version must equal 1")
    release_id = manifest.get("release_id")
    if not isinstance(release_id, str) or not release_id or release_id.startswith("replace-"):
        errors.append("release_id must identify the real release")
    commit_sha = manifest.get("commit_sha")
    if not isinstance(commit_sha, str) or re.fullmatch(r"[0-9a-f]{40}", commit_sha) is None:
        errors.append("commit_sha must be a 40-character lowercase Git SHA")

    gates = manifest.get("gates")
    if not isinstance(gates, list):
        errors.append("gates must be a list")
        gates = []
    ids = [gate.get("id") for gate in gates if isinstance(gate, dict)]
    if len(ids) != len(set(ids)):
        errors.append("gate ids must be unique")
    missing = sorted(REQUIRED_GATES - set(ids))
    unknown = sorted(set(ids) - REQUIRED_GATES)
    if missing:
        errors.append("missing gates: " + ", ".join(missing))
    if unknown:
        errors.append("unknown gates: " + ", ".join(unknown))

    gate_statuses: dict[str, str] = {}
    for gate in gates:
        if not isinstance(gate, dict):
            errors.append("each gate must be an object")
            continue
        gate_id = gate.get("id")
        if not isinstance(gate_id, str):
            errors.append("each gate must have a string id")
            continue
        status = gate.get("status")
        if status not in STATUSES:
            errors.append(f"{gate_id}: invalid status")
            continue
        gate_statuses[gate_id] = status
        owner = gate.get("owner")
        if not isinstance(owner, str) or not owner:
            errors.append(f"{gate_id}: owner is required")
        evidence = gate.get("evidence")
        if not isinstance(evidence, list) or any(
            not isinstance(item, str) or not item for item in evidence
        ):
            errors.append(f"{gate_id}: evidence must be a list of non-empty references")
        if status == "pass":
            if not evidence:
                errors.append(f"{gate_id}: pass requires evidence")
            decided_by = gate.get("decided_by")
            if not isinstance(decided_by, str) or not decided_by:
                errors.append(f"{gate_id}: pass requires decided_by")
            decided_at = gate.get("decided_at")
            if not isinstance(decided_at, str) or ISO_UTC.fullmatch(decided_at) is None:
                errors.append(f"{gate_id}: pass requires a UTC decided_at timestamp")

    all_pass = set(gate_statuses) == REQUIRED_GATES and all(
        gate_statuses[gate_id] == "pass" for gate_id in REQUIRED_GATES
    )
    return {
        "schema_version": 1,
        "release_id": release_id,
        "commit_sha": commit_sha,
        "decision": "go" if all_pass and not errors else "no_go",
        "gate_statuses": dict(sorted(gate_statuses.items())),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise TypeError("manifest root must be an object")
        report = validate(manifest)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        report = {"schema_version": 1, "decision": "no_go", "errors": [str(exc)]}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["decision"] == "go" else 2


if __name__ == "__main__":
    raise SystemExit(main())
