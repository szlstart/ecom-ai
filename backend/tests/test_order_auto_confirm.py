from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import SecurityService, utc_now
from app.modules.orders.models import Order, OrderStatusLog
from app.modules.orders.service import OrderService
from app.modules.system.models import OutboxEvent


@pytest.mark.asyncio
async def test_auto_confirm_marks_received_and_emits_auditable_event() -> None:
    order = Order(
        id=41,
        order_no="ord_auto_confirm_test",
        order_status="shipped",
        fulfillment_status="shipped",
        after_sale_status="none",
        version=8,
    )
    session = MagicMock(spec=AsyncSession)
    session.commit = AsyncMock()
    service = OrderService(
        cast(AsyncSession, session),
        get_settings(),
        SecurityService(get_settings()),
    )
    repository = MagicMock()
    repository.auto_confirmable_orders = AsyncMock(return_value=[order])
    cast(Any, service).repository = repository

    assert await service.auto_confirm_due(days=7, limit=20) == 1
    assert order.order_status == "completed"
    assert order.fulfillment_status == "received"
    assert order.completed_at is not None and order.completed_at <= utc_now()
    assert order.version == 9
    session.commit.assert_awaited_once()
    added = [call.args[0] for call in session.add.call_args_list]
    outbox = next(item for item in added if isinstance(item, OutboxEvent))
    assert outbox.payload["confirmation_type"] == "automatic"
    logs = [item for call in session.add_all.call_args_list for item in call.args[0]]
    assert len(logs) == 2
    assert all(isinstance(item, OrderStatusLog) for item in logs)
    assert {item.event_code for item in logs} == {"order.receipt_auto_confirmed"}
