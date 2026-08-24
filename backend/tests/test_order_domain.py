from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.modules.orders.domain import (
    FULFILLMENT_TRANSITIONS,
    ORDER_TRANSITIONS,
    OrderPolicySnapshot,
    available_action_codes,
    can_hide,
    matched_views,
    require_transition,
)
from app.modules.orders.pricing import AdjustmentItem, allocate_adjustment


def _load_domain_registry() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "docs" / "domain_registry.yaml"
    with path.open(encoding="utf-8") as registry_file:
        value = yaml.safe_load(registry_file)
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _snapshot(**overrides: object) -> OrderPolicySnapshot:
    values: dict[str, object] = {
        "order_status": "pending_payment",
        "payment_status": "unpaid",
        "fulfillment_status": "unfulfilled",
        "after_sale_status": "none",
        "paid_amount": 0,
        "expires_at": utc_now() + timedelta(minutes=10),
        "all_reviews_terminal": False,
        "has_pending_review": False,
        "has_after_sale_history": False,
        "has_refundable_items": False,
    }
    values.update(overrides)
    return OrderPolicySnapshot(**values)  # type: ignore[arg-type]


def test_order_transition_maps_match_normative_registry() -> None:
    registry = _load_domain_registry()["aggregates"]
    for aggregate, implementation in (
        ("order", ORDER_TRANSITIONS),
        ("fulfillment", FULFILLMENT_TRANSITIONS),
    ):
        expected = {
            item["command"]: (frozenset(item["from"]), item["to"])
            for item in registry[aggregate]["transitions"]
        }
        assert implementation == expected


def test_every_illegal_order_transition_is_rejected_without_target() -> None:
    states = {
        state for sources, target in ORDER_TRANSITIONS.values() for state in (*sources, target)
    }
    for command, (sources, target) in ORDER_TRANSITIONS.items():
        for state in states:
            if state in sources:
                assert require_transition(ORDER_TRANSITIONS, state, command) == target
            else:
                with pytest.raises(ApplicationError) as error:
                    require_transition(ORDER_TRANSITIONS, state, command)
                assert error.value.code == "ORDER_STATE_CONFLICT"


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (_snapshot(), ["all", "pending_payment"]),
        (_snapshot(order_status="pending_shipment"), ["all", "pending_shipment"]),
        (_snapshot(order_status="shipped", fulfillment_status="shipped"), ["all", "in_transit"]),
        (_snapshot(order_status="completed"), ["all", "completed"]),
        (
            _snapshot(
                order_status="completed",
                has_pending_review=True,
                has_after_sale_history=True,
            ),
            ["all", "completed", "pending_review", "after_sale"],
        ),
        (_snapshot(order_status="cancelled"), ["all", "cancelled"]),
        (_snapshot(order_status="closed", paid_amount=100), ["all"]),
    ],
)
def test_composite_order_views(snapshot: OrderPolicySnapshot, expected: list[str]) -> None:
    assert matched_views(snapshot, utc_now()) == expected


def test_available_actions_share_the_hide_policy() -> None:
    pending = _snapshot()
    assert available_action_codes(pending, utc_now()) == ["pay", "cancel_order"]
    processing = _snapshot(payment_status="processing")
    assert available_action_codes(processing, utc_now()) == []
    completed_pending_review = _snapshot(
        order_status="completed",
        fulfillment_status="received",
        has_pending_review=True,
    )
    assert can_hide(completed_pending_review) is False
    assert available_action_codes(completed_pending_review, utc_now()) == [
        "view_logistics",
        "review",
        "repurchase",
    ]
    completed_reviewed = _snapshot(order_status="completed", all_reviews_terminal=True)
    assert available_action_codes(completed_reviewed, utc_now()) == [
        "delete_order",
        "repurchase",
    ]
    partially_shipped = _snapshot(order_status="pending_shipment", fulfillment_status="partial")
    assert available_action_codes(partially_shipped, utc_now()) == ["view_logistics"]
    in_transit = _snapshot(order_status="shipped", fulfillment_status="shipped")
    assert available_action_codes(in_transit, utc_now()) == [
        "view_logistics",
        "confirm_receipt",
    ]


def test_adjustment_allocator_preserves_minor_units_and_is_deterministic() -> None:
    items = [
        AdjustmentItem(20, 100, 100),
        AdjustmentItem(10, 100, 100),
        AdjustmentItem(30, 300, 300),
    ]
    assert allocate_adjustment(items, 7) == {20: 1, 10: 2, 30: 4}
    discount = allocate_adjustment(items, -499)
    assert sum(discount.values()) == -499
    assert all(item.current_payable_amount + discount[item.item_id] >= 0 for item in items)
    assert allocate_adjustment(list(reversed(items)), 7) == {30: 4, 10: 2, 20: 1}


def test_adjustment_allocator_rejects_over_discount() -> None:
    with pytest.raises(ApplicationError) as error:
        allocate_adjustment([AdjustmentItem(1, 100, 100)], -101)
    assert error.value.code == "ORDER_ADJUSTMENT_EXCEEDS_PAYABLE"
