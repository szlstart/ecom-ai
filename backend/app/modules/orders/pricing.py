from __future__ import annotations

from dataclasses import dataclass

from app.core.exceptions import ApplicationError


@dataclass(frozen=True)
class AdjustmentItem:
    item_id: int
    gross_amount: int
    current_payable_amount: int


def allocate_adjustment(items: list[AdjustmentItem], adjustment_delta: int) -> dict[int, int]:
    """Allocate an order-level integer minor-unit adjustment deterministically.

    Positive surcharges use gross amount as weight. Discounts use the current payable
    amount, which guarantees that a valid order-level discount cannot make an item negative.
    Equal remainders are resolved by stable item ID.
    """
    if not items:
        raise ValueError("at least one order item is required")
    if len({item.item_id for item in items}) != len(items):
        raise ValueError("order item IDs must be unique")
    if any(item.gross_amount < 0 or item.current_payable_amount < 0 for item in items):
        raise ValueError("amounts must be non-negative")
    if adjustment_delta == 0:
        return {item.item_id: 0 for item in items}
    current_total = sum(item.current_payable_amount for item in items)
    if current_total + adjustment_delta < 0:
        raise ApplicationError(
            status=409,
            code="ORDER_ADJUSTMENT_EXCEEDS_PAYABLE",
            title="Order adjustment exceeds payable amount",
            detail="订单优惠金额不能超过当前应付金额。",
        )
    magnitude = abs(adjustment_delta)
    weights = [
        item.gross_amount if adjustment_delta > 0 else item.current_payable_amount for item in items
    ]
    weight_total = sum(weights)
    if weight_total == 0:
        if adjustment_delta < 0:
            raise ApplicationError(
                status=409,
                code="ORDER_ADJUSTMENT_EXCEEDS_PAYABLE",
                title="Order adjustment exceeds payable amount",
                detail="零金额订单不能继续优惠。",
            )
        weights = [1] * len(items)
        weight_total = len(items)
    bases = [(magnitude * weight) // weight_total for weight in weights]
    remainders = [(magnitude * weight) % weight_total for weight in weights]
    remaining = magnitude - sum(bases)
    ranked = sorted(range(len(items)), key=lambda index: (-remainders[index], items[index].item_id))
    for index in ranked[:remaining]:
        bases[index] += 1
    sign = 1 if adjustment_delta > 0 else -1
    result = {item.item_id: sign * bases[index] for index, item in enumerate(items)}
    if sum(result.values()) != adjustment_delta:
        raise RuntimeError("adjustment allocation invariant violated")
    if any(item.current_payable_amount + result[item.item_id] < 0 for item in items):
        raise RuntimeError("adjustment produced a negative item payable amount")
    return result
