from types import SimpleNamespace
from typing import Any, cast

from app.modules.agent_runtime.context_window import ContextWindow, RecentTurn
from app.modules.agent_runtime.conversation_state import (
    ConversationStateSnapshot,
    _card_sets,
    _domain_for_intent,
    latest_card_set,
)


def test_typed_state_owns_latest_card_set_and_empty_boundary() -> None:
    state = ConversationStateSnapshot(
        topic_generation=4,
        source_sequence_no=18,
        payload={
            "active_domain": "catalog",
            "active_intent": "product_search",
            "card_sets": [
                {
                    "card_type": "product",
                    "card_set_id": "msg_old:product",
                    "cards": [{"product_id": "prd_old", "product_name": "旧商品"}],
                },
                {
                    "card_type": "product",
                    "card_set_id": "msg_new:product",
                    "cards": [],
                },
            ],
        },
    )

    assert latest_card_set(state, "product") == []
    projection = state.planning_projection()
    assert projection["topic_generation"] == 4
    assert projection["business_fact_authoritative"] is False


def test_context_places_typed_state_before_unstructured_history() -> None:
    state = ConversationStateSnapshot(
        topic_generation=2,
        source_sequence_no=7,
        payload={"active_domain": "order", "active_intent": "logistics_lookup"},
    )
    window = ContextWindow(
        recent_turns=(RecentTurn("msg_1", "用户", "旧对话"),),
        omitted_count=5,
        character_count=3,
    ).with_conversation_state(state)

    planning = window.planning_input("它到哪了")
    assert planning.index("TYPED_CONVERSATION_STATE_V2") < planning.index(
        "RECENT_UNTRUSTED_DIALOGUE"
    )
    assert '"active_intent":"logistics_lookup"' in planning


def test_ordinal_reference_can_return_to_prior_card_collection() -> None:
    state = ConversationStateSnapshot(
        topic_generation=3,
        source_sequence_no=12,
        payload={
            "card_sets": [
                {
                    "card_type": "product",
                    "cards": [
                        {"product_id": "prd_1"},
                        {"product_id": "prd_2"},
                        {"product_id": "prd_3"},
                    ],
                },
                {"card_type": "product", "cards": [{"product_id": "prd_2"}]},
            ]
        },
    )

    assert latest_card_set(state, "product") == [{"product_id": "prd_2"}]
    assert latest_card_set(state, "product", minimum_count=3) == [
        {"product_id": "prd_1"},
        {"product_id": "prd_2"},
        {"product_id": "prd_3"},
    ]


def test_operations_intents_keep_separate_typed_domains() -> None:
    assert _domain_for_intent("catalog", None) == "merchant_catalog"
    assert _domain_for_intent("inventory", None) == "merchant_inventory"
    assert _domain_for_intent("orders", None) == "operations_order"
    assert _domain_for_intent("users", None) == "governance_user"
    assert _domain_for_intent("stores", None) == "governance_store"
    assert _domain_for_intent("runtime", None) == "observability"


def test_operations_detail_cards_are_kept_as_typed_card_sets() -> None:
    response = SimpleNamespace(
        message_no="msg_ops",
        sequence_no=21,
        content_payload={
            "detail_cards": [
                {
                    "kind": "inventory_risk",
                    "title": "蓝色款库存不足",
                    "badge": "低库存",
                    "resource_id": "sku_1",
                },
                {
                    "kind": "merchant_product",
                    "title": "考试铅笔",
                    "product_id": "prd_1",
                },
            ]
        },
    )

    sets = _card_sets(cast(Any, response), "inventory", 4)

    assert [item["card_type"] for item in sets] == [
        "detail:inventory_risk",
        "detail:merchant_product",
    ]
    assert sets[0]["cards"] == [
        {
            "resource_id": "sku_1",
            "kind": "inventory_risk",
            "title": "蓝色款库存不足",
            "badge": "低库存",
        }
    ]
