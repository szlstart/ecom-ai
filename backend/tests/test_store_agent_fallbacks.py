from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent_runtime.checkpoints import AgentCheckpointStore
from app.modules.agent_runtime.model_gateway import (
    DeterministicStoreModelGateway,
    StoreAgentPlan,
    StoreSupervisorPlan,
    StoreSupervisorSubtask,
)
from app.modules.agent_runtime.store_agent import (
    _attach_product_fulfillment_facts,
    _attach_store_knowledge,
    _conditional_recommendation_result,
    _merge_store_supervisor_plans,
    _normalize_store_supervisor_plan_for_request,
    _render,
    _requested_store_cart_quantity,
    _store_detail_cards,
)
from app.modules.agent_runtime.store_context import TrustedStoreAgentContext
from app.modules.agent_runtime.store_tools import StoreToolResult, _matches_recommendation_scenario
from app.modules.knowledge.service import KnowledgeService


async def test_store_supervisor_preserves_product_and_inventory_goals() -> None:
    plan = await DeterministicStoreModelGateway().plan_tasks("这件衣服最大码还有货吗,付款后多久发?")

    assert [task.intent for task in plan.tasks] == ["product_qa", "inventory_lookup"]


async def test_store_supervisor_preserves_handoff_and_business_goal() -> None:
    plan = await DeterministicStoreModelGateway().plan_tasks(
        "先告诉我这款还有没有库存，然后帮我转人工"
    )

    assert [task.intent for task in plan.tasks] == ["human_handoff", "inventory_lookup"]


async def test_store_cart_add_is_scoped_as_one_reversible_write() -> None:
    plan = await DeterministicStoreModelGateway().plan_tasks("把刚才第二件的10支款加入购物车2件")

    assert [task.intent for task in plan.tasks] == ["cart_add"]
    assert plan.tasks[0].objective == "将用户明确选择的本店商品款式加入本人购物车"
    assert _requested_store_cart_quantity("加入购物车2件") == 2


async def test_store_cart_add_understands_natural_possessive_phrasing() -> None:
    gateway = DeterministicStoreModelGateway()

    for text in (
        "把刚才的8支款加入我的购物车，只加1件",
        "把它放到我购物车",
        "请加进本人购物车",
    ):
        assert (await gateway.plan(text)).intent == "cart_add"


def test_store_recommendation_does_not_fill_exam_results_with_utility_knives() -> None:
    query = "推荐3件适合考试使用的本店文具"

    assert _matches_recommendation_scenario(query, "2B考试铅笔", None)
    assert _matches_recommendation_scenario(query, "透明直尺15cm", None)
    assert not _matches_recommendation_scenario(query, "办公铝合金美工刀裁纸刀", None)


def test_store_policy_uses_platform_after_sale_fallback_when_store_has_no_override() -> None:
    data = {
        "items": [],
        "knowledge_sources": [
            {
                "document_id": "kdoc_order",
                "title": "[系统] 购物与订单规则",
                "excerpt": "商品价格和库存以结算页实时结果为准。",
                "scope": "platform:platform",
            },
            {
                "document_id": "kdoc_after_sale",
                "title": "[系统] 售后、退款与客服规则",
                "excerpt": (
                    "商品存在质量问题时，用户可从对应订单的售后入口发起申请。"
                    "是否支持退货或退款需要结合订单状态和当前规则检查。"
                ),
                "scope": "platform:platform",
            },
        ],
    }

    answer = _render(
        StoreAgentPlan("policy_qa"),
        data,
        "你们店的退换货政策是什么？商品有质量问题怎么办？",
    )
    cards = _store_detail_cards(
        StoreAgentPlan("policy_qa"),
        data,
        "你们店的退换货政策是什么？商品有质量问题怎么办？",
    )

    assert "商品存在质量问题" in answer
    assert cards[0]["rows"][0]["value"] == "从对应订单发起售后"
    assert cards[0]["rows"][1]["value"] == "提交前实时检查"
    assert len(cards[0]["rows"]) == 2
    assert "优先使用本店公开政策" in cards[0]["summary"]


