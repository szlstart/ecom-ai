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
_HEALTH_STATE: dict[str, object] = {
    "consecutive_failures": 0,
    "last_success_at": None,
    "last_error_code": None,
}


def start_worker_heartbeat(
    worker_name: str,
    settings: Settings,
    stopping: asyncio.Event,
) -> asyncio.Task[None]:
    """Start an event-loop heartbeat; a blocked or dead loop stops refreshing it."""

    _HEALTH_STATE.update(
        consecutive_failures=0,
        last_success_at=datetime.now(UTC).isoformat(),
        last_error_code=None,
    )

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
        "consecutive_failures": int(str(_HEALTH_STATE["consecutive_failures"])),
        "last_success_at": _HEALTH_STATE["last_success_at"],
        "last_error_code": _HEALTH_STATE["last_error_code"],
    }
    temporary = HEARTBEAT_PATH.with_name(f"{HEARTBEAT_PATH.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(HEARTBEAT_PATH)


def record_worker_success() -> None:
    _HEALTH_STATE.update(
        consecutive_failures=0,
        last_success_at=datetime.now(UTC).isoformat(),
        last_error_code=None,
    )


def record_worker_failure(error_code: str) -> None:
    _HEALTH_STATE.update(
        consecutive_failures=int(str(_HEALTH_STATE["consecutive_failures"])) + 1,
        last_error_code=error_code[:120],
    )


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
        max_failures = int(os.getenv("ECOM_WORKER_HEALTH_MAX_CONSECUTIVE_FAILURES", "5"))
        if int(payload.get("consecutive_failures", 0)) >= max_failures:
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
