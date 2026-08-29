import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core import worker_health
from app.core.config import Settings


async def test_worker_heartbeat_proves_event_loop_and_build_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat_path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(worker_health, "HEARTBEAT_PATH", heartbeat_path)
    monkeypatch.setenv("ECOM_BUILD_SHA", "test-build")
    stopping = asyncio.Event()
    task = worker_health.start_worker_heartbeat(
        "test-worker", Settings(build_sha="test-build"), stopping
    )
    await asyncio.sleep(0.01)
    assert worker_health.check_worker_heartbeat() == 0
    monkeypatch.setenv("ECOM_BUILD_SHA", "different-build")
    assert worker_health.check_worker_heartbeat() == 1
    stopping.set()
    await task


def test_worker_heartbeat_rejects_stale_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat_path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(worker_health, "HEARTBEAT_PATH", heartbeat_path)
    observed = datetime.now(UTC) - timedelta(minutes=10)
    heartbeat_path.write_text(
        '{"worker":"stale","build_sha":"test","event_loop_at":"' + observed.isoformat() + '"}',
        encoding="utf-8",
    )
    assert worker_health.check_worker_heartbeat(max_age_seconds=30) == 1