@pytest.mark.asyncio
async def test_store_rag_failure_is_explicit_and_keeps_business_answer_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rolled_back = False

    async def rollback() -> None:
        nonlocal rolled_back
        rolled_back = True

    async def fail_search(_service: KnowledgeService, **_kwargs: object) -> object:
        raise SQLAlchemyError("simulated retrieval outage")

    monkeypatch.setattr(KnowledgeService, "search_for_agent", fail_search)
    postgres = cast(AsyncSession, SimpleNamespace(rollback=rollback))
    checkpoint = cast(AgentCheckpointStore, SimpleNamespace(session=postgres))
    context = cast(
        TrustedStoreAgentContext,
        SimpleNamespace(
            run=SimpleNamespace(current_phase="executing", version=0, trace_id="trc_test"),
            trigger=SimpleNamespace(text_content="你们店支持退换货吗"),
            store=SimpleNamespace(store_no="sto_test"),
        ),
    )
    data: dict[str, object] = {
        "items": [{"policy_id": "pol_local", "title": "本店售后", "content": "支持售后申请"}]
    }

    await _attach_store_knowledge(
        cast(AsyncSession, SimpleNamespace()), checkpoint, context, "policy_qa", data
    )

    assert rolled_back is True
    assert data["items"]
    assert data["rag"] == {
        "scope": "store:sto_test",
        "returned_count": 0,
        "degraded": True,
        "error_code": "RAG_RETRIEVAL_UNAVAILABLE",
    }


async def test_maximum_package_price_and_stock_use_one_sku_tool() -> None:
    deterministic = await DeterministicStoreModelGateway().plan_tasks(
        "这款最大规格是什么，哪种还有货，多少钱?"
    )
    normalized = _normalize_store_supervisor_plan_for_request(
        deterministic,
        "这款最大规格是什么，哪种还有货，多少钱?",
    )

    assert [task.intent for task in normalized.tasks] == ["sku_compare"]
    assert "最大规格" in normalized.tasks[0].objective


def test_provider_goal_plan_is_not_rewritten_by_local_keyword_rules() -> None:
    plan = StoreSupervisorPlan(
        (
            StoreSupervisorSubtask("task_1", "product_recommend", "推荐三件考试文具"),
            StoreSupervisorSubtask("task_2", "inventory_lookup", "核对实时库存"),
        ),
        confidence=0.9,
    )

    normalized = _normalize_store_supervisor_plan_for_request(
        plan,
        "推荐三件适合考试、目前有现货的本店文具",
    )

    assert [task.intent for task in normalized.tasks] == [
        "product_recommend",
        "inventory_lookup",
    ]


def test_conditional_recommendation_is_skipped_when_named_sku_is_available() -> None:
    inventory_task = StoreSupervisorSubtask("task_1", "inventory_lookup", "查询 8支 库存")
    recommendation_task = StoreSupervisorSubtask(
        "task_2", "product_recommend", "如果8支缺货，推荐本店另一个考试文具"
    )
    result = _conditional_recommendation_result(
        recommendation_task,
        [
            (
                inventory_task,
                StoreAgentPlan("inventory_lookup"),
                StoreToolResult(
                    "succeeded",
                    {"items": [{"sku_name": "8支", "available_quantity": 99}]},
                ),
            )
        ],
    )

    assert result is not None
    assert result.data["conditional_skipped"] is True
    assert "目前有货" in _render(StoreAgentPlan("product_recommend"), result.data)


def test_product_review_question_uses_compact_review_summary_card() -> None:
    data = {
        "product_id": "prd_demo",
        "name": "考试用铅笔",
        "review_summary": {
            "rating_score": "4.80",
            "review_count": 12,
            "samples": [{"rating": 5, "content": "书写顺滑，考试使用方便。"}],
        },
    }

    answer = _render(StoreAgentPlan("product_qa"), data, "这件评价怎么样")
    cards = _store_detail_cards(StoreAgentPlan("product_qa"), data, "口碑如何")

    assert "12 条公开评价" in answer
    assert "4.80 分" in answer
    assert cards[0]["kind"] == "review_summary"
    assert cards[0]["rows"][0]["value"] == "书写顺滑，考试使用方便。"


