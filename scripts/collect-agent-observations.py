from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import get_settings
from app.modules.evaluation.collector import LiveModelObservationCollector
from app.modules.evaluation.runner import load_dataset


async def collect(dataset: Path, output: Path, concurrency: int, refresh: bool) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = load_dataset(dataset)
    existing: list[dict[str, object]] = []
    if output.exists() and not refresh:
        raw = json.loads(output.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("dataset_sha256") == manifest.sha256:
            rows = raw.get("observations")
            if isinstance(rows, list):
                existing = [item for item in rows if isinstance(item, dict)]

    def checkpoint(payload: dict[str, object]) -> None:
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已完成 {len(payload['observations'])}/{len(manifest.cases)} 个配对用例。", flush=True)

    artifact = await LiveModelObservationCollector(
        get_settings(), concurrency=concurrency
    ).collect(dataset, existing_observations=existing, on_checkpoint=checkpoint)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Collected {len(artifact['observations'])} paired live-model observations "
        f"for dataset {artifact['dataset_version']}."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect trusted paired Agent observations")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=1, choices=range(1, 9))
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    asyncio.run(collect(args.dataset, args.output, args.concurrency, args.refresh))


if __name__ == "__main__":
    main()
