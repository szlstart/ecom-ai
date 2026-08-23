import pytest

from app.core.exceptions import ApplicationError
from app.modules.logistics.domain import require_shipment_transition


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
