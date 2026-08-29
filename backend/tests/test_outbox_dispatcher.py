from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.events.dispatcher import DomainEventDispatcher, _redact


def test_outbox_projection_redacts_sensitive_values_recursively() -> None:
    result = _redact(
        {
            "user_id": "usr_01",
            "email": "person@example.com",
            "nested": {
                "access_token": "do-not-leak",
                "items": [{"shipping_address": "private"}, {"quantity": 2}],
            },
        }
    )

    assert result == {
        "user_id": "usr_01",
        "email": "***",
        "nested": {
            "access_token": "***",
            "items": [{"shipping_address": "***"}, {"quantity": 2}],
        },
    }


@pytest.mark.asyncio
async def test_outbox_projection_publishes_redacted_payload_atomically() -> None:
    redis = MagicMock()
    redis.eval = AsyncMock(return_value="1-0")
    dispatcher = DomainEventDispatcher(AsyncMock(), redis, "test")
    event = SimpleNamespace(
        event_no="evt_01",
        event_type="order.created.v1",
        aggregate_type="order",
        aggregate_no="ord_01",
        aggregate_version=1,
        trace_id="trace_01",
        created_at=datetime(2026, 1, 1),
        payload={"order_id": "ord_01", "email": "private@example.com"},
    )

    await dispatcher._publish(event)

    redis.eval.assert_awaited_once()
    arguments = redis.eval.await_args.args
    assert arguments[2] == "ecom:outbox:published:evt_01"
    assert arguments[3] == "ecom:test:stream:domain-events:v1"
    assert '"email":"***"' in arguments[-1]
    assert "private@example.com" not in arguments[-1]


@pytest.mark.asyncio
async def test_observer_consumer_acknowledges_only_valid_stream_records() -> None:
    redis = MagicMock()
    redis.xgroup_create = AsyncMock(return_value=True)
    redis.xreadgroup = AsyncMock(
        return_value=[
            (
                "ecom:test:stream:domain-events:v1",
                [("1-0", {"event_no": "evt_01"}), ("2-0", {"payload": "{}"})],
            )
        ]
    )
    redis.xack = AsyncMock(return_value=1)
    redis.set = AsyncMock(return_value=True)
    dispatcher = DomainEventDispatcher(AsyncMock(), redis, "test")

    acknowledged = await dispatcher.consume_observer_batch(10)

    assert acknowledged == 1
    redis.xack.assert_awaited_once_with(
        "ecom:test:stream:domain-events:v1", "domain-event-observers-v1", "1-0"
    )
    redis.set.assert_awaited_once()
