import pytest

from app.core.exceptions import ApplicationError
from app.modules.logistics.domain import require_shipment_transition
from app.modules.logistics.service import _provider


@pytest.mark.parametrize(
    ("current", "command", "target"),
    [
        ("created", "RecordPickup", "picked_up"),
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
        ("created", "RecordDelivery"),
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
