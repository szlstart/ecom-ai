from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.modules.agent_runtime.checkpoints import AgentCheckpointStore
from app.modules.agent_runtime.context_window import ContextWindow, ContextWindowBuilder
from app.modules.agent_runtime.conversation_summary import attach_rolling_summary
from app.modules.agent_runtime.model_gateway import (
    DeterministicStoreModelGateway,
    ModelGatewayError,
    StoreAgentPlan,
    StoreModelGateway,
)
from app.modules.agent_runtime.models import AgentRun
from app.modules.agent_runtime.prompt_safety import detects_prompt_injection, safe_untrusted_excerpt
from app.modules.agent_runtime.provider_gateway import ProviderStoreModelGateway, model_failure_code
from app.modules.agent_runtime.public_trace import ensure_public_trace, public_trace
from app.modules.agent_runtime.store_context import StoreContextBuilder, TrustedStoreAgentContext
from app.modules.agent_runtime.store_tools import StoreToolGateway, StoreToolResult
from app.modules.agent_runtime.trigger_text import agent_trace_question, agent_trigger_text
from app.modules.knowledge.service import KnowledgeService
from app.modules.messaging.models import Message
from app.modules.system.models import OutboxEvent


async def process_store_run(
    session: AsyncSession,
    run: AgentRun,
    *,
    model_gateway: StoreModelGateway | None = None,
    checkpoint_store: AgentCheckpointStore | None = None,
    security: SecurityService | None = None,
) -> None:
    builder = StoreContextBuilder(session)
    try:
        context = await builder.build(run)
    except ApplicationError as exc:
        _fail_run(run, exc.code)
        return
    if context.conversation.conversation_status != "active":
        run.run_status = "cancelled"
        run.current_phase = "cancelled"
        run.error_code = "AGENT_DISABLED_BY_CONVERSATION_STATE"
        run.version += 1
        return

    if checkpoint_store is not None:
        try:
            await checkpoint_store.initialize(context)
            await checkpoint_store.write(
                run.run_no,
                "planning",
                _checkpoint_state(context, intent=None),
            )
        except Exception:
            await checkpoint_store.session.rollback()
            await _handoff_or_fallback(session, context, "CHECKPOINT_UNAVAILABLE")
            return

    run.run_status = "running"
    run.current_phase = "planning"
    run.version += 1
    trigger_text = agent_trigger_text(context.trigger)
    if detects_prompt_injection(trigger_text):
        await _complete_message(
            session,
            context,
            "检测到可能要求绕过系统规则或泄露敏感信息的指令，本次不会调用业务工具。你可以重新描述正常的商品、订单或政策问题。",
            error_code="AI_PROMPT_INJECTION_BLOCKED",
            degraded_reason="prompt_injection_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    gateway = model_gateway or DeterministicStoreModelGateway()
    context_window = await ContextWindowBuilder(session).build(
        context.conversation, context.trigger
    )
    if checkpoint_store is not None and security is not None:
        context_window = await attach_rolling_summary(
            context_window,
            mysql=session,
            postgres=checkpoint_store.session,
            security=security,
            conversation=context.conversation,
            trigger=context.trigger,
            user_no=context.user.user_no,
            store_no=context.store.store_no,
        )
    planning_input = (
        context_window.planning_input(trigger_text)
        if isinstance(gateway, ProviderStoreModelGateway)
        else trigger_text
    )
    try:
        plan = await gateway.plan(planning_input)
    except (ModelGatewayError, TimeoutError) as exc:
        plan = await DeterministicStoreModelGateway().plan(trigger_text)
        run.degraded_reason = model_failure_code(exc, "planning")

    if checkpoint_store is not None:
        try:
            await checkpoint_store.write(
                run.run_no,
                "tool_planned",
                _checkpoint_state(context, intent=plan.intent),
            )
        except Exception:
            await checkpoint_store.session.rollback()
            await _handoff_or_fallback(session, context, "CHECKPOINT_UNAVAILABLE")
            return

    tools = StoreToolGateway(session)
    try:
        outcome = await _execute_plan(builder, tools, context, plan)
    except ApplicationError as exc:
        await _complete_message(
            session,
            context,
            "页面中的咨询对象已经变化，请返回对应商品或订单页面重新选择后再问我。",
            error_code=exc.code,
            degraded_reason="context_unavailable",
        )
        await _finish_checkpoint(checkpoint_store, context, plan.intent)
        return
    if outcome.status == "succeeded":
        _attach_conversation_window(context_window, outcome.data)
        await _attach_store_knowledge(
            session,
            checkpoint_store,
            context,
            plan.intent,
            outcome.data,
        )
        answer, trace = await _grounded_answer(
            context,
            gateway,
            plan,
            outcome.data,
        )
        await _complete_message(
            session,
            context,
            answer,
            data=outcome.data,
            execution_trace=trace,
        )
        await _finish_checkpoint(checkpoint_store, context, plan.intent)
        return
    if outcome.error_code in {"TOOL_TIMEOUT_UNKNOWN", "TOOL_EXECUTION_FAILED"}:
        await _handoff_or_fallback(session, context, outcome.error_code)
        await _finish_checkpoint(checkpoint_store, context, plan.intent)
        return
    await _complete_message(
        session,
        context,
        "我无法在当前店铺和当前页面范围内读取这项信息。请重新选择商品或订单，或转人工客服核实。",
        error_code=outcome.error_code,
        degraded_reason="tool_denied",
    )
    await _finish_checkpoint(checkpoint_store, context, plan.intent)


async def _execute_plan(
    builder: StoreContextBuilder,
    tools: StoreToolGateway,
    context: TrustedStoreAgentContext,
    plan: StoreAgentPlan,
) -> StoreToolResult:
    context.run.current_phase = "tool_call"
    context.run.version += 1
    if plan.intent == "general_chat":
        return StoreToolResult(
            "succeeded",
            {
                "assistant_scope": (
                    "可以协助当前店铺内的商品咨询、款式对比、库存、服务政策、"
                    "订单解释和商品推荐，只有用户明确要求时才转人工客服。"
                )
            },
        )
    if plan.intent == "human_handoff":
        return await tools.handoff(
            context, ticket_type="general", reason_code="USER_REQUESTED_HUMAN"
        )
    if plan.intent == "policy_qa":
        return await tools.policies(context)
    if plan.intent == "product_recommend":
        recommendations = await tools.recommendations(context, plan.search_text)
        if recommendations.status != "succeeded":
            return recommendations
        items = recommendations.data.get("items")
        product_nos = (
            [
                str(item["product_id"])
                for item in items[:4]
                if isinstance(item, dict) and isinstance(item.get("product_id"), str)
            ]
            if isinstance(items, list)
            else []
        )
        if len(product_nos) >= 2:
            comparison = await tools.compare_products(context, product_nos)
            if comparison.status == "succeeded":
                recommendations.data["comparison"] = comparison.data.get("items", [])
        return recommendations
    if plan.intent == "order_explain":
        ref = await builder.require_active_context(context, "order")
        summary = await tools.order_summary(context, ref.resource_no)
        if summary.status != "succeeded":
            return summary
        shipment_result = await tools.shipments(context, ref.resource_no)
        if shipment_result.status != "succeeded":
            return shipment_result
        summary.data["shipments"] = shipment_result.data.get("items", [])
        return summary
    ref = await builder.require_active_context(context, "product")
    if plan.intent == "sku_compare":
        return await tools.compare_skus(context, ref.resource_no)
    if plan.intent == "inventory_lookup":
        return await tools.inventory(context, ref.resource_no)
    return await tools.product(context, ref.resource_no)


async def _handoff_or_fallback(
    session: AsyncSession,
    context: TrustedStoreAgentContext,
    reason_code: str,
) -> None:
    result = await StoreToolGateway(session).handoff(
        context, ticket_type="general", reason_code=reason_code
    )
    if result.status == "succeeded":
        await _complete_message(
            session,
            context,
            "智能客服暂时无法可靠完成查询，已为你转接本店人工客服。请留意排队状态。",
            data=result.data,
            degraded_reason=reason_code.casefold(),
        )
    else:
        await _complete_message(
            session,
            context,
            "智能客服暂时不可用，自动转人工也没有成功。请稍后直接告诉我“转人工”，我会再次尝试。",
            error_code=result.error_code or reason_code,
            degraded_reason="handoff_failed",
        )


async def _complete_message(
    session: AsyncSession,
    context: TrustedStoreAgentContext,
    text: str,
    *,
    data: Mapping[str, Any] | None = None,
    error_code: str | None = None,
    degraded_reason: str | None = None,
    execution_trace: Mapping[str, Any] | None = None,
) -> None:
    now = utc_now()
    conversation = context.conversation
    trace = ensure_public_trace(
        execution_trace,
        run_id=context.run.run_no,
        agent="店铺客服",
        model=context.agent_version.model_profile,
        question=agent_trace_question(context.trigger),
        data=data or {},
        degraded_reason=degraded_reason,
    )
    conversation.last_sequence_no += 1
    conversation.last_message_at = now
    conversation.version += 1
    message = Message(
        message_no=new_prefixed_ulid("msg_"),
        conversation_id=conversation.id,
        sequence_no=conversation.last_sequence_no,
        client_message_no=None,
        sender_type="agent",
        sender_id=None,
        message_type="text",
        text_content=text[:4000],
        content_payload={
            "run_id": context.run.run_no,
            "sources": _source_refs(data or {}),
            "data_scope": context.trusted_scope,
            "execution_trace": trace,
        },
        agent_version_id=context.agent_version.id,
        ai_run_no=context.run.run_no,
        message_status="sent",
        moderation_status="passed",
        sent_at=now,
    )
    session.add(message)
    await session.flush()
    conversation.last_message_id = message.id
    context.run.response_message_id = message.id
    context.run.public_output = message.text_content
    context.run.run_status = "completed"
    context.run.current_phase = "completed"
    context.run.error_code = error_code
    if degraded_reason is not None:
        context.run.degraded_reason = degraded_reason
    context.run.version += 1
    session.add_all(
        [
            *_stream_events(context, message, text, now),
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="message.sent.v1",
                aggregate_type="conversation",
                aggregate_no=conversation.conversation_no,
                aggregate_version=conversation.version,
                payload={
                    "conversation_id": conversation.conversation_no,
                    "message_id": message.message_no,
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=context.run.trace_id,
            ),
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="agent.run.completed.v1",
                aggregate_type="agent_run",
                aggregate_no=context.run.run_no,
                aggregate_version=context.run.version,
                payload={"run_id": context.run.run_no, "message_id": message.message_no},
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=context.run.trace_id,
            ),
        ]
    )


