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
