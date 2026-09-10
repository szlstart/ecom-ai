from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.modules.agent_runtime.answer_formatting import concise_policy_answer
from app.modules.agent_runtime.checkpoints import AgentCheckpointStore
from app.modules.agent_runtime.context_window import ContextWindow, ContextWindowBuilder
from app.modules.agent_runtime.conversation_summary import attach_rolling_summary
from app.modules.agent_runtime.model_gateway import (
    DeterministicStoreModelGateway,
    ModelGatewayError,
    StoreAgentPlan,
    StoreModelGateway,
    complete_store_plan,
    refine_store_plan_for_context,
    requests_cross_store_search,
    requests_other_user_data,
)
from app.modules.agent_runtime.models import AgentRun
from app.modules.agent_runtime.order_cards import (
    build_order_cards,
    order_nos_from_result,
    recent_agent_order_nos,
    referenced_order_no,
    requests_direct_transaction_action,
)
from app.modules.agent_runtime.product_cards import (
    build_product_cards,
    is_short_affirmative,
    product_card_reference_index,
    product_card_reference_indices,
    product_nos_from_result,
    recent_agent_product_cards,
    referenced_product_card,
)
from app.modules.agent_runtime.prompt_safety import detects_prompt_injection, safe_untrusted_excerpt
from app.modules.agent_runtime.provider_gateway import (
    AgentStreamCallback,
    ProviderStoreModelGateway,
    model_failure_code,
)
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
    stream_callback: AgentStreamCallback | None = None,
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
    if requests_other_user_data(trigger_text):
        await _complete_message(
            session,
            context,
            "我只能读取你本人在本店的订单和物流，不能查看其他顾客的账号、订单或购买记录。",
            error_code="AI_OTHER_USER_DATA_BLOCKED",
            degraded_reason="data_scope_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    if requests_cross_store_search(trigger_text):
        await _complete_message(
            session,
            context,
            "本店客服只能查询当前店铺的商品。要查其他店铺或进行全平台比较，请点击左侧置顶的“专属客服”。",
            error_code="AI_CROSS_STORE_SCOPE_BLOCKED",
            degraded_reason="data_scope_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    if requests_direct_transaction_action(trigger_text):
        await _complete_message(
            session,
            context,
            "为了保护你的账户和资金安全，我不能代你付款、取消订单或确认收货。请在购物车或订单详情页核对金额和状态后自行操作。",
            error_code="AI_DIRECT_TRANSACTION_BLOCKED",
            degraded_reason="protected_action_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    gateway = model_gateway or DeterministicStoreModelGateway()
    context_window = await ContextWindowBuilder(session).build(
        context.conversation, context.trigger
    )
    planning_product_cards = await recent_agent_product_cards(
        session,
        context.conversation,
        before_sequence=context.trigger.sequence_no,
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
    fast_plan = await DeterministicStoreModelGateway().plan(trigger_text)
    fast_plan = refine_store_plan_for_context(
        fast_plan,
        trigger_text,
        has_product_context=(
            "product" in context.context_refs
            or len(planning_product_cards) == 1
            or referenced_product_card(trigger_text, planning_product_cards) is not None
        ),
        has_order_context="order" in context.context_refs,
    )
    if fast_plan.intent != "general_chat" or not isinstance(gateway, ProviderStoreModelGateway):
        plan = fast_plan
    else:
        planning_input = context_window.planning_input(trigger_text)
        try:
            plan = await gateway.plan(planning_input)
        except (ModelGatewayError, TimeoutError) as exc:
            plan = fast_plan
            run.degraded_reason = model_failure_code(exc, "planning")
        plan = refine_store_plan_for_context(
            plan,
            trigger_text,
            has_product_context=(
                "product" in context.context_refs
                or len(planning_product_cards) == 1
                or referenced_product_card(trigger_text, planning_product_cards) is not None
            ),
            has_order_context="order" in context.context_refs,
        )

    if is_short_affirmative(trigger_text) and len(planning_product_cards) > 1:
        plan = complete_store_plan(
            StoreAgentPlan(
                "product_qa",
                confidence=1.0,
                missing_slots=("product_choice",),
                continuation_of_previous_turn=True,
                response_strategy="clarify",
            )
        )

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

    if plan.response_strategy == "clarify" and plan.missing_slots:
        await _complete_message(
            session,
            context,
            _clarification_text(plan.missing_slots),
            execution_trace=_clarification_trace(plan),
        )
        await _finish_checkpoint(checkpoint_store, context, plan.intent)
        return

    tools = StoreToolGateway(session)
    try:
        outcome = await _execute_plan(builder, tools, context, plan, trigger_text)
    except ApplicationError as exc:
        context_message = (
            "我没有在本店找到能唯一对应的商品。你可以换个商品关键词，"
            "如果要查询其他店铺，请切换到置顶的专属客服。"
            if exc.code == "AGENT_CONTEXT_REQUIRED"
            else "页面中的商品或订单信息已经更新，请重新选择后再问我。"
        )
        await _complete_message(
            session,
            context,
            context_message,
            error_code=exc.code,
            degraded_reason="context_unavailable",
        )
        await _finish_checkpoint(checkpoint_store, context, plan.intent)
        return
    if outcome.status == "succeeded":
        _attach_conversation_window(context_window, context.context_refs, outcome.data)
        if plan.intent in {"inventory_lookup", "sku_compare", "policy_qa"}:
            outcome.data["presentation"] = "detail_cards"
        elif context.trigger.message_type == "product_card":
            # A card is already a deliberate resource selection. Render it
            # immediately instead of reinterpreting an older page context.
            outcome.data["presentation"] = "product_cards"
        elif plan.intent == "product_qa" and _is_usage_question(trigger_text):
            # Suitability questions need a deterministic, evidence-only answer.
            # A model must not repeat the product title and then ask the same
            # question back to the shopper.
            outcome.data["presentation"] = "product_cards"
        if context.trigger.message_type not in {"product_card", "order_card"}:
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
            stream_callback=stream_callback,
        )
        order_cards = await build_order_cards(
            session,
            context.user,
            context.conversation,
            order_nos_from_result(outcome.data),
        )
        product_cards = (
            await build_product_cards(
                session,
                context.conversation,
                product_nos_from_result(outcome.data),
            )
            if plan.intent
            in {
                "product_qa",
                "product_compare",
                "sku_compare",
                "inventory_lookup",
                "product_recommend",
            }
            else []
        )
        rich_content: dict[str, object] = {}
        if order_cards:
            rich_content["order_cards"] = order_cards
        if product_cards:
            rich_content["product_cards"] = product_cards
        detail_cards = _store_detail_cards(plan, outcome.data, trigger_text)
        if detail_cards:
            rich_content["detail_cards"] = detail_cards
        await _complete_message(
            session,
            context,
            answer,
            data=outcome.data,
            execution_trace=trace,
            extra_content=rich_content or None,
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
    trigger_text: str,
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
        recommendations.data["presentation"] = "product_cards"
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
    if plan.intent == "product_compare":
        reference_indices = product_card_reference_indices(trigger_text)
        minimum_count = max(reference_indices, default=1) + 1
        recent_cards = await recent_agent_product_cards(
            tools.session,
            context.conversation,
            before_sequence=context.trigger.sequence_no,
            minimum_count=minimum_count,
        )
        selected_indices = reference_indices if len(reference_indices) >= 2 else [0, 1]
        product_nos = [
            str(recent_cards[index]["product_id"])
            for index in selected_indices[:4]
            if index < len(recent_cards) and isinstance(recent_cards[index].get("product_id"), str)
        ]
        if len(dict.fromkeys(product_nos)) < 2:
            return StoreToolResult(
                "succeeded",
                {"items": [], "comparison_source_count": len(dict.fromkeys(product_nos))},
            )
        comparison = await tools.compare_products(context, product_nos)
        if comparison.status == "succeeded":
            comparison.data["presentation"] = "product_comparison"
        return comparison
    if plan.intent == "order_explain":
        if _requests_order_list(trigger_text):
            return await tools.list_user_orders(context)
        order_no = _trigger_resource_no(context.trigger, "order_card", "order_id")
        if order_no is not None:
            pass
        elif "order" in context.context_refs:
            order_no = (await builder.require_active_context(context, "order")).resource_no
        else:
            recent_order_nos = await recent_agent_order_nos(
                tools.session,
                context.conversation,
                before_sequence=context.trigger.sequence_no,
            )
            order_no = referenced_order_no(trigger_text, recent_order_nos)
        if order_no is None:
            return await tools.list_user_orders(context)
        summary = await tools.order_summary(context, order_no)
        if summary.status != "succeeded":
            return summary
        shipment_result = await tools.shipments(context, order_no)
        if shipment_result.status != "succeeded":
            return shipment_result
        summary.data["shipments"] = shipment_result.data.get("items", [])
        summary.data["presentation"] = "order_card"
        return summary
    direct_product_no = _trigger_resource_no(context.trigger, "product_card", "product_id")
    captured_product_ref = None
    if "product" in context.context_refs:
        # Validate the immutable context snapshot before resolving conversational
        # focus. A recent card may guide pronouns, but must never bypass a page
        # context switch that happened after the user sent this message.
        captured_product_ref = await builder.require_active_context(context, "product")
    if direct_product_no is not None:
        product_no = direct_product_no
        if captured_product_ref is not None and product_no != captured_product_ref.resource_no:
            captured_scope_check = await tools.product(context, captured_product_ref.resource_no)
            if captured_scope_check.status != "succeeded":
                return captured_scope_check
        if plan.intent == "sku_compare":
            comparison = await tools.compare_skus(context, product_no)
            _limit_sku_comparison_to_named_variants(comparison.data, trigger_text)
            return comparison
        if plan.intent == "inventory_lookup":
            return await tools.inventory(context, product_no)
        return await tools.product(context, product_no)
    reference_index = product_card_reference_index(trigger_text)
    minimum_card_count = (
        2
        if reference_index == 0 and _requests_previous_result_reselection(trigger_text)
        else max(1, reference_index + 1)
        if reference_index is not None
        else 1
    )
    recent_cards = await recent_agent_product_cards(
        tools.session,
        context.conversation,
        before_sequence=context.trigger.sequence_no,
        minimum_count=minimum_card_count,
    )
    recent_reference = referenced_product_card(
        trigger_text,
        recent_cards,
    )
    recent_product_no = recent_reference.get("product_id") if recent_reference else None
    if isinstance(recent_product_no, str):
        product_no = recent_product_no
    else:
        resolution = await tools.resolve_product(
            context,
            trigger_text,
            # Color, size and package names frequently repeat across a store.  An
            # active page-bound product remains the focus unless the shopper names
            # another product (matched from its title) or selects a recent card.
            include_sku_aliases=captured_product_ref is None,
        )
        if resolution.status != "succeeded":
            return resolution
        resolved_product_no = resolution.data.get("product_id")
        if isinstance(resolved_product_no, str):
            product_no = resolved_product_no
        elif "product" in context.context_refs:
            # A deictic product question remains bound to the server-captured
            # product context when neither an ordinal card nor an explicit name
            # resolves to another current-store product.
            product_no = (await builder.require_active_context(context, "product")).resource_no
        elif "order" in context.context_refs:
            product_no = await _single_order_product_no(builder, tools, context)
        else:
            product_no = (await builder.require_active_context(context, "product")).resource_no
    if captured_product_ref is not None and product_no != captured_product_ref.resource_no:
        # A conversationally selected card may become the new linguistic focus,
        # while the original page context still needs a scoped, audited check.
        captured_scope_check = await tools.product(context, captured_product_ref.resource_no)
        if captured_scope_check.status != "succeeded":
            return captured_scope_check
    if plan.intent == "sku_compare":
        comparison = await tools.compare_skus(context, product_no)
        _limit_sku_comparison_to_named_variants(comparison.data, trigger_text)
        return comparison
    if plan.intent == "inventory_lookup":
        return await tools.inventory(context, product_no)
    return await tools.product(context, product_no)


def _limit_sku_comparison_to_named_variants(data: dict[str, Any], user_text: str) -> None:
    """Keep a comparison focused on SKU names explicitly mentioned by the shopper."""

    values = data.get("items")
    if not isinstance(values, list):
        return
    normalized = re.sub(r"\s+", "", user_text).casefold()
    selected = [
        item
        for item in values
        if isinstance(item, Mapping)
        and isinstance(item.get("name"), str)
        and re.sub(r"\s+", "", item["name"]).casefold() in normalized
    ]
    if len(selected) >= 2:
        data["items"] = selected[:4]


def _requests_previous_result_reselection(user_text: str) -> bool:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    return any(
        marker in normalized
        for marker in ("再看看第一个", "再看第一个", "回到第一个", "换回第一个")
    )


def _trigger_resource_no(message: Message, message_type: str, key: str) -> str | None:
    """Read a server-validated resource id from the current structured turn."""

    if message.message_type != message_type or not isinstance(message.content_payload, Mapping):
        return None
    value = message.content_payload.get(key)
    return value if isinstance(value, str) else None


async def _single_order_product_no(
    builder: StoreContextBuilder,
    tools: StoreToolGateway,
    context: TrustedStoreAgentContext,
) -> str:
    """Resolve a referential product question against a one-product order card."""

    order_ref = await builder.require_active_context(context, "order")
    summary = await tools.order_summary(context, order_ref.resource_no)
    if summary.status != "succeeded":
        raise ApplicationError(
            status=409,
            code=summary.error_code or "AGENT_CONTEXT_REQUIRED",
            title="Agent context unavailable",
            detail="订单中的商品暂时无法读取。",
        )
    items = summary.data.get("items")
    product_nos = (
        list(
            dict.fromkeys(
                str(item["product_id"])
                for item in items
                if isinstance(item, dict) and isinstance(item.get("product_id"), str)
            )
        )
        if isinstance(items, list)
        else []
    )
    if len(product_nos) != 1:
        raise ApplicationError(
            status=409,
            code="AGENT_PRODUCT_CONTEXT_AMBIGUOUS",
            title="Agent context unavailable",
            detail="订单包含多个商品，请先选择要咨询的商品。",
        )
    return product_nos[0]


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
    extra_content: Mapping[str, Any] | None = None,
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
            **dict(extra_content or {}),
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
    *,
    stream_callback: AgentStreamCallback | None = None,
) -> tuple[str, dict[str, object]]:
    fallback = _render(plan, data, agent_trigger_text(context.trigger))
    tool_code = (
        "order.list_user_store_orders"
        if data.get("presentation") == "order_cards"
        else _tool_for_intent(plan.intent)
    )
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
        {"kind": "plan", "label": "分析用户诉求", "status": "completed"},
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
    steps.append({"kind": "answer", "label": "核验依据并组织答复", "status": "completed"})
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
                "label": "读取最近会话",
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
        extra={
            "planning_confidence": plan.confidence,
            "required_capabilities": list(plan.required_capabilities),
            "missing_slots": list(plan.missing_slots),
            "continuation_of_previous_turn": plan.continuation_of_previous_turn,
            "response_strategy": plan.response_strategy,
        },
    )
    if data.get("presentation") in {
        "order_card",
        "order_cards",
        "product_cards",
        "detail_cards",
    }:
        trace["answer_mode"] = "structured_ui"
        return fallback, trace
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
            stream_callback=stream_callback,
        )
    except (ModelGatewayError, TimeoutError) as exc:
        reason = model_failure_code(exc, "answer")
        trace["answer_mode"] = "deterministic_fallback"
        trace["degraded_reason"] = reason
        context.run.degraded_reason = reason
        if stream_callback is not None:
            await stream_callback("answer_replace", fallback)
        return fallback, trace
    trace["answer_mode"] = "model_grounded"
    trace["confidence"] = answer.confidence
    trace["cited_source_ids"] = list(answer.cited_source_ids)
    trace["thinking_mode"] = "enabled" if answer.thinking_used else "not_reported"
    trace["grounding_verified"] = answer.grounding_verified
    trace["evidence_truncated"] = answer.evidence_truncated
    trace["truncated_evidence_fields"] = list(answer.truncated_evidence_fields)
    trace["model_invocation"] = _model_invocation_trace(answer)
    if answer.analysis_summary:
        trace["analysis_summary"] = answer.analysis_summary
    if answer.analysis_details:
        trace["analysis_details"] = list(answer.analysis_details)
    if answer.limitation:
        trace["limitation"] = answer.limitation
    return answer.text, trace


def _tool_for_intent(intent: str) -> str:
    return {
        "general_chat": "none",
        "human_handoff": "support.create_store_ticket",
        "policy_qa": "catalog.get_store_policy",
        "product_recommend": "catalog.search_store_products",
        "product_compare": "catalog.compare_products",
        "order_explain": "order.get_store_order_summary",
        "sku_compare": "catalog.compare_skus",
        "inventory_lookup": "catalog.get_inventory_availability",
        "product_qa": "catalog.get_product",
    }.get(intent, "unknown")


def _model_invocation_trace(answer: object) -> dict[str, object]:
    return {
        "model": getattr(answer, "model_name", None),
        "input_tokens": getattr(answer, "input_tokens", None),
        "output_tokens": getattr(answer, "output_tokens", None),
        "total_tokens": getattr(answer, "total_tokens", None),
        "first_token_latency_ms": getattr(answer, "first_token_latency_ms", None),
        "model_latency_ms": getattr(answer, "model_latency_ms", None),
        "estimated_cost_usd": getattr(answer, "estimated_cost_usd", None),
        "cost_status": (
            "known" if getattr(answer, "estimated_cost_usd", None) is not None else "unknown"
        ),
    }


def _render(plan: StoreAgentPlan, data: Mapping[str, Any], user_text: str = "") -> str:
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
        product_name = safe_untrusted_excerpt(data.get("product_name"), 160)
        return (
            f"已查到“{product_name or '当前商品'}”的实时库存。款式、价格和可售数量都整理在卡片中。"
        )
    if plan.intent == "sku_compare":
        items = data.get("items")
        count = len(items) if isinstance(items, list) else 0
        return f"已把 {count} 个可选款式放在对比卡片中。点击商品入口可以继续选择和购买。"
    if plan.intent == "product_compare":
        items = data.get("items")
        count = len(items) if isinstance(items, list) else 0
        if count < 2:
            return "请先让我推荐至少两件商品，再说“对比前两个”或明确要比较的序号。"
        return f"已按同一口径对比这 {count} 件商品，价格、公开参数和商品入口都在下方卡片中。"
    if plan.intent == "policy_qa":
        items = data.get("items")
        knowledge = data.get("knowledge_sources")
        has_sources = (isinstance(items, list) and bool(items)) or (
            isinstance(knowledge, list) and bool(knowledge)
        )
        if not has_sources:
            delivery = data.get("platform_delivery")
            asks_delivery = any(term in user_text for term in ("包邮", "运费", "配送", "邮寄"))
            asks_origin = "发货地" in user_text or "哪里发货" in user_text
            if asks_delivery and isinstance(delivery, Mapping):
                suffix = (
                    "本店暂未发布额外退换政策，具体售后资格请以订单售后入口为准。"
                    if any(term in user_text for term in ("退换", "退款", "售后"))
                    else "最终仍以结算页实时结果为准。"
                )
                origin_note = (
                    "发货地按具体商品设置，请发送商品卡片或打开商品详情查看。"
                    if asks_origin
                    else ""
                )
                return f"当前平台统一采用邮寄且包邮。{origin_note}{suffix}"
            return "本店暂未发布可用于回答该问题的有效政策，请转人工客服核实。"
        sources = [
            (item.get("title"), item.get("content"))
            for item in (items if isinstance(items, list) else [])[:3]
            if isinstance(item, dict)
        ]
        sources.extend(
            (item.get("title"), item.get("excerpt"))
            for item in (knowledge if isinstance(knowledge, list) else [])[:4]
            if isinstance(item, dict)
        )
        return concise_policy_answer(user_text, sources, intro="根据本店当前生效政策")
    if plan.intent == "order_explain":
        if data.get("presentation") == "order_cards":
            items = data.get("items")
            count = len(items) if isinstance(items, list) else 0
            if count == 0:
                return "你在本店暂时没有可见订单。"
            if requests_direct_transaction_action(user_text):
                return (
                    "为了避免误操作，我不能代你付款、取消订单或确认收货。"
                    "请从下方订单卡片进入详情页自行核对并操作。"
                )
            return f"找到你在本店的 {count} 笔订单。点击卡片可查看详情或继续处理。"
        if "用户发送了订单卡片" in user_text:
            return (
                "我已经看到这笔订单了。你遇到的是付款、发货、物流、收货，"
                "还是退款售后方面的问题? 告诉我具体情况，我来帮你查。"
            )
        if data.get("presentation") == "order_card":
            if requests_direct_transaction_action(user_text):
                return (
                    "为了避免误操作，我不能代你付款、取消订单或确认收货。"
                    "请点击下方订单卡片，核对状态后自行操作。"
                )
            focused = _store_order_focus_answer(data, user_text)
            if focused is not None:
                return focused
            return "已找到这笔订单。点击卡片可查看详情, 也可以直接告诉我你想查物流、收货还是售后。"
        status_value = data.get("status")
        amounts_value = data.get("amounts")
        status: Mapping[str, Any] = status_value if isinstance(status_value, dict) else {}
        amounts: Mapping[str, Any] = amounts_value if isinstance(amounts_value, dict) else {}
        result = (
            f"该本店订单当前状态: 订单{_store_status_label('order', status.get('order'))}，"
            f"支付{_store_status_label('payment', status.get('payment'))}，"
            f"履约{_store_status_label('fulfillment', status.get('fulfillment'))}，"
            f"售后{_store_status_label('after_sale', status.get('after_sale'))}。实付"
            f"{_money_value(amounts.get('paid'))}。"
            "如需执行取消、确认收货或退款，请进入订单详情页操作。"
        )
        actions = data.get("available_actions")
        if isinstance(actions, list) and actions:
            result += (
                " 当前页面可用操作: "
                + "、".join(_ORDER_ACTION_LABELS.get(str(item), "查看订单") for item in actions[:8])
                + "。"
            )
        shipments = data.get("shipments")
        if isinstance(shipments, list) and shipments:
            result += f" 当前共有 {len(shipments)} 个公开物流包裹，可进入订单物流页查看轨迹。"
        return result
    if plan.intent == "product_recommend":
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return "本店当前没有符合条件的在售商品。我不会跨店补充结果，你可以调整一个筛选条件。"
        if len(items) == 1:
            return (
                "按你给出的条件，本店目前只找到 1 件匹配的在售商品，已放在卡片中。"
                "我没有用不相关商品凑数; 你可以补充预算、考试类型或科目，我再继续筛选。"
            )
        return (
            f"为你找到 {min(len(items), 5)} 件本店在售商品。"
            "可以直接点击卡片查看详情; 如果你告诉我考试类型、科目或预算，我还能继续缩小范围。"
        )
    if "用户发送了商品卡片" in user_text:
        return (
            "我看到你发来的商品了，关键信息已放在下方卡片中。"
            "你更想了解款式或规格、实时库存、发货，还是它是否适合你的使用场景?"
        )
    if _is_affirmative_product_follow_up(user_text, data):
        product_name = safe_untrusted_excerpt(data.get("name", "当前商品"), 120)
        return (
            f"可以，我们接着看“{product_name}”。你想先看款式和尺码、实时库存、"
            "发货信息，还是具体使用场景? 选一个方向，我就继续帮你查。"
        )
    size_answer = _render_size_answer(data, user_text)
    if size_answer is not None:
        return size_answer
    usage_answer = _render_usage_answer(data, user_text)
    if usage_answer is not None:
        return usage_answer
    product_name = safe_untrusted_excerpt(data.get("name", "当前商品"), 120)
    lines = [f"这款是“{product_name}”。"]
    attributes = data.get("attributes")
    highlights: list[str] = []
    if isinstance(attributes, list):
        for item in attributes[:5]:
            if not isinstance(item, Mapping):
                continue
            name = safe_untrusted_excerpt(item.get("name", item.get("code", "特点")), 40)
            value = safe_untrusted_excerpt(item.get("value", ""), 80)
            unit = safe_untrusted_excerpt(item.get("unit") or "", 12)
            if name and value:
                highlights.append(f"{name}为{value}{unit}")
    if highlights:
        lines.append("它的主要特点是" + "，".join(highlights[:4]) + "。")
    skus = data.get("skus")
    if isinstance(skus, list) and skus:
        lines.append(f"目前商品页有 {len(skus)} 个可选款式，具体价格和库存以下单时为准。")
    lines.append("发货时效以店铺已发布政策和订单物流为准，我不会承诺具体发货时间。")
    lines.append("你更想了解款式、尺码或规格、库存、发货，还是适不适合某个使用场景?")
    return "\n\n".join(lines)


def _store_order_focus_answer(data: Mapping[str, Any], user_text: str) -> str | None:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    if any(term in normalized for term in ("物流", "快递", "包裹", "到哪里", "到哪")):
        shipments = data.get("shipments")
        values = shipments if isinstance(shipments, list) else []
        if not values:
            return "这笔订单当前还没有可见物流包裹。你可以稍后再查，或点击订单卡片查看状态。"
        first = values[0] if isinstance(values[0], Mapping) else {}
        tracks = first.get("latest_tracks")
        latest = tracks[0] if isinstance(tracks, list) and tracks else {}
        latest_map = latest if isinstance(latest, Mapping) else {}
        status = _store_status_label("shipment", first.get("shipment_status"))
        location = safe_untrusted_excerpt(latest_map.get("location") or "位置更新中", 80)
        return f"这笔订单物流当前为“{status}”，最新位置是“{location}”。详细轨迹已放在下方卡片中。"
    if any(term in normalized for term in ("退款", "退货", "售后")):
        actions = data.get("available_actions")
        can_apply = isinstance(actions, list) and "apply_after_sale" in actions
        return (
            "这笔订单当前可以发起售后。点击订单卡片进入详情页后选择“申请售后”。"
            if can_apply
            else "这笔订单当前没有可用的售后入口。点击订单卡片可以查看状态和具体原因。"
        )
    return None


def _is_usage_question(user_text: str) -> bool:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    return any(
        marker in normalized
        for marker in ("适合", "使用场景", "什么场景", "什么用途", "用来做什么", "能干什么")
    )


def _render_usage_answer(data: Mapping[str, Any], user_text: str) -> str | None:
    if not _is_usage_question(user_text):
        return None
    evidence_values = [
        data.get("name"),
        data.get("subtitle"),
        data.get("description"),
        data.get("safe_detail_text"),
    ]
    faqs = data.get("faqs")
    if isinstance(faqs, list):
        for item in faqs[:10]:
            if isinstance(item, Mapping):
                evidence_values.extend((item.get("question"), item.get("answer")))
    evidence = " ".join(str(value) for value in evidence_values if value)
    usage_terms = (
        "考试",
        "办公",
        "学习",
        "学生",
        "书写",
        "绘图",
        "测量",
        "记录",
        "日程",
        "裁纸",
        "裁剪",
        "手工",
        "手帐",
        "切割",
        "削笔",
    )
    supported = [term for term in usage_terms if term in evidence]
    product_name = safe_untrusted_excerpt(data.get("name") or "这款商品", 80)
    normalized_question = re.sub(r"\s+", "", user_text).casefold()
    requested = [term for term in usage_terms if term in normalized_question]
    if requested and all(term in supported for term in requested):
        conclusion = f"商家资料明确标注它可用于{'、'.join(requested)}。"
    elif requested:
        conclusion = (
            f"商家资料没有明确标注“{'、'.join(requested)}”这一用途，所以我不能替商家保证适用。"
        )
    elif supported:
        conclusion = f"商家资料明确标注的使用场景包括{'、'.join(supported[:6])}。"
    else:
        conclusion = "商家当前资料没有明确写出适用场景，所以我不能只凭商品名称替你判断。"
    known_uses = (
        f" 已核实的用途关键词还有{'、'.join(supported[:6])}。" if requested and supported else ""
    )
    inventory_note = _inventory_note(data, user_text)
    return (
        f"关于“{product_name}”，{conclusion}{known_uses}"
        f"{inventory_note}"
        "你可以告诉我具体准备怎么用，我再按现有尺寸、材质和款式帮你核对。"
    )


def _inventory_note(data: Mapping[str, Any], user_text: str) -> str:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    if not any(term in normalized for term in ("库存", "有货", "现货", "多少件")):
        return ""
    values = data.get("skus")
    rows = values if isinstance(values, list) else []
    available = [
        (
            safe_untrusted_excerpt(row.get("sku_name") or "默认款式", 50),
            int(row.get("available_quantity") or 0),
        )
        for row in rows
        if isinstance(row, Mapping)
    ]
    if not available:
        return " 当前没有可靠的实时库存结果。"
    if len(available) == 1:
        return f" 当前实时可售库存为 {available[0][1]} 件。"
    summary = "、".join(f"{name} {quantity} 件" for name, quantity in available[:5])
    return f" 当前各款式实时可售库存为: {summary}。"


def _requests_order_list(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return any(
        marker in normalized
        for marker in (
            "哪些订单",
            "什么订单",
            "所有订单",
            "全部订单",
            "订单记录",
            "购买记录",
            "买过什么",
            "买了什么",
            "我都买过",
            "我在你店买过",
        )
    )


def _store_detail_cards(
    plan: StoreAgentPlan,
    data: Mapping[str, Any],
    user_text: str = "",
) -> list[dict[str, object]]:
    product_no = data.get("product_id")
    product_action = (
        {"resource_type": "product", "resource_id": product_no, "label": "打开商品详情"}
        if isinstance(product_no, str)
        else None
    )
    normalized_text = re.sub(r"\s+", "", user_text).casefold()
    asks_logistics = any(
        term in normalized_text for term in ("物流", "快递", "包裹", "到哪里", "到哪")
    )
    if plan.intent == "order_explain" and isinstance(data.get("order_id"), str) and asks_logistics:
        shipment_values = data.get("shipments")
        rows: list[dict[str, object]] = []
        for shipment in (shipment_values if isinstance(shipment_values, list) else [])[:3]:
            if not isinstance(shipment, Mapping):
                continue
            tracks = shipment.get("latest_tracks")
            latest = tracks[0] if isinstance(tracks, list) and tracks else {}
            latest_map = latest if isinstance(latest, Mapping) else {}
            rows.append(
                {
                    "label": safe_untrusted_excerpt(shipment.get("carrier_name") or "物流包裹", 60),
                    "value": _store_status_label("shipment", shipment.get("shipment_status")),
                    "meta": safe_untrusted_excerpt(
                        " · ".join(
                            value
                            for value in (
                                _compact_store_tracking_no(shipment.get("tracking_no_masked")),
                                str(latest_map.get("location") or ""),
                                str(latest_map.get("description") or ""),
                            )
                            if value
                        ),
                        180,
                    ),
                }
            )
        if rows:
            return [
                {
                    "kind": "store_order_logistics",
                    "icon": "运",
                    "eyebrow": "本店订单物流",
                    "title": "包裹最新进度",
                    "badge": "已核验",
                    "summary": "轨迹来自当前订单的最近一次物流同步。",
                    "rows": rows,
                    "action": {
                        "resource_type": "order",
                        "resource_id": data["order_id"],
                        "label": "查看订单与完整物流",
                    },
                }
            ]
    if plan.intent == "inventory_lookup":
        values = data.get("items")
        rows = [
            {
                "label": safe_untrusted_excerpt(item.get("sku_name") or "默认款式", 80),
                "value": _money_value(item.get("price")),
                "meta": (
                    f"{safe_untrusted_excerpt(item.get('availability_label') or '库存待确认', 40)}"
                    f" · 可售 {max(0, int(item.get('available_quantity', 0)))} 件"
                ),
            }
            for item in (values if isinstance(values, list) else [])[:8]
            if isinstance(item, Mapping)
        ]
        return [
            {
                "kind": "inventory",
                "icon": "库",
                "eyebrow": "实时库存",
                "title": safe_untrusted_excerpt(data.get("product_name") or "当前商品", 120),
                "badge": "实时查询",
                "summary": "库存不会提前占用, 最终数量以结算页为准。",
                "rows": rows,
                "action": product_action,
            }
        ]
    if plan.intent == "sku_compare":
        values = data.get("items")
        rows = []
        for item in (values if isinstance(values, list) else [])[:8]:
            if not isinstance(item, Mapping):
                continue
            specifications = item.get("specifications")
            specification_text = _sku_specification_text(specifications)
            availability = safe_untrusted_excerpt(
                item.get("availability_label") or "库存待确认", 40
            )
            available_quantity = item.get("available_quantity")
            stock_text = (
                f"可售 {max(0, int(available_quantity))} 件"
                if isinstance(available_quantity, int)
                else "数量待确认"
            )
            rows.append(
                {
                    "label": safe_untrusted_excerpt(item.get("name") or "商品款式", 80),
                    "value": _money(item.get("sale_price_amount"), item.get("currency")),
                    "meta": " · ".join(
                        value for value in (specification_text, availability, stock_text) if value
                    ),
                }
            )
        return [
            {
                "kind": "sku_compare",
                "icon": "比",
                "eyebrow": "款式对比",
                "title": "可选款式一览",
                "badge": f"{len(rows)} 个款式",
                "summary": "直接比较价格和关键规格, 点击下方可进入商品页选择。",
                "rows": rows,
                "action": product_action,
            }
        ]
    if plan.intent == "product_compare":
        values = data.get("items")
        comparison_rows: list[dict[str, str]] = []
        for item in (values if isinstance(values, list) else [])[:4]:
            if not isinstance(item, Mapping):
                continue
            price = item.get("price")
            min_amount = price.get("min_amount") if isinstance(price, Mapping) else None
            max_amount = price.get("max_amount") if isinstance(price, Mapping) else None
            currency = price.get("currency") if isinstance(price, Mapping) else "CNY"
            price_text = _money(min_amount, currency)
            if isinstance(max_amount, int) and max_amount != min_amount:
                price_text = f"{price_text} 至 {_money(max_amount, currency)}"
            attributes = item.get("attributes")
            attribute_text = " / ".join(
                f"{safe_untrusted_excerpt(attribute.get('name') or '参数', 24)}: "
                f"{safe_untrusted_excerpt(attribute.get('value') or '', 40)}"
                for attribute in (attributes if isinstance(attributes, list) else [])[:3]
                if isinstance(attribute, Mapping) and attribute.get("value")
            )
            comparison_rows.append(
                {
                    "label": safe_untrusted_excerpt(item.get("name") or "商品", 100),
                    "value": price_text,
                    "meta": attribute_text or "点击商品卡片查看完整信息",
                }
            )
        if comparison_rows:
            return [
                {
                    "kind": "product_compare",
                    "icon": "比",
                    "eyebrow": "商品对比",
                    "title": "推荐商品差异",
                    "badge": f"{len(comparison_rows)} 件商品",
                    "summary": "以下是公开价格和关键参数，库存以商品详情与结算页为准。",
                    "rows": comparison_rows,
                }
            ]
    if plan.intent == "policy_qa":
        values = data.get("items")
        rows = [
            {
                "label": safe_untrusted_excerpt(item.get("title") or "店铺政策", 80),
                "value": "当前生效",
                "meta": safe_untrusted_excerpt(item.get("content") or "", 180),
            }
            for item in (values if isinstance(values, list) else [])[:4]
            if isinstance(item, Mapping)
        ]
        if rows:
            return [
                {
                    "kind": "store_policy",
                    "icon": "规",
                    "eyebrow": "店铺服务政策",
                    "title": "本次回答依据",
                    "badge": "已核验",
                    "summary": "以下内容来自本店当前发布的有效政策。",
                    "rows": rows,
                }
            ]
        delivery = data.get("platform_delivery")
        if isinstance(delivery, Mapping):
            return [
                {
                    "kind": "store_policy",
                    "icon": "寄",
                    "eyebrow": "平台配送规则",
                    "title": "邮寄 · 包邮",
                    "badge": "当前生效",
                    "summary": "本店暂无额外配送政策，结算时会再次实时核验。",
                    "rows": [
                        {
                            "label": "配送方式",
                            "value": safe_untrusted_excerpt(delivery.get("method") or "邮寄", 20),
                            "meta": "运费 ¥0.00",
                        }
                    ],
                }
            ]
    return []


def _compact_store_tracking_no(value: object) -> str:
    text = safe_untrusted_excerpt(value or "物流单号待更新", 80)
    if text.count("*") >= 6:
        suffix = text.rstrip("*")[-4:] if not text.endswith("*") else ""
        return f"尾号 {suffix}" if suffix else "物流单号已脱敏"
    return text


def _sku_specification_text(value: object) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value[:4]:
        if not isinstance(item, Mapping):
            continue
        name = safe_untrusted_excerpt(item.get("name") or "规格", 30)
        detail = safe_untrusted_excerpt(item.get("value") or "", 50)
        if detail:
            parts.append(f"{name}: {detail}")
    return " / ".join(parts)


def _is_affirmative_product_follow_up(user_text: str, data: Mapping[str, Any]) -> bool:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    affirmative_replies = {
        "好",
        "好的",
        "好呀",
        "可以",
        "行",
        "行啊",
        "继续",
        "嗯",
        "嗯嗯",
        "ok",
        "okay",
    }
    if normalized not in affirmative_replies:
        return False
    window = data.get("conversation_window")
    return isinstance(window, Mapping) and bool(window.get("recent_turns"))


_SIZE_TOKEN = re.compile(
    r"(?<![A-Za-z])(?:XXXXXL|XXXXL|XXXL|XXL|XL|L|M|S|XS|XXS|XXXS)(?![A-Za-z])",
    re.IGNORECASE,
)
_SIZE_ORDER = {
    value: index
    for index, value in enumerate(
        ("XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "XXXXL", "XXXXXL")
    )
}


def _render_size_answer(data: Mapping[str, Any], user_text: str) -> str | None:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    if not any(
        term in normalized
        for term in (
            "尺码",
            "码数",
            "最大码",
            "最小码",
            "多少码",
            "几码",
            "多大码",
            "最大号",
            "最小号",
        )
    ):
        return None
    skus = data.get("skus")
    if not isinstance(skus, list) or not skus:
        return "当前商品资料中没有可核实的尺码信息，请以商品页款式选择区为准。"
    variants: dict[str, list[str]] = {}
    for item in skus[:20]:
        if not isinstance(item, Mapping):
            continue
        sku_name = safe_untrusted_excerpt(item.get("sku_name"), 160)
        values = [sku_name]
        specifications = item.get("specifications")
        if isinstance(specifications, list):
            for spec in specifications:
                if not isinstance(spec, Mapping):
                    continue
                name = str(spec.get("name") or "").casefold()
                if any(term in name for term in ("尺码", "码数", "大小", "size")):
                    values.append(str(spec.get("value") or ""))
        for value in values:
            for match in _SIZE_TOKEN.findall(value):
                size = match.upper()
                variants.setdefault(size, [])
                if sku_name and sku_name not in variants[size]:
                    variants[size].append(sku_name)
    if not variants:
        return None
    ordered = sorted(variants, key=lambda value: _SIZE_ORDER.get(value, -1))
    product_name = safe_untrusted_excerpt(data.get("name") or "当前商品", 120)
    if "最小" in normalized:
        selected = ordered[0]
        prefix = f"{product_name}当前最小尺码是 {selected}"
    elif "最大" in normalized or "多大码" in normalized:
        selected = ordered[-1]
        prefix = f"{product_name}当前最大尺码是 {selected}"
    else:
        return f"{product_name}当前可选尺码为: {'、'.join(ordered)}。"
    sku_names = variants[selected]
    if sku_names:
        return prefix + "。对应款式: " + "、".join(sku_names[:8]) + "。"
    return prefix + "。"


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


def _attach_conversation_window(
    window: ContextWindow,
    resource_refs: Mapping[str, Any],
    data: dict[str, object],
) -> None:
    if window.recent_turns or window.rolling_summary:
        data["conversation_window"] = window.model_projection(resource_refs)


def _clarification_text(missing_slots: tuple[str, ...]) -> str:
    if "product_choice" in missing_slots:
        return "你想继续了解第几个商品? 可以直接说“第一个”或“第二个”，也可以点击上面的商品卡片。"
    details = "、".join(
        safe_untrusted_excerpt(item, 64).strip() for item in missing_slots if item.strip()
    )
    return f"为了准确帮你处理，还需要你补充: {details}。"


def _clarification_trace(plan: StoreAgentPlan) -> dict[str, object]:
    return {
        "intent": plan.intent,
        "steps": [
            {"kind": "plan", "label": "识别仍需用户补充的信息", "status": "completed"},
            {"kind": "answer", "label": "提出一个最小澄清问题", "status": "completed"},
        ],
        "planning_confidence": plan.confidence,
        "required_capabilities": list(plan.required_capabilities),
        "missing_slots": list(plan.missing_slots),
        "continuation_of_previous_turn": plan.continuation_of_previous_turn,
        "response_strategy": plan.response_strategy,
    }


def _money(amount: object, currency: object) -> str:
    if not isinstance(amount, int) or isinstance(amount, bool):
        return "金额未知"
    prefix = "¥" if str(currency or "CNY") == "CNY" else f"{currency or 'CNY'!s} "
    return f"{prefix}{amount / 100:.2f}"


_STORE_STATUS_LABELS: dict[str, dict[str, str]] = {
    "order": {
        "pending_payment": "待付款",
        "paid": "已付款",
        "pending_shipment": "待发货",
        "shipped": "运输中",
        "completed": "已完成",
        "cancelled": "已取消",
        "closed": "已关闭",
    },
    "payment": {
        "unpaid": "未付款",
        "processing": "处理中",
        "paid": "已支付",
        "partially_refunded": "部分退款",
        "refunded": "已退款",
    },
    "fulfillment": {
        "unfulfilled": "未履约",
        "partial": "部分发货",
        "shipped": "已发货",
        "received": "已收货",
    },
    "after_sale": {
        "none": "无进行中售后",
        "in_progress": "处理中",
        "partial": "部分处理完成",
        "completed": "已完成",
    },
    "shipment": {
        "pending_pickup": "待揽收",
        "picked_up": "已揽收",
        "in_transit": "运输中",
        "out_for_delivery": "派送中",
        "delivered": "已签收",
        "exception": "物流异常",
    },
}

_ORDER_ACTION_LABELS = {
    "view_logistics": "查看物流",
    "confirm_receipt": "确认收货",
    "apply_after_sale": "申请售后",
    "cancel": "取消订单",
    "pay": "去支付",
    "review": "评价",
}


def _store_status_label(kind: str, value: object) -> str:
    text = str(value or "未知")
    return _STORE_STATUS_LABELS.get(kind, {}).get(text, "未知")


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
