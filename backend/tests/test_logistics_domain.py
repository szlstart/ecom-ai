from datetime import datetime
from typing import cast

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ClauseElement

from app.core.exceptions import ApplicationError
from app.modules.logistics.domain import require_shipment_transition
from app.modules.logistics.models import Shipment
from app.modules.logistics.repository import LogisticsRepository
from app.modules.logistics.service import _provider


@pytest.mark.parametrize(
    ("current", "command", "target"),
    [
        ("created", "RecordPickup", "picked_up"),
        ("created", "RecordInTransit", "in_transit"),
        ("created", "RecordDelivery", "delivered"),
        ("created", "RecordException", "exception"),
        ("picked_up", "RecordInTransit", "in_transit"),
        ("exception", "RecordInTransit", "in_transit"),
        ("in_transit", "RecordDelivery", "delivered"),
        ("created", "VoidShipment", "voided"),
    ],
)
def test_registered_shipment_transitions(current: str, command: str, target: str) -> None:
    assert require_shipment_transition(current, command) == target


@pytest.mark.parametrize(
    ("current", "command"),
    [
        ("picked_up", "VoidShipment"),
        ("delivered", "RecordInTransit"),
        ("voided", "RecordPickup"),
        ("created", "RecordReturn"),
    ],
)
def test_illegal_shipment_transitions_are_rejected(current: str, command: str) -> None:
    with pytest.raises(ApplicationError) as error:
        require_shipment_transition(current, command)
    assert error.value.code == "SHIPMENT_STATE_CONFLICT"


def test_unregistered_logistics_provider_is_rejected() -> None:
    with pytest.raises(ApplicationError) as error:
        _provider("unknown_carrier")
    assert error.value.status == 422
    assert error.value.code == "SHIPMENT_CARRIER_UNSUPPORTED"


async def test_fake_express_is_excluded_from_timed_sync_candidates() -> None:
    captured: list[ClauseElement] = []

    class EmptyScalars:
        def all(self) -> list[Shipment]:
            return []

    class CapturingSession:
        async def scalars(self, statement: ClauseElement) -> EmptyScalars:
            captured.append(statement)
            return EmptyScalars()

    repository = LogisticsRepository(cast(AsyncSession, CapturingSession()))
    now = datetime(2026, 9, 13, 8, 0, 0)

    assert await repository.sync_candidates(now=now, stale_before=now, limit=20) == []
    assert len(captured) == 1
    sql = str(
        captured[0].compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "shipments.carrier_code != 'fake_express'" in sql
