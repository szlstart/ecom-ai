from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings

HEARTBEAT_PATH = Path(tempfile.gettempdir()) / "ecom-ai-worker-heartbeat.json"
_HEARTBEAT_TASKS: set[asyncio.Task[None]] = set()


def start_worker_heartbeat(
    worker_name: str,
    settings: Settings,
    stopping: asyncio.Event,
) -> asyncio.Task[None]:
    """Start an event-loop heartbeat; a blocked or dead loop stops refreshing it."""

    async def heartbeat() -> None:
        while not stopping.is_set():
            _write(worker_name, settings.build_sha)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=10)
            except TimeoutError:
                pass

    task = asyncio.create_task(heartbeat(), name=f"{worker_name}-heartbeat")
    _HEARTBEAT_TASKS.add(task)
    task.add_done_callback(_HEARTBEAT_TASKS.discard)
    return task


def _write(worker_name: str, build_sha: str) -> None:
    payload = {
        "worker": worker_name,
        "build_sha": build_sha,
        "event_loop_at": datetime.now(UTC).isoformat(),
        "monotonic_seconds": time.monotonic(),
    }
    temporary = HEARTBEAT_PATH.with_name(f"{HEARTBEAT_PATH.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(HEARTBEAT_PATH)


def check_worker_heartbeat(max_age_seconds: float = 90) -> int:
    try:
        payload = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
        observed = datetime.fromisoformat(str(payload["event_loop_at"]))
        age = (datetime.now(UTC) - observed).total_seconds()
        expected_sha = os.getenv("ECOM_BUILD_SHA")
        if age < 0 or age > max_age_seconds:
            return 1
        if expected_sha and payload.get("build_sha") != expected_sha:
            return 1
        if not payload.get("worker"):
            return 1
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=float(os.getenv("ECOM_WORKER_HEALTH_MAX_AGE_SECONDS", "90")),
    )
    arguments = parser.parse_args()
    if not arguments.check:
        parser.error("--check is required")
    raise SystemExit(check_worker_heartbeat(arguments.max_age_seconds))


if __name__ == "__main__":
    main()