async def _grounded_answer(
    context: TrustedStoreAgentContext,
    gateway: StoreModelGateway,
    plan: StoreAgentPlan,
    data: Mapping[str, Any],
) -> tuple[str, dict[str, object]]:
    fallback = _render(plan, data)
    tool_code = _tool_for_intent(plan.intent)
    sources = _source_refs(data)
    source_ids = tuple(
        f"{item['type']}:{item['id']}"
        for item in sources
        if isinstance(item.get("type"), str) and isinstance(item.get("id"), str)
    )
    if plan.intent == "general_chat":
        source_ids = ("context:assistant_scope",)
    elif not source_ids:
        source_ids = (f"tool:{tool_code}",)
    steps: list[dict[str, object]] = [
        {"kind": "plan", "label": "理解当前消息", "status": "completed"},
    ]
    if plan.intent != "general_chat":
        steps.append(
            {
                "kind": "tool",
                "label": "查询店铺可信数据",
                "tool_code": tool_code,
                "status": "completed",
            }
        )
    steps.append({"kind": "answer", "label": "生成安全回复", "status": "completed"})
    if isinstance(data.get("rag"), dict):
        rag = data["rag"]
        steps.insert(
            1,
            {
                "kind": "rag",
                "label": "检索当前店铺公开知识",
                "status": "completed",
                "degraded": bool(rag.get("degraded")),
            },
        )
    if isinstance(data.get("conversation_window"), dict):
        window = data["conversation_window"]
        steps.insert(
            1,
            {
                "kind": "context",
                "label": "重建最近对话上下文",
                "status": "completed",
                "message_count": int(window.get("included_count", 0)),
                "omitted_count": int(window.get("omitted_count", 0)),
            },
        )
    trace = public_trace(
        run_id=context.run.run_no,
        agent="店铺客服",
        model=context.agent_version.model_profile,
        question=agent_trace_question(context.trigger),
        intent=plan.intent,
        data=data,
        steps=steps,
        source_ids=source_ids,
        tool_code=tool_code,
    )
    if not isinstance(gateway, ProviderStoreModelGateway):
        trace["answer_mode"] = "deterministic_fallback"
        return fallback, trace
    context.run.current_phase = "answering"
    context.run.version += 1
    try:
        answer = await gateway.synthesize(
            agent_prompt=context.agent_version.system_prompt,
            user_text=agent_trigger_text(context.trigger),
            intent=plan.intent,
            evidence=data,
            source_ids=source_ids,
        )
    except (ModelGatewayError, TimeoutError) as exc:
        reason = model_failure_code(exc, "answer")
        trace["answer_mode"] = "deterministic_fallback"
        trace["degraded_reason"] = reason
        context.run.degraded_reason = reason
        return fallback, trace
    trace["answer_mode"] = "model_grounded"
    trace["confidence"] = answer.confidence
    trace["cited_source_ids"] = list(answer.cited_source_ids)
    if answer.limitation:
        trace["limitation"] = answer.limitation
    return answer.text, trace


