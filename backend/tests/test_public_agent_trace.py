from __future__ import annotations

from typing import cast

from app.modules.agent_runtime.public_trace import ensure_public_trace, public_trace, result_count


def test_result_count_does_not_hide_rag_sources_behind_an_empty_items_list() -> None:
    assert result_count({"items": [], "knowledge_sources": [{"document_id": "kdoc_1"}]}) == 1
    assert result_count({"compound_results": {"orders": {}, "wallet": {}, "address": {}}}) == 3
    assert result_count({"specialists": {"catalog": {}, "orders": {}, "policy": {}}}) == 3


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

    assert trace["version"] == "auditable-agent-trace-v3"
    assert trace["question"] == "帮我找三件适合画画的商品"
    assert "全平台在售商品" in str(trace["analysis_summary"])
    assert "2 项可用结果" in str(trace["result_summary"])
    assert trace["raw_reasoning_exposed"] is False
    details = cast(list[str], trace["analysis_details"])
    assert any("catalog.search_products" in item for item in details)
    assert any("返回 2 项可用结果" in item for item in details)
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

    assert trace["version"] == "auditable-agent-trace-v3"
    assert trace["question"] == "请记住我喜欢蓝色"
    assert trace["raw_reasoning_exposed"] is False


def test_ensure_public_trace_preserves_safe_model_observability_metrics() -> None:
    trace = ensure_public_trace(
        {
            "intent": "general_chat",
            "grounding_verified": True,
            "evidence_truncated": True,
            "truncated_evidence_fields": ["orders"],
            "model_invocation": {
                "model": "gpt-test",
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
                "estimated_cost_usd": None,
                "cost_status": "unknown",
            },
        },
        run_id="run-1",
        agent="exclusive",
        model="gpt-test",
        question="你好",
        data={},
    )

    assert trace["grounding_verified"] is True
    assert trace["evidence_truncated"] is True
    assert trace["truncated_evidence_fields"] == ["orders"]
    assert trace["model_invocation"] == {
        "model": "gpt-test",
        "input_tokens": 12,
        "output_tokens": 4,
        "total_tokens": 16,
        "estimated_cost_usd": None,
        "cost_status": "unknown",
    }


def test_public_trace_explains_supervisor_and_specialist_delegations() -> None:
    trace = public_trace(
        run_id="run_multi",
        agent="AI 管家",
        model="kimi",
        question="请综合分析用户、店铺和订单",
        intent="complex_platform_diagnosis",
        data={"specialists": [{"status": "succeeded"}]},
        steps=[
            {
                "kind": "supervisor",
                "label": "并行委派领域助手",
                "status": "completed",
                "delegation_count": 3,
            },
            {
                "kind": "delegation",
                "label": "订单与履约 Agent",
                "status": "succeeded",
                "specialist": "governance_orders",
                "tool_code": "governance.order_summary",
                "tool_calls": 1,
                "latency_ms": 42,
            },
        ],
        extra={
            "planning_source": "provider_model_supervisor",
            "goal_ledger": [
                {
                    "goal_key": "goal_1",
                    "description": "分析用户、店铺和订单",
                    "assigned_task_key": "task_1",
                }
            ],
            "coverage_complete": True,
            "subtasks": [
                {
                    "subtask_key": "task_1",
                    "specialist": "治理诊断 Agent",
                    "intent": "complex_platform_diagnosis",
                }
            ],
        },
    )

    details = cast(list[str], trace["analysis_details"])
    assert "3 个相互隔离的专业子任务" in details[0]
    assert "governance_orders" in details[1]
    assert "1 次受控工具调用" in details[1]
    assert "governance.order_summary" in details[1]
    assert "42 毫秒" in details[1]
    assert "raw_private_reasoning" not in trace
    orchestration = cast(dict[str, object], trace["orchestration_trace"])
    assert orchestration["planning_source"] == "provider_model_supervisor"
    assert orchestration["coverage_complete"] is True
    assert orchestration["goal_ledger"] == [
        {
            "goal_key": "goal_1",
            "description": "分析用户、店铺和订单",
            "assigned_task_key": "task_1",
        }
    ]


def test_auditable_trace_includes_tool_arguments_results_context_rag_and_memory() -> None:
    trace = public_trace(
        run_id="run_audit",
        agent="专属客服 Supervisor Agent",
        model="gpt-test",
        question="结合偏好查规则",
        intent="personalized_recommendation",
        data={
            "_audit_tool_calls": [
                {
                    "tool_code": "catalog.search_products",
                    "arguments": {"query": "蓝色文具", "api_key": "must-not-leak"},
                    "status": "succeeded",
                    "result": {"items": [{"product_id": "prd_1"}]},
                    "result_count": 1,
                    "latency_ms": 17,
                }
            ],
            "conversation_window": {"included_count": 2, "recent_turns": []},
            "rag": {"scope": "platform:platform", "retrieval_mode": "hybrid"},
            "knowledge_sources": [
                {"document_id": "kdoc_1", "title": "平台规则", "score": 0.92}
            ],
            "memory": {"scope": "exclusive", "authorized": True, "used_count": 1},
            "recalled_memories": [
                {"memory_id": "mem_1", "value": "喜欢蓝色", "relevance": 0.88}
            ],
        },
        steps=[
            {
                "kind": "tool",
                "label": "搜索商品",
                "tool_code": "catalog.search_products",
                "status": "succeeded",
            }
        ],
        source_ids=["tool:catalog.search_products", "knowledge:kdoc_1", "memory:mem_1"],
        tool_code="catalog.search_products",
    )

    calls = cast(list[dict[str, object]], trace["tool_calls"])
    assert calls[0]["arguments"] == {
        "query": "蓝色文具",
        "api_key": "[受保护值未进入消息轨迹]",
    }
    context = cast(dict[str, object], trace["context_trace"])
    assert context["status"] == "read"
    assert cast(dict[str, object], context["window"])["included_count"] == 2
    assert cast(dict[str, object], trace["knowledge_trace"])["matches"]
    assert cast(dict[str, object], trace["memory_trace"])["items"]
    step = cast(list[dict[str, object]], trace["steps"])[0]
    assert cast(dict[str, object], step["tool_call"])["latency_ms"] == 17


def test_auditable_trace_explicitly_records_components_not_invoked() -> None:
    trace = public_trace(
        run_id="run_skipped",
        agent="专属客服",
        model="gpt-test",
        question="查询购物车",
        intent="cart_lookup",
        data={},
        steps=[],
    )

    assert cast(dict[str, object], trace["knowledge_trace"])["status"] == "not_invoked"
    assert cast(dict[str, object], trace["memory_trace"])["status"] == "not_invoked"
    assert cast(dict[str, object], trace["context_trace"])["status"] == "not_recorded"
    assert cast(dict[str, object], trace["model_invocation"])["provider_request_sent"] is False
