from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    "personalized_recommendation": "结合已授权偏好筛选商品",
    "order_lookup": "查询本人订单",
    "logistics_lookup": "查询订单物流",
    "refund_eligibility": "检查售后资格并准备草稿",
    "refund_progress": "查询售后处理进度",
    "human_handoff": "识别人工服务请求",
    "overview": "分析当前经营概览",
    "catalog": "分析商品运营情况",
    "orders": "分析订单与履约情况",
    "inventory": "分析库存风险",
    "users": "分析平台用户情况",
    "stores": "分析平台店铺情况",
    "runtime": "分析系统与 Agent 运行情况",
    "complex_platform_diagnosis": "拆解跨领域平台诊断任务",
    "security_refusal": "识别并阻断不安全请求",
}


def public_question(value: object) -> str:
    """Return a short, redacted question suitable for the public audit panel."""

    return safe_untrusted_excerpt(value, 360).strip() or "本次会话消息"


def result_count(data: Mapping[str, Any]) -> int:
    for key in ("items", "specialists", "knowledge_sources", "shipments"):
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
    return 1 if data else 0


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
    scope = "授权范围内的业务数据" if tool_code and tool_code != "none" else "当前会话与服务范围"
    trace: dict[str, object] = {
        "version": "public-agent-trace-v2",
        "run_id": run_id,
        "agent": agent,
        "model": model,
        "status": "completed",
        "question": public_question(question),
        "intent": intent,
        "intent_label": intent_label,
        "analysis_summary": (
            f"我先判断这条消息属于“{intent_label}”，再核对当前身份、会话上下文和可用权限。"
            f"随后仅使用{scope}组织回答。"
        ),
        "result_summary": (
            f"已完成 {len(enriched_steps)} 个受控步骤"
            + (f"，获得 {count} 项可用结果" if count else "，未发现需要展示的结构化结果")
            + "。最终回复只使用通过权限校验的信息。"
        ),
        "steps": enriched_steps,
        "source_ids": list(source_ids),
        "raw_reasoning_exposed": False,
    }
    trace.update(dict(extra or {}))
    return trace


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
        "limitation",
        "orchestration_mode",
        "degraded_reason",
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


def _enrich_step(
    step: Mapping[str, Any], data: Mapping[str, Any], count: int
) -> dict[str, object]:
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