def _tool_for_intent(intent: str) -> str:
    return {
        "general_chat": "none",
        "human_handoff": "support.create_store_ticket",
        "policy_qa": "catalog.get_store_policy",
        "product_recommend": "catalog.search_store_products",
        "order_explain": "order.get_store_order_summary",
        "sku_compare": "catalog.compare_skus",
        "inventory_lookup": "catalog.get_inventory_availability",
        "product_qa": "catalog.get_product",
    }.get(intent, "unknown")


def _render(plan: StoreAgentPlan, data: Mapping[str, Any]) -> str:
    if plan.intent == "general_chat":
        return "你好，我是本店智能客服。你可以直接问我商品、款式、库存、服务政策或订单问题。"
    if plan.intent == "human_handoff":
        return (
            "我正在帮你转接本店人工客服。转接期间我会暂停回复，店铺人员结束服务后我会继续协助你。"
        )
    if plan.intent == "inventory_lookup":
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return "当前没有可靠的展示库存结果，请稍后刷新商品页。"
        lines = ["当前展示库存如下 (查询结果不代表预占, 最终以结算为准):"]
        for item in items[:4]:
            if isinstance(item, dict):
                lines.append(
                    f"- {item.get('sku_name', '规格')}: {item.get('availability', 'unknown')}"
                )
        return "\n".join(lines)
    if plan.intent == "sku_compare":
        items = data.get("items")
        lines = ["同一商品下的规格对比 (价格和可售状态以结算为准):"]
        if isinstance(items, list):
            for item in items[:4]:
                if isinstance(item, dict):
                    lines.append(
                        f"- {item.get('name', '规格')}: {item.get('specifications', [])}, "
                        f"{_money(item.get('sale_price_amount'), item.get('currency'))}"
                    )
        return "\n".join(lines)
    if plan.intent == "policy_qa":
        items = data.get("items")
        knowledge = data.get("knowledge_sources")
        if (not isinstance(items, list) or not items) and not (
            isinstance(knowledge, list) and knowledge
        ):
            return "本店暂未发布可用于回答该问题的有效政策，请转人工客服核实。"
        lines = ["根据本店当前生效政策:"]
        for item in items[:3] if isinstance(items, list) else []:
            if isinstance(item, dict):
                excerpt = safe_untrusted_excerpt(item.get("content"), 240)
                lines.append(
                    f"- {item.get('title', '店铺政策')} "
                    f"(版本 {item.get('source_version')}): {excerpt}"
                )
        for item in knowledge[:4] if isinstance(knowledge, list) else []:
            if isinstance(item, dict):
                lines.append(
                    f"- {safe_untrusted_excerpt(item.get('title'), 160)} "
                    f"(知识版本 {item.get('version')}): "
                    f"{safe_untrusted_excerpt(item.get('excerpt'), 500)}"
                )
        lines.append("如需政策外例外或商家承诺，请转人工客服确认。")
        return "\n".join(lines)
    if plan.intent == "order_explain":
        status_value = data.get("status")
        amounts_value = data.get("amounts")
        status: Mapping[str, Any] = status_value if isinstance(status_value, dict) else {}
        amounts: Mapping[str, Any] = amounts_value if isinstance(amounts_value, dict) else {}
        result = (
            f"该本店订单当前状态: 订单 {status.get('order', 'unknown')}, "
            f"支付 {status.get('payment', 'unknown')}, "
            f"履约 {status.get('fulfillment', 'unknown')}, "
            f"售后 {status.get('after_sale', 'unknown')}。实付 "
            f"{_money_value(amounts.get('paid'))}。"
            "如需执行取消、确认收货或退款，请进入订单详情页操作。"
        )
        actions = data.get("available_actions")
        if isinstance(actions, list) and actions:
            result += " 当前页面可用操作: " + "、".join(str(item) for item in actions[:8]) + "。"
        shipments = data.get("shipments")
        if isinstance(shipments, list) and shipments:
            result += f" 当前共有 {len(shipments)} 个公开物流包裹，可进入订单物流页查看轨迹。"
        return result
    if plan.intent == "product_recommend":
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return "本店当前没有符合条件的在售商品。我不会跨店补充结果，你可以调整一个筛选条件。"
        lines = ["根据本店当前在售商品, 先提供这些候选:"]
        for item in items[:5]:
            if isinstance(item, dict):
                price_value = item.get("price")
                price: Mapping[str, Any] = price_value if isinstance(price_value, dict) else {}
                lines.append(
                    f"- {safe_untrusted_excerpt(item.get('name', '商品'), 120)}: "
                    f"{safe_untrusted_excerpt(item.get('subtitle') or '查看商品详情', 300)}, "
                    f"{_money(price.get('min_amount'), price.get('currency'))} 起"
                )
        return "\n".join(lines)
    attributes = data.get("attributes")
    lines = [f"商品: {safe_untrusted_excerpt(data.get('name', '当前商品'), 120)}"]
    if data.get("subtitle"):
        lines.append(f"商品说明: {safe_untrusted_excerpt(data['subtitle'], 300)}")
    if isinstance(attributes, list) and attributes:
        lines.append("公开参数:")
        for item in attributes[:8]:
            if isinstance(item, dict):
                lines.append(
                    f"- {safe_untrusted_excerpt(item.get('name', item.get('code', '参数')), 100)}: "
                    f"{safe_untrusted_excerpt(item.get('value', '未提供'), 300)}"
                    f"{safe_untrusted_excerpt(item.get('unit') or '', 30)}"
                )
    faqs = data.get("faqs")
    if isinstance(faqs, list) and faqs and isinstance(faqs[0], dict):
        lines.append(f"店铺 FAQ: {safe_untrusted_excerpt(faqs[0].get('answer'), 500)}")
    estimate_value = data.get("dispatch_estimate")
    estimate: Mapping[str, Any] = estimate_value if isinstance(estimate_value, dict) else {}
    if estimate.get("status") == "available":
        lines.append(
            "预计发货范围: "
            f"{estimate.get('min_at')} 至 {estimate.get('max_at')}。"
            "这是基于店铺资料的估算，不构成发货承诺。"
        )
    else:
        lines.append("当前没有可靠的发货时效估算，我不会承诺具体发货时间。")
    lines.append("以上是店铺公开资料; 无法确认的兼容性、安全性或效果问题请转人工核实。")
    return "\n".join(lines)


