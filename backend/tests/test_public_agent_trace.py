from __future__ import annotations

from typing import cast

from app.modules.agent_runtime.public_trace import ensure_public_trace, public_trace


def test_public_trace_explains_question_actions_and_result_without_private_reasoning() -> None:
    trace = public_trace(
        run_id="run_01KPUBLICTRACE000000000000",
        agent="专属客服",
        model="kimi",
        question="帮我找三件适合画画的商品",
        intent="product_search",
        data={"items": [{"product_id": "prd_1"}, {"product_id": "prd_2"}]},
        steps=[
            {"kind": "plan", "label": "理解商品需求", "status": "completed"},
            {
                "kind": "tool",
                "label": "搜索在售商品",
                "tool_code": "catalog.search_products",
                "status": "completed",
            },
            {"kind": "answer", "label": "整理回复", "status": "completed"},
        ],
        source_ids=["product:prd_1", "product:prd_2"],
        tool_code="catalog.search_products",
    )

    assert trace["version"] == "public-agent-trace-v2"
    assert trace["question"] == "帮我找三件适合画画的商品"
    assert "全平台在售商品" in str(trace["analysis_summary"])
    assert "2 项可用结果" in str(trace["result_summary"])
    assert trace["raw_reasoning_exposed"] is False
    tool_step = cast(list[dict[str, object]], trace["steps"])[1]
    assert tool_step["result_count"] == 2
    assert "2 项可用结果" in str(tool_step["summary"])


def test_ensure_public_trace_upgrades_security_and_memory_responses() -> None:
    trace = ensure_public_trace(
        {
            "intent": "memory_candidate",
            "steps": [{"kind": "memory", "label": "创建候选记忆"}],
            "raw_private_reasoning": "must never be copied",
        },
        run_id="run_01KPUBLICTRACE000000000001",
        agent="专属客服",
        model="kimi",
        question="请记住我喜欢蓝色",
        data={},
    )

    assert trace["version"] == "public-agent-trace-v2"
    assert trace["question"] == "请记住我喜欢蓝色"
    assert trace["raw_reasoning_exposed"] is False
    assert "raw_private_reasoning" not in trace
