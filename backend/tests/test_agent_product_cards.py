from app.modules.agent_runtime.order_cards import (
    referenced_order_no,
    requests_direct_transaction_action,
)
from app.modules.agent_runtime.product_cards import (
    is_short_affirmative,
    product_card_reference_index,
    referenced_product_card,
)


def test_short_affirmative_requires_exact_acknowledgement() -> None:
    assert is_short_affirmative("好") is True
    assert is_short_affirmative("  OK  ") is True
    assert is_short_affirmative("好的，介绍第二个") is False


def test_product_card_reference_index_tracks_requested_collection_size() -> None:
    assert product_card_reference_index("接着看看第三个") == 2
    assert product_card_reference_index("这个怎么样") is None


def test_resolves_ordinal_and_single_card_follow_ups() -> None:
    cards: list[dict[str, object]] = [
        {"product_id": "prd_ONE", "product_name": "考试直尺"},
        {"product_id": "prd_TWO", "product_name": "涂卡铅笔"},
    ]

    assert referenced_product_card("第二个适合考试吗", cards) == cards[1]
    assert referenced_product_card("第三个呢", cards) is None
    assert referenced_product_card("它还有库存吗", cards) is None
    assert referenced_product_card("好，继续", [cards[0]]) == cards[0]


def test_resolves_unique_natural_product_noun_but_never_guesses_ambiguous_cards() -> None:
    cards: list[dict[str, object]] = [
        {"product_id": "prd_RULER", "product_name": "透明亚克力双色直尺15cm"},
        {"product_id": "prd_KNIFE", "product_name": "铝合金小号美工刀"},
    ]
    duplicate: list[dict[str, object]] = [
        {"product_id": "prd_A", "product_name": "透明直尺15cm"},
        {"product_id": "prd_B", "product_name": "木质直尺20cm"},
    ]

    assert referenced_product_card("这把直尺适合考试吗?", cards) == cards[0]
    assert referenced_product_card("直尺还有库存吗?", duplicate) is None


def test_resolves_order_ordinals_but_does_not_guess_between_multiple_orders() -> None:
    orders = ["ord_FIRST", "ord_SECOND"]

    assert referenced_order_no("第一笔订单的物流到哪了?", orders) == "ord_FIRST"
    assert referenced_order_no("这个订单到哪了?", orders) is None
    assert referenced_order_no("这个订单到哪了?", ["ord_ONLY"]) == "ord_ONLY"


def test_direct_transaction_action_detection_requires_explicit_delegation() -> None:
    assert requests_direct_transaction_action("帮我付款") is True
    assert requests_direct_transaction_action("帮我直接付款") is True
    assert requests_direct_transaction_action("这个订单付款了吗") is False