def _stream_events(
    context: TrustedStoreAgentContext,
    message: Message,
    text: str,
    now: Any,
) -> list[OutboxEvent]:
    common = {
        "conversation_id": context.conversation.conversation_no,
        "run_id": context.run.run_no,
    }
    events: list[OutboxEvent] = []
    for index, end in enumerate(range(160, len(text) + 160, 160), start=1):
        events.append(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="agent.response.delta.v1",
                aggregate_type="conversation",
                aggregate_no=context.conversation.conversation_no,
                aggregate_version=context.conversation.version,
                payload={
                    **common,
                    "chunk_index": index,
                    "text_so_far": text[:end],
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=context.run.trace_id,
            )
        )
    events.append(
        OutboxEvent(
            event_no=new_prefixed_ulid("evt_"),
            event_type="agent.response.completed.v1",
            aggregate_type="conversation",
            aggregate_no=context.conversation.conversation_no,
            aggregate_version=context.conversation.version,
            payload={**common, "message_id": message.message_no},
            event_status="pending",
            available_at=now,
            attempt_count=0,
            trace_id=context.run.trace_id,
        )
    )
    return events


def _source_refs(data: Mapping[str, Any]) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    if isinstance(data.get("product_id"), str):
        refs.append({"type": "product", "id": data["product_id"]})
    if isinstance(data.get("order_id"), str):
        refs.append({"type": "order", "id": data["order_id"]})
    items = data.get("items")
    if isinstance(items, list):
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("policy_id"), str):
                refs.append({"type": "store_policy", "id": item["policy_id"]})
            elif isinstance(item.get("product_id"), str):
                refs.append({"type": "product", "id": item["product_id"]})
    knowledge = data.get("knowledge_sources")
    if isinstance(knowledge, list):
        for item in knowledge[:8]:
            if not isinstance(item, dict) or not isinstance(item.get("document_id"), str):
                continue
            refs.append(
                {
                    "type": "knowledge",
                    "id": item["document_id"],
                    "title": safe_untrusted_excerpt(item.get("title"), 160),
                    "version": item.get("version"),
                    "score": item.get("score"),
                }
            )
    return refs


