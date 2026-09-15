from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.modules.agent_runtime.prompt_safety import safe_untrusted_excerpt

_INTENT_LABELS = {
    "general_chat": "理解日常咨询并确认可服务范围",
    "product_qa": "核对当前商品信息",
    "sku_compare": "比较商品款式与规格",
    "inventory_lookup": "查询当前可售库存",
    "policy_qa": "检索适用的服务政策",
    "order_explain": "查询并解释当前订单",
    "product_recommend": "筛选店内商品候选",
    "product_search": "搜索全平台在售商品",
    "product_compare": "对比已选商品",
    "cart_lookup": "读取我的购物车",
    "cart_add": "把已选商品加入我的购物车",
    "cart_update": "修改购物车商品数量",
    "cart_remove": "移除购物车中的一件商品",
    "cart_clear": "准备清空我的购物车",
    "checkout_preview": "校验购物车并生成结算预览",
    "address_lookup": "读取我的收货地址",
    "wallet_lookup": "读取我的账户余额",
    "favorites_lookup": "读取我的商品与店铺收藏",
    "favorite_update": "修改我的商品或店铺收藏",
    "review_draft": "为待评价订单整理评价草稿",
    "memory_lookup": "读取我已确认的购物偏好",
    "memory_candidate": "整理待确认的购物偏好",
    "personalized_recommendation": "结合已授权偏好筛选商品",
    "order_lookup": "查询本人订单",
    "logistics_lookup": "查询订单物流",
    "refund_precheck": "只读检查售后资格",
    "refund_eligibility": "检查售后资格并准备草稿",
    "refund_progress": "查询售后处理进度",
    "human_handoff": "识别人工服务请求",
    "compound_request": "拆分并协同处理多项请求",
    "overview": "分析当前经营概览",
    "catalog": "分析商品运营情况",
    "orders": "分析订单与履约情况",
    "inventory": "分析库存风险",
    "users": "分析平台用户情况",
    "stores": "分析平台店铺情况",
    "runtime": "分析系统与 Agent 运行情况",
    "complex_platform_diagnosis": "拆解跨领域平台诊断任务",
    "complex_store_diagnosis": "拆解本店经营与服务诊断任务",
    "security_refusal": "识别并阻断不安全请求",
}


def public_question(value: object) -> str:
    """Return a short, redacted question suitable for the public audit panel."""

    return safe_untrusted_excerpt(value, 360).strip() or "本次会话消息"


def result_count(data: Mapping[str, Any]) -> int:
    counts: list[int] = []
    compound = data.get("compound_results")
    if isinstance(compound, Mapping):
        counts.append(len(compound))
    for key in ("items", "specialists", "knowledge_sources", "shipments"):
        value = data.get(key)
        if isinstance(value, list):
            counts.append(len(value))
        elif isinstance(value, Mapping):
            counts.append(len(value))
    if counts:
        return max(counts)
    return 1 if data else 0


_PROTECTED_AUDIT_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "cookie",
    "password",
    "refresh_token",
    "secret",
    "token",
}


def audit_projection(value: object, *, depth: int = 0) -> object:
    """Build a bounded, JSON-compatible snapshot for the developer execution trace.

    Agent tool arguments and results are already scoped by the server.  This projection
    preserves those business values while preventing credentials and unbounded payloads
    from being copied into every chat message.
    """

    if depth >= 8:
        return "[达到展示深度上限]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        return safe_untrusted_excerpt(value, 1_000)
    if isinstance(value, Mapping):
        projected: dict[str, object] = {}
        for raw_key, raw_value in list(value.items())[:60]:
            key = str(raw_key)
            normalized = key.casefold()
            if normalized in _PROTECTED_AUDIT_KEYS or any(
                marker in normalized for marker in ("credential", "private_key")
            ):
                projected[key] = "[受保护值未进入消息轨迹]"
            else:
                projected[key] = audit_projection(raw_value, depth=depth + 1)
        if len(value) > 60:
            projected["_omitted_fields"] = len(value) - 60
        return projected
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        projected_items = [audit_projection(item, depth=depth + 1) for item in value[:20]]
        if len(value) > 20:
            projected_items.append({"_omitted_items": len(value) - 20})
        return projected_items
    return safe_untrusted_excerpt(str(value), 1_000)