def test_valid_provider_store_plan_is_authoritative_over_keyword_fallback() -> None:
    merged = _merge_store_supervisor_plans(
        StoreSupervisorPlan(
            (StoreSupervisorSubtask("task_1", "inventory_lookup", "查询实时库存"),),
            confidence=0.9,
        ),
        StoreSupervisorPlan(
            (
                StoreSupervisorSubtask("task_1", "product_qa", "查询商品详情和发货承诺"),
                StoreSupervisorSubtask("task_2", "inventory_lookup", "查询实时库存"),
            )
        ),
    )

    assert [task.intent for task in merged.tasks] == ["inventory_lookup"]


def test_product_fulfillment_exposes_requested_dispatch_origin() -> None:
    data: dict[str, Any] = {
        "product_id": "prd_demo",
        "name": "考试铅笔",
        "safe_detail_text": "付款后2天内发货",
        "attributes": [{"name": "发货地", "value": "江西省"}],
    }

    _attach_product_fulfillment_facts(data, "付款后多久发，从哪里发?")
    cards = _store_detail_cards(StoreAgentPlan("product_qa"), data)

    assert data["product_fulfillment_facts"]["dispatch_origin"] == "江西省"
    assert any(row["label"] == "发货地" and row["value"] == "江西省" for row in cards[0]["rows"])


def test_order_fallback_localizes_status_actions_and_money() -> None:
    answer = _render(
        StoreAgentPlan("order_explain"),
        {
            "status": {
                "order": "shipped",
                "payment": "paid",
                "fulfillment": "shipped",
                "after_sale": "none",
            },
            "amounts": {"paid": {"minor_units": "600", "currency": "CNY"}},
            "available_actions": [
                "view_logistics",
                "confirm_receipt",
                "apply_after_sale",
            ],
            "shipments": [{}],
        },
    )

    assert "订单运输中" in answer
    assert "支付已支付" in answer
    assert "履约已发货" in answer
    assert "售后无进行中售后" in answer
    assert "¥6.00" in answer
    assert "查看物流、确认收货、申请售后" in answer
    for internal_code in (
        "shipped",
        "paid",
        "none",
        "view_logistics",
        "confirm_receipt",
        "apply_after_sale",
    ):
        assert internal_code not in answer


def test_product_fallback_continues_after_affirmative_short_reply() -> None:
    answer = _render(
        StoreAgentPlan("product_qa"),
        {
            "name": "通勤阔腿裤",
            "conversation_window": {
                "recent_turns": [{"role": "AI客服", "text": "我可以继续帮你看尺码、库存或发货。"}]
            },
        },
        "好",
    )

    assert "接着看“通勤阔腿裤”" in answer
    assert "款式和尺码" in answer
    assert "你好，我是本店智能客服" not in answer


def test_store_after_sale_progress_is_a_card_first_scoped_result() -> None:
    data = {
        "items": [
            {
                "refund_id": "ref_demo",
                "product_name": "考试铅笔",
                "refund_status": "merchant_review",
                "requested_amount": {"minor_units": 600, "currency": "CNY"},
                "reason_detail": "商品破损",
                "submitted_at": "2026-09-14T01:00:00",
            }
        ],
        "presentation": "after_sale_cards",
    }

    answer = _render(StoreAgentPlan("after_sale_progress"), data)
    cards = _store_detail_cards(StoreAgentPlan("after_sale_progress"), data)

    assert "1 笔售后申请" in answer
    assert cards[0]["kind"] == "store_after_sale_progress"
    assert cards[0]["badge"] == "商家处理中"
    assert cards[0]["rows"][0]["value"] == "¥6.00"