async def _attach_store_knowledge(
    mysql: AsyncSession,
    checkpoint_store: AgentCheckpointStore | None,
    context: TrustedStoreAgentContext,
    intent: str,
    data: dict[str, object],
) -> None:
    if checkpoint_store is None or intent not in {"policy_qa", "product_qa"}:
        return
    context.run.current_phase = "retrieving"
    context.run.version += 1
    try:
        result = await KnowledgeService(mysql, checkpoint_store.session).search_for_agent(
            query=context.trigger.text_content or "店铺公开信息",
            scope_type="store",
            scope_no=context.store.store_no,
            limit=6,
            trace_id=context.run.trace_id,
        )
    except SQLAlchemyError:
        await checkpoint_store.session.rollback()
        data["rag"] = {
            "scope": f"store:{context.store.store_no}",
            "returned_count": 0,
            "degraded": True,
            "error_code": "RAG_RETRIEVAL_UNAVAILABLE",
        }
        return
    data["knowledge_sources"] = [
        {
            "document_id": item.document_id,
            "title": item.title,
            "version": item.content_version,
            "excerpt": item.excerpt,
            "score": round(item.score, 6),
        }
        for item in result.items
    ]
    data["rag"] = {
        "scope": f"store:{context.store.store_no}",
        "returned_count": len(result.items),
        "degraded": result.degraded,
        "retrieval_mode": "keyword_only" if result.degraded else "hybrid",
    }


