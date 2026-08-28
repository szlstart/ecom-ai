from datetime import timedelta

import pytest

from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.modules.logistics.domain import require_shipment_transition
from app.modules.logistics.service import _provider, _simulated_tracking_snapshot


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


@pytest.mark.parametrize(
    ("elapsed_seconds", "expected_stages"),
    [
        (4, []),
        (5, ["WAITING_PICKUP"]),
        (10, ["WAITING_PICKUP", "PICKED_UP"]),
        (15, ["WAITING_PICKUP", "PICKED_UP", "OUT_FOR_DELIVERY"]),
        (
            20,
            ["WAITING_PICKUP", "PICKED_UP", "OUT_FOR_DELIVERY", "DELIVERED"],
        ),
    ],
)
def test_simulated_tracking_advances_in_five_second_milestones(
    elapsed_seconds: int, expected_stages: list[str]
) -> None:
    started_at = utc_now().replace(microsecond=0)
    snapshot = _simulated_tracking_snapshot(
        shipment_no="shp_01TEST",
        tracking_no="ECOMTEST123456",
        started_at=started_at,
        now=started_at + timedelta(seconds=elapsed_seconds),
        origin_region_code="310000",
        destination_district_code="440106",
        destination_address="体育西路 1 号",
    )

    assert [track.provider_status for track in snapshot.tracks] == expected_stages
    if elapsed_seconds >= 10:
        assert snapshot.tracks[1].location_text == "310000"
    if elapsed_seconds >= 15:
        assert snapshot.tracks[2].location_text == "440106"
    if elapsed_seconds >= 20:
        assert snapshot.tracks[3].location_text == "体育西路 1 号"
