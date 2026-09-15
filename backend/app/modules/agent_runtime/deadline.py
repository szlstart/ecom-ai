from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from app.modules.agent_runtime.provider_gateway import AgentStreamCallback


class AgentStreamGate:
    """Stop a timed-out model task from publishing stale partial output."""

    def __init__(self, callback: AgentStreamCallback | None) -> None:
        self._callback = callback
        self._open = True

    async def publish(self, event_type: str, text: str) -> None:
        if self._open and self._callback is not None:
            await self._callback(event_type, text)

    def close(self) -> None:
        self._open = False


def _consume_background_result(task: asyncio.Task[object]) -> None:
    try:
        task.result()
    except BaseException:
        # The caller has already recorded a bounded timeout and continued with a
        # deterministic evidence-backed response.  Consume the cancelled/late
        # task result so it cannot become an unhandled event-loop exception.
        pass


async def hard_deadline[T](awaitable: Awaitable[T], *, budget_seconds: float) -> T:
    """Return on deadline even if an upstream coroutine is slow to cancel.

    ``asyncio.wait_for`` waits for cancellation cleanup.  Some third-party HTTP
    streams can keep that cleanup pending after their answer text has arrived,
    which leaves the commerce conversation in an endless transient state.  This
    helper cancels best-effort but does not make the durable Agent run depend on
    the provider acknowledging cancellation.
    """

    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=budget_seconds)
    if task in done:
        return task.result()
    task.cancel()
    task.add_done_callback(_consume_background_result)
    raise TimeoutError(f"Agent model stage exceeded {budget_seconds:g} seconds")