def _attach_conversation_window(window: ContextWindow, data: dict[str, object]) -> None:
    if window.recent_turns:
        data["conversation_window"] = window.evidence_projection()


def _money(amount: object, currency: object) -> str:
    if not isinstance(amount, int) or isinstance(amount, bool):
        return "金额未知"
    return f"{currency or 'CNY'!s} {amount / 100:.2f}"


def _money_value(value: object) -> str:
    if isinstance(value, Mapping):
        display = value.get("display")
        if isinstance(display, str) and display:
            return display
        try:
            return _money(int(str(value.get("minor_units"))), value.get("currency"))
        except (TypeError, ValueError):
            return "金额未知"
    return _money(value, "CNY")


def _fail_run(run: AgentRun, code: str) -> None:
    run.run_status = "failed"
    run.current_phase = "failed"
    run.error_code = code
    run.version += 1


def _checkpoint_state(
    context: TrustedStoreAgentContext, *, intent: str | None
) -> dict[str, object]:
    state: dict[str, object] = {
        "run_no": context.run.run_no,
        "conversation_no": context.conversation.conversation_no,
        "trigger_message_no": context.trigger.message_no,
        "user_no": context.user.user_no,
        "store_no": context.store.store_no,
        "agent_version_no": str(context.agent_version.version_no),
        "context_refs": [
            {
                "context_no": item.get("context_id"),
                "context_type": item.get("context_type"),
                "resource_no": item.get("resource_id"),
                "resource_version": item.get("resource_version"),
            }
            for item in context.run.context_snapshot
        ],
    }
    if intent is not None:
        state["intent"] = intent
    return state


async def _finish_checkpoint(
    checkpoint_store: AgentCheckpointStore | None,
    context: TrustedStoreAgentContext,
    intent: str,
) -> None:
    if checkpoint_store is None:
        return
    try:
        await checkpoint_store.write(
            context.run.run_no,
            "completed",
            _checkpoint_state(context, intent=intent),
            status="completed",
        )
    except Exception:
        await checkpoint_store.session.rollback()
        context.run.degraded_reason = "checkpoint_terminal_write_failed"
