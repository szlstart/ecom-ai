from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.exceptions import ApplicationError

ORDER_TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "RecordPaymentSucceeded": (frozenset({"pending_payment"}), "paid"),
    "InitializeFulfillment": (frozenset({"paid"}), "pending_shipment"),
    "CancelUnpaidOrder": (frozenset({"pending_payment"}), "cancelled"),
    "CloseBeforeShipment": (frozenset({"paid", "pending_shipment"}), "closed"),
    "RecordAllItemsShipped": (frozenset({"pending_shipment"}), "shipped"),
    "ReopenForShipmentCorrection": (frozenset({"shipped"}), "pending_shipment"),
    "ConfirmReceipt": (frozenset({"shipped"}), "completed"),
    "AutoConfirmReceipt": (frozenset({"shipped"}), "completed"),
    "CloseAbnormalFulfillment": (frozenset({"shipped"}), "closed"),
}

FULFILLMENT_TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "RecordPartialShipment": (frozenset({"unfulfilled"}), "partial"),
    "RecordAllItemsShipped": (frozenset({"unfulfilled", "partial"}), "shipped"),
    "ReopenPartialShipment": (frozenset({"shipped"}), "partial"),
    "ResetUnfulfilled": (frozenset({"partial", "shipped"}), "unfulfilled"),
    "ConfirmReceipt": (frozenset({"shipped"}), "received"),
    "AutoConfirmReceipt": (frozenset({"shipped"}), "received"),
}


@dataclass(frozen=True)
class OrderPolicySnapshot:
    order_status: str
    payment_status: str
    fulfillment_status: str
    after_sale_status: str
    paid_amount: int
    expires_at: datetime
    all_reviews_terminal: bool
    has_pending_review: bool
    has_after_sale_history: bool
    has_refundable_items: bool


def require_transition(
    registry: dict[str, tuple[frozenset[str], str]], current: str, command: str
) -> str:
    transition = registry.get(command)
    if transition is None or current not in transition[0]:
        raise ApplicationError(
            status=409,
            code="ORDER_STATE_CONFLICT",
            title="Order state conflict",
            detail=f"当前状态不允许执行 {command}。",
        )
    return transition[1]


def matched_views(snapshot: OrderPolicySnapshot, now: datetime) -> list[str]:
    result = ["all"]
    if snapshot.order_status == "pending_payment" and snapshot.expires_at > now:
        result.append("pending_payment")
    if snapshot.order_status == "pending_shipment":
        result.append("pending_shipment")
    if snapshot.order_status == "shipped" and snapshot.fulfillment_status != "received":
        result.append("in_transit")
    if snapshot.order_status == "completed":
        result.append("completed")
        if snapshot.has_pending_review:
            result.append("pending_review")
    if snapshot.has_after_sale_history:
        result.append("after_sale")
    if snapshot.order_status in {"cancelled", "closed"} and snapshot.paid_amount == 0:
        result.append("cancelled")
    return result


def can_hide(snapshot: OrderPolicySnapshot) -> bool:
    if snapshot.after_sale_status == "in_progress":
        return False
    if snapshot.order_status in {"cancelled", "closed"}:
        return True
    return snapshot.order_status == "completed" and snapshot.all_reviews_terminal


def available_action_codes(snapshot: OrderPolicySnapshot, now: datetime) -> list[str]:
    actions: list[str] = []
    if (
        snapshot.order_status == "pending_payment"
        and snapshot.payment_status == "unpaid"
        and snapshot.expires_at > now
    ):
        actions.extend(("pay", "cancel_order"))
    if snapshot.fulfillment_status in {"partial", "shipped", "received"}:
        actions.append("view_logistics")
    if snapshot.order_status == "shipped" and snapshot.fulfillment_status == "shipped":
        actions.append("confirm_receipt")
    if snapshot.has_after_sale_history:
        actions.append("view_after_sale")
    if (
        snapshot.payment_status in {"paid", "partially_refunded"}
        and snapshot.order_status not in {"cancelled", "closed"}
        and snapshot.has_refundable_items
    ):
        actions.append("apply_after_sale")
    if (
        snapshot.order_status == "completed"
        and snapshot.fulfillment_status == "received"
        and snapshot.has_pending_review
    ):
        actions.append("review")
    if can_hide(snapshot):
        actions.append("delete_order")
    if snapshot.order_status in {"completed", "cancelled", "closed"}:
        actions.append("repurchase")
    return actions
