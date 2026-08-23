from app.core.exceptions import ApplicationError

SHIPMENT_TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "VoidShipment": (frozenset({"created"}), "voided"),
    "RecordPickup": (frozenset({"created"}), "picked_up"),
    "RecordInTransit": (frozenset({"picked_up", "exception"}), "in_transit"),
    "RecordDelivery": (
        frozenset({"picked_up", "in_transit", "exception"}),
        "delivered",
    ),
    "RecordException": (frozenset({"picked_up", "in_transit"}), "exception"),
    "RecordReturn": (
        frozenset({"picked_up", "in_transit", "exception"}),
        "returned",
    ),
    "CloseShipment": (
        frozenset({"created", "picked_up", "in_transit", "exception"}),
        "closed",
    ),
}


def require_shipment_transition(current: str, command: str) -> str:
    transition = SHIPMENT_TRANSITIONS.get(command)
    if transition is None or current not in transition[0]:
        raise ApplicationError(
            status=409,
            code="SHIPMENT_STATE_CONFLICT",
            title="Shipment state conflict",
            detail=f"当前包裹状态不允许执行 {command}。",
        )
    return transition[1]
