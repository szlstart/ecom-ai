#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.modules.evaluation.runner import evaluate, load_dataset, load_observations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate immutable paired Agent observations against a Golden Dataset."
    )
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--require-significant-gain", action="store_true")
    parser.add_argument(
        "--allow-missing-observations",
        action="store_true",
        help="return success only for the explicit no-observations insufficient-evidence report",
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    dataset = load_dataset(arguments.dataset)
    observations = (
        load_observations(arguments.observations, dataset)
        if arguments.observations
        else None
    )
    report = evaluate(
        dataset,
        observations,
        require_significant_gain=arguments.require_significant_gain,
    )
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    if report["release_gate"] == "pass":
        return 0
    if (
        arguments.allow_missing_observations
        and arguments.observations is None
        and report["release_gate"] == "insufficient_evidence"
        and report.get("reasons") == ["observations_missing"]
    ):
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
