from app.modules.agent_runtime.order_cards import (
    _message_order_cards,
    extreme_order_no_from_cards,
    order_reference_index,
    referenced_order_no,
    requests_direct_transaction_action,
)
from app.modules.agent_runtime.product_cards import (
    _mentioned_product_cards,
    is_short_affirmative,
    product_card_reference_index,
    product_card_reference_indices,
    referenced_product_card,
)
from app.modules.messaging.models import Message


def test_short_affirmative_requires_exact_acknowledgement() -> None:
    assert is_short_affirmative("好") is True
    assert is_short_affirmative("  OK  ") is True
    assert is_short_affirmative("好的，介绍第二个") is False


def test_product_card_reference_index_tracks_requested_collection_size() -> None:
    assert product_card_reference_index("接着看看第三个") == 2
    assert product_card_reference_index("这个怎么样") is None
    assert product_card_reference_indices("第二个和第三个有什么区别") == [1, 2]


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
    assert referenced_order_no("这笔能退款吗?", ["ord_ONLY"]) == "ord_ONLY"
    assert referenced_order_no("现在可以检查售后资格吗?不要提交", ["ord_ONLY"]) == "ord_ONLY"


def test_normalizes_agent_and_human_order_card_message_shapes() -> None:
    agent_message = Message(
        message_type="text",
        content_payload={"order_cards": [{"order_id": "ord_AGENT"}]},
    )
    human_message = Message(
        message_type="order_card",
        content_payload={"order_id": "ord_HUMAN", "items": []},
    )

    assert [card["order_id"] for card in _message_order_cards(agent_message)] == [
        "ord_AGENT"
    ]
    assert [card["order_id"] for card in _message_order_cards(human_message)] == [
        "ord_HUMAN"
    ]


def test_order_ordinal_resolution_respects_text_order_and_explicit_corrections() -> None:
    orders = ["ord_FIRST", "ord_SECOND"]

    assert order_reference_index("第二笔是哪一笔，并说明为什么上一轮仍显示第一笔") == 1
    assert referenced_order_no(
        "第二笔是哪一笔，并说明为什么上一轮仍显示第一笔", orders
    ) == "ord_SECOND"
    assert order_reference_index("不是第一笔，我说的是第二笔") == 1
    assert referenced_order_no("不是第一笔，我说的是第二笔", orders) == "ord_SECOND"


def test_resolves_unique_highest_amount_from_latest_visible_order_cards() -> None:
    cards: list[dict[str, object]] = [
        {
            "order_id": "ord_LOW",
            "payable_amount": {"minor_units": "700", "currency": "CNY"},
        },
        {
            "order_id": "ord_HIGH",
            "payable_amount": {"minor_units": "1910", "currency": "CNY"},
        },
    ]

    assert (
        extreme_order_no_from_cards("这些订单金额最高的是哪一笔?", cards)
        == "ord_HIGH"
    )


def test_direct_transaction_action_detection_requires_explicit_delegation() -> None:
    assert requests_direct_transaction_action("帮我付款") is True
    assert requests_direct_transaction_action("帮我直接付款") is True
    assert requests_direct_transaction_action("这个订单付款了吗") is False
    assert requests_direct_transaction_action("帮我用支付宝充值100元") is False
    assert requests_direct_transaction_action("帮我用支付宝付款") is True


def test_explicit_product_names_can_recover_multiple_older_cards() -> None:
    cards: list[dict[str, object]] = [
        {"product_id": "prd_PANTS", "product_name": "男士休闲长裤"},
        {"product_id": "prd_PENCIL", "product_name": "绿杆2B书写铅笔考试专用"},
        {"product_id": "prd_RULER", "product_name": "透明亚克力直尺15厘米"},
        {"product_id": "prd_OTHER", "product_name": "普通绘图铅笔"},
    ]

    matched = _mentioned_product_cards("比较刚才的2B铅笔和15厘米直尺", cards)

    assert [card["product_id"] for card in matched[:2]] == ["prd_PENCIL", "prd_RULER"]