def public_trace(
    *,
    run_id: str,
    agent: str,
    model: str,
    question: object,
    intent: str,
    data: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    source_ids: Sequence[str] = (),
    tool_code: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    count = result_count(data)
    intent_label = _INTENT_LABELS.get(intent, "理解问题并限定处理范围")
    enriched_steps = [_enrich_step(step, data, count) for step in steps]
    tool_calls = _tool_calls(data) or _derived_tool_calls(enriched_steps, count, data)
    enriched_steps = _attach_tool_calls(enriched_steps, tool_calls)
    scope = "授权范围内的业务数据" if tool_code and tool_code != "none" else "当前会话与服务范围"
    analysis_details = [_analysis_detail(step, count) for step in enriched_steps]
    trace: dict[str, object] = {
        "version": "auditable-agent-trace-v3",
        "run_id": run_id,
        "agent": agent,
        "model": model,
        "status": "completed",
        "question": public_question(question),
        "intent": intent,
        "intent_label": intent_label,
        "analysis_summary": (
            f"本次先把用户当前消息识别为“{intent_label}”。系统随后检查会话所属用户、"
            f"当前服务端身份、已绑定的商品或订单上下文以及 Agent 的能力边界。确认可访问范围后，"
            f"只从{scope}取数。下列记录来自本次实际执行轨迹，不使用模型自行编造的步骤。"
        ),
        "analysis_details": analysis_details,
        "result_summary": (
            f"已完成 {len(enriched_steps)} 个受控步骤"
            + (f"，获得 {count} 项可用结果" if count else "，未发现需要展示的结构化结果")
            + "。最终回复只使用通过权限校验的信息。"
        ),
        "steps": enriched_steps,
        "source_ids": list(source_ids),
        "raw_reasoning_exposed": False,
        "tool_calls": tool_calls,
        "context_trace": _context_trace(data),
        "knowledge_trace": _knowledge_trace(data),
        "memory_trace": _memory_trace(data),
        "model_invocation": _model_invocation_trace(data),
    }
    trace.update(dict(extra or {}))
    trace["orchestration_trace"] = _orchestration_trace(trace, enriched_steps)
    return trace


def _tool_calls(data: Mapping[str, Any]) -> list[dict[str, object]]:
    supplied = data.get("_audit_tool_calls")
    if not isinstance(supplied, list):
        return []
    projected_calls: list[dict[str, object]] = []
    for item in supplied:
        if not isinstance(item, Mapping):
            continue
        projected = audit_projection(item)
        if isinstance(projected, Mapping):
            projected_calls.append(dict(projected))
    return projected_calls


def _derived_tool_calls(
    steps: Sequence[Mapping[str, Any]], count: int, data: Mapping[str, Any]
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    result_snapshot = audit_projection(
        {
            key: value
            for key, value in data.items()
            if not key.startswith("_audit_") and key != "conversation_window"
        }
    )
    for step in steps:
        tool_code = step.get("tool_code")
        if not isinstance(tool_code, str) or tool_code in {"", "none", "multi_agent"}:
            continue
        calls.append(
            {
                "sequence": len(calls) + 1,
                "tool_code": tool_code,
                "arguments": audit_projection(step.get("tool_arguments") or {}),
                "status": step.get("status") or "completed",
                "result": result_snapshot,
                "result_count": step.get("result_count", count),
                "error_code": step.get("error_code"),
                "latency_ms": step.get("latency_ms"),
                "record_source": "step_projection",
            }
        )
    return calls


def _attach_tool_calls(
    steps: list[dict[str, object]], tool_calls: list[dict[str, object]]
) -> list[dict[str, object]]:
    pending = list(tool_calls)
    for step in steps:
        tool_code = step.get("tool_code")
        if not isinstance(tool_code, str) or tool_code in {"", "none", "multi_agent"}:
            continue
        match = next((item for item in pending if item.get("tool_code") == tool_code), None)
        if match is None:
            continue
        pending.remove(match)
        step["tool_call"] = match
    return steps


def _context_trace(data: Mapping[str, Any]) -> object:
    window = data.get("conversation_window")
    if isinstance(window, Mapping):
        return {
            "status": "read",
            "recorded": True,
            "window": audit_projection(window),
        }
    return {
        "status": "not_recorded",
        "recorded": False,
        "reason": "本轮执行数据未包含对话窗口快照。",
    }


def _knowledge_trace(data: Mapping[str, Any]) -> object:
    rag = data.get("rag")
    sources = data.get("knowledge_sources")
    if not isinstance(rag, Mapping) and not isinstance(sources, list):
        return {
            "status": "not_invoked",
            "invoked": False,
            "query": None,
            "matches": [],
            "reason": "本轮任务由业务工具直接完成，没有发起知识库或 RAG 检索。",
        }
    return audit_projection(
        {
            "status": "completed",
            "invoked": True,
            "query": data.get("policy_query"),
            "retrieval": rag if isinstance(rag, Mapping) else {},
            "matches": sources if isinstance(sources, list) else [],
        }
    )


def _memory_trace(data: Mapping[str, Any]) -> object:
    recall = data.get("memory")
    items = data.get("recalled_memories")
    if not isinstance(recall, Mapping) and not isinstance(items, list):
        return {
            "status": "not_invoked",
            "invoked": False,
            "items": [],
            "reason": "本轮没有发起长期记忆召回。短期会话上下文单独记录。",
        }
    return audit_projection(
        {
            "status": "completed",
            "invoked": True,
            "recall": recall if isinstance(recall, Mapping) else {},
            "items": items if isinstance(items, list) else [],
        }
    )


def _model_invocation_trace(data: Mapping[str, Any]) -> object:
    invocation = data.get("model_invocation")
    if isinstance(invocation, Mapping):
        return audit_projection(
            {"status": "completed", "provider_request_sent": True, **invocation}
        )
    return {
        "status": "not_invoked",
        "provider_request_sent": False,
        "reason": "本轮没有模型调用记录。规划或回复由可复核的受控 Agent 逻辑完成。",
    }


def _orchestration_trace(
    trace: Mapping[str, Any], steps: Sequence[Mapping[str, Any]]
) -> dict[str, object]:
    subtasks = trace.get("subtasks")
    goal_ledger = trace.get("goal_ledger")
    delegations = [step for step in steps if step.get("kind") == "delegation"]
    return {
        "mode": trace.get("orchestration_mode")
        or ("multi_agent" if delegations else "single_agent"),
        "supervisor": trace.get("agent"),
        "planning_source": trace.get("planning_source") or "not_recorded",
        "execution_strategy": trace.get("execution_strategy") or "not_recorded",
        "delegation_count": trace.get("delegation_count") or len(delegations),
        "batch_count": trace.get("batch_count") or 1,
        "max_parallel_per_batch": trace.get("max_parallel_per_batch") or 1,
        "goal_ledger": audit_projection(
            goal_ledger if isinstance(goal_ledger, list) else []
        ),
        "coverage_complete": trace.get("coverage_complete") is True,
        "subtasks": audit_projection(subtasks if isinstance(subtasks, list) else []),
        "delegations": audit_projection(delegations),
    }


def _analysis_detail(step: Mapping[str, Any], count: int) -> str:
    kind = str(step.get("kind") or "action")
    label = str(step.get("label") or "受控处理动作").strip()
    tool_code = str(step.get("tool_code") or "")
    status = str(step.get("status") or "completed")
    status_label = {
        "completed": "已完成",
        "succeeded": "成功",
        "partial": "部分完成",
        "failed": "失败",
        "timed_out": "超时",
        "reused": "复用已验证结果",
    }.get(status, status)
    details: list[str] = []
    if kind == "tool" and tool_code and tool_code != "none":
        details.append(f"“{label}”调用业务工具 {tool_code}，并通过身份、权限和数据范围网关")
    elif kind in {"rag", "retrieval"}:
        details.append(f"“{label}”在当前用户与店铺数据范围内执行知识检索")
    elif kind == "context":
        details.append(f"“{label}”读取本会话最近消息和有效滚动摘要，用于理解指代与连续问题")
    elif kind == "memory":
        details.append(f"“{label}”仅检查用户明确授权、尚未过期且与当前问题相关的长期偏好")
    elif kind == "supervisor":
        delegation_count = int(step.get("delegation_count") or 0)
        details.append(
            f"“{label}”把复杂只读任务拆成 {delegation_count} 个相互隔离的专业子任务，"
            "限制委派深度并禁止子 Agent 执行写操作"
        )
    elif kind == "delegation":
        specialist = str(step.get("specialist") or "受限专业 Agent")
        delegated_tool = str(step.get("tool_code") or "")
        tool_calls = int(step.get("tool_calls") or 0)
        latency_ms = int(step.get("latency_ms") or 0)
        details.append(
            f"“{label}”由 {specialist} 在继承的数据范围内完成，"
            f"执行 {tool_calls} 次受控工具调用"
            + (f"，工具为 {delegated_tool}" if delegated_tool else "")
            + (f"，耗时 {latency_ms} 毫秒" if latency_ms else "")
        )
    elif kind == "security":
        details.append(f"“{label}”检查越权、敏感信息与提示词注入风险")
    elif kind == "answer":
        details.append(f"“{label}”对取回的数据进行事实一致性检查，并据此生成聊天区中的回答")
    elif kind == "plan":
        details.append(f"“{label}”识别当前消息的业务意图，并确定所需的数据域和只读能力")
    else:
        details.append(f"已执行“{label}”")
    if kind in {"tool", "rag", "retrieval"}:
        details.append(f"返回 {count} 项可用结果")
    details.append(f"执行状态为{status_label}")
    return "。".join(details) + "。"


def ensure_public_trace(
    trace: Mapping[str, Any] | None,
    *,
    run_id: str,
    agent: str,
    model: str,
    question: object,
    data: Mapping[str, Any],
    degraded_reason: str | None = None,
) -> dict[str, object]:
    supplied = dict(trace or {})
    fallback_intent = (
        "security_refusal" if degraded_reason == "prompt_injection_blocked" else "response"
    )
    intent = str(supplied.get("intent") or fallback_intent)
    supplied_steps = supplied.get("steps")
    steps = (
        [item for item in supplied_steps if isinstance(item, Mapping)]
        if isinstance(supplied_steps, list)
        else [
            {
                "kind": "security" if intent == "security_refusal" else "answer",
                "label": "执行安全检查" if intent == "security_refusal" else "组织受控回复",
                "status": "completed",
            }
        ]
    )
    source_values = supplied.get("source_ids")
    source_ids = (
        [str(item) for item in source_values if isinstance(item, str)]
        if isinstance(source_values, list)
        else []
    )
    allowed_extra = {
        "answer_mode",
        "confidence",
        "cited_source_ids",
        "grounding_verified",
        "evidence_truncated",
        "truncated_evidence_fields",
        "model_invocation",
        "limitation",
        "orchestration_mode",
        "degraded_reason",
        "analysis_summary",
        "analysis_details",
        "thinking_mode",
        "planning_confidence",
        "planning_source",
        "execution_strategy",
        "required_capabilities",
        "missing_slots",
        "continuation_of_previous_turn",
        "response_strategy",
        "goal_ledger",
        "coverage_complete",
        "subtasks",
        "deterministic_tasks",
        "delegation_count",
        "batch_count",
        "max_parallel_per_batch",
        "approval_id",
    }
    extra = {key: value for key, value in supplied.items() if key in allowed_extra}
    if degraded_reason:
        extra.setdefault("degraded_reason", degraded_reason)
    return public_trace(
        run_id=run_id,
        agent=agent,
        model=model,
        question=question,
        intent=intent,
        data=data,
        steps=steps,
        source_ids=source_ids,
        extra=extra,
    )


def _enrich_step(step: Mapping[str, Any], data: Mapping[str, Any], count: int) -> dict[str, object]:
    value = dict(step)
    kind = str(value.get("kind") or "action")
    label = str(value.get("label") or "受控处理步骤")
    if not value.get("summary"):
        value["summary"] = {
            "plan": f"已完成意图识别与任务边界判断: {label}。",
            "supervisor": "已将复杂任务拆成相互隔离的只读子任务，并限制委派深度。",
            "delegation": "已向受限专业 Agent 委派子任务，只返回允许公开的结果摘要。",
            "context": "已读取本会话最近消息与滚动摘要，旧消息不会无限进入上下文。",
            "memory": "仅召回用户已明确授权且仍在有效期内的长期偏好。",
            "rag": f"已在当前权限范围的知识库中检索，返回 {count} 项候选内容。",
            "retrieval": f"已完成检索，返回 {count} 项候选内容。",
            "tool": f"已执行只读或受控工具调用，返回 {count} 项可用结果。",
            "security": "已阻断越权、提示词注入或高风险操作，未读取受限数据。",
            "answer": "已依据已校验的上下文、工具结果和知识来源生成面向用户的回复。",
        }.get(kind, f"已完成: {label}。")
    value.setdefault("status", "completed")
    if kind in {"rag", "retrieval", "tool"}:
        value.setdefault("result_count", count)
    return value
