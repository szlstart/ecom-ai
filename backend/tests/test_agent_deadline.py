from __future__ import annotations

import asyncio

import pytest

from app.modules.agent_runtime.deadline import AgentStreamGate, hard_deadline


@pytest.mark.asyncio
async def test_hard_deadline_returns_without_waiting_for_slow_cancellation() -> None:
    cancellation_released = asyncio.Event()

    async def stubborn_provider() -> str:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await cancellation_released.wait()
        return "late"

    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError, match="exceeded"):
        await hard_deadline(stubborn_provider(), budget_seconds=0.01)
    assert asyncio.get_running_loop().time() - started < 0.2
    cancellation_released.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stream_gate_ignores_late_events_after_timeout() -> None:
    updates: list[tuple[str, str]] = []

    async def capture(event_type: str, text: str) -> None:
        updates.append((event_type, text))

    gate = AgentStreamGate(capture)
    await gate.publish("answer", "current")
    gate.close()
    await gate.publish("answer", "stale")
    assert updates == [("answer", "current")]
