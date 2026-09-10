from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.modules.agent_runtime.answer_formatting import concise_policy_answer
from app.modules.agent_runtime.approval_service import AgentApprovalService
from app.modules.agent_runtime.checkpoints import AgentCheckpointStore
from app.modules.agent_runtime.context_window import ContextWindow, ContextWindowBuilder
from app.modules.agent_runtime.conversation_summary import attach_rolling_summary
from app.modules.agent_runtime.exclusive_context import (
    ExclusiveContextBuilder,
    TrustedExclusiveAgentContext,
)
from app.modules.agent_runtime.exclusive_model_gateway import (
    DeterministicExclusiveModelGateway,
    ExclusiveAgentPlan,
    ExclusiveModelGateway,
    complete_exclusive_plan,
)
from app.modules.agent_runtime.exclusive_tools import ExclusiveToolGateway
from app.modules.agent_runtime.memory_runtime import AgentMemoryRuntime, explicit_memory_request
from app.modules.agent_runtime.model_gateway import ModelGatewayError, requests_other_user_data
from app.modules.agent_runtime.models import AgentRun, AgentToolApproval
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
    product_nos_from_result,
    recent_agent_product_cards,
    referenced_product_card,
)
from app.modules.agent_runtime.prompt_safety import detects_prompt_injection, safe_untrusted_excerpt
from app.modules.agent_runtime.provider_gateway import (
    AgentStreamCallback,
    ProviderExclusiveModelGateway,
    model_failure_code,
)
from app.modules.agent_runtime.public_trace import ensure_public_trace, public_trace
from app.modules.agent_runtime.store_agent import _model_invocation_trace, _stream_events
from app.modules.agent_runtime.store_tools import StoreToolResult
from app.modules.agent_runtime.trigger_text import agent_trace_question, agent_trigger_text
from app.modules.content.models import PlatformContentEntry, PlatformContentVersion
from app.modules.knowledge.embedding import embedding_provider
from app.modules.knowledge.service import KnowledgeService
from app.modules.messaging.models import Message
from app.modules.system.models import OutboxEvent


async def process_exclusive_run(
    session: AsyncSession,
    run: AgentRun,
    *,
    settings: Settings,
    security: SecurityService,
    checkpoint_store: AgentCheckpointStore,
    model_gateway: ExclusiveModelGateway | None = None,
    stream_callback: AgentStreamCallback | None = None,
) -> None:
    builder = ExclusiveContextBuilder(session)
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
    try:
        await checkpoint_store.initialize_exclusive(context)
        await checkpoint_store.write(
            run.run_no,
            "planning",
            _checkpoint_state(context, intent=None),
        )
    except Exception:
        await checkpoint_store.session.rollback()
        await _handoff(session, context, settings, security, "CHECKPOINT_UNAVAILABLE")
        return

    run.run_status = "running"
    run.current_phase = "planning"
    run.version += 1
    trigger_text = agent_trigger_text(context.trigger)
    if detects_prompt_injection(trigger_text):
        await _complete(
            session,
            context,
            "检测到可能要求绕过系统规则或泄露敏感信息的指令，本次不会调用业务工具。你可以重新描述正常的平台、订单、物流或售后问题。",
            error_code="AI_PROMPT_INJECTION_BLOCKED",
            degraded_reason="prompt_injection_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    if requests_other_user_data(trigger_text):
        await _complete(
            session,
            context,
            "我只能读取当前登录账号本人的订单、物流、售后、购物车和收藏，不能查看其他用户的数据。",
            error_code="AI_OTHER_USER_DATA_BLOCKED",
            degraded_reason="data_scope_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    if requests_direct_transaction_action(trigger_text):
        await _complete(
            session,
            context,
            "为了保护你的账户和资金安全，我不能代你付款、取消订单或确认收货。请在购物车或订单详情页核对金额和状态后自行操作。",
            error_code="AI_DIRECT_TRANSACTION_BLOCKED",
            degraded_reason="protected_action_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    if _requests_direct_refund_payout(trigger_text):
        await _complete(
            session,
            context,
            "我不能跳过用户确认、售后审核或支付处理直接退款。你可以让我先检查退款资格。符合条件时，我只能准备申请草稿，并在你核对确认后提交申请。",
            error_code="AI_DIRECT_REFUND_BLOCKED",
            degraded_reason="protected_action_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    requested_memory = explicit_memory_request(trigger_text)
    if requested_memory is not None:
        try:
            candidate = await AgentMemoryRuntime(
                session,
                checkpoint_store.session,
                security,
                embedding_provider(settings),
                settings.memory_min_vector_similarity,
            ).propose_exclusive(
                context.user,
                source_message_no=context.trigger.message_no,
                value=requested_memory,
            )
        except SQLAlchemyError:
            await checkpoint_store.session.rollback()
            candidate = None
        if candidate is None:
            await _complete(
                session,
                context,
                "这条内容没有写入长期记忆。请先在“我的 → AI 个性化与记忆”开启授权，"
                "并且只提交低敏、稳定的购物偏好。密码、证件、支付、地址和订单事实均不会记忆。",
                degraded_reason="memory_candidate_rejected",
                execution_trace={
                    "version": "public-agent-trace-v1",
                    "run_id": context.run.run_no,
                    "agent": "专属客服",
                    "status": "completed",
                    "intent": "memory_candidate",
                    "steps": [
                        {
                            "kind": "security",
                            "label": "检查个性化授权与记忆安全范围",
                            "status": "completed",
                        }
                    ],
                    "raw_reasoning_exposed": False,
                },
            )
        else:
            await _complete(
                session,
                context,
                "我已把你明确表达的购物偏好整理为候选。它现在还不会被召回，只有你点击下方“确认记住”后才会生效。",
                message_type="memory_candidate",
                extra_content={
                    "memory_id": candidate.memory_no,
                    "memory_type": candidate.memory_type,
                    "memory_key": candidate.memory_key,
                    "memory_value": candidate.value,
                    "memory_status": "candidate",
                    "memory_version": candidate.version,
                    "expires_at": candidate.expires_at.isoformat(),
                },
                execution_trace={
                    "version": "public-agent-trace-v1",
                    "run_id": context.run.run_no,
                    "agent": "专属客服",
                    "status": "completed",
                    "intent": "memory_candidate",
                    "steps": [
                        {"kind": "security", "label": "检查授权与敏感信息", "status": "completed"},
                        {"kind": "memory", "label": "创建加密候选记忆", "status": "completed"},
                        {"kind": "answer", "label": "等待用户明确确认", "status": "completed"},
                    ],
                    "raw_reasoning_exposed": False,
                },
            )
        await _finish_checkpoint(checkpoint_store, context, "memory_candidate")
        return
    approval = await session.scalar(
        select(AgentToolApproval).where(AgentToolApproval.run_id == run.id)
    )
    if approval is not None:
        await _resume_approval(
            session,
            context,
            approval,
            settings,
            security,
            checkpoint_store,
        )
        return

    gateway = model_gateway or DeterministicExclusiveModelGateway()
    context_window = await ContextWindowBuilder(session).build(
        context.conversation, context.trigger
    )
    planning_product_cards = await recent_agent_product_cards(
        session,
        context.conversation,
        before_sequence=context.trigger.sequence_no,
    )
    context_window = await attach_rolling_summary(
        context_window,
        mysql=session,
        postgres=checkpoint_store.session,
        security=security,
        conversation=context.conversation,
        trigger=context.trigger,
        user_no=context.user.user_no,
        store_no=None,
    )
    fast_plan = complete_exclusive_plan(
        await DeterministicExclusiveModelGateway().plan(trigger_text)
    )
    if fast_plan.intent != "general_chat" or not isinstance(gateway, ProviderExclusiveModelGateway):
        plan = fast_plan
    else:
        planning_input = context_window.planning_input(trigger_text)
        try:
            plan = await gateway.plan(planning_input)
        except (ModelGatewayError, TimeoutError) as exc:
            plan = fast_plan
            run.degraded_reason = model_failure_code(exc, "planning")
    plan = complete_exclusive_plan(plan)
    if plan.intent == "general_chat" and is_short_affirmative(trigger_text):
        if len(planning_product_cards) == 1:
            product_name = planning_product_cards[0].get("product_name")
            plan = complete_exclusive_plan(
                ExclusiveAgentPlan(
                    "product_search",
                    search_text=str(product_name) if isinstance(product_name, str) else None,
                    confidence=1.0,
                    continuation_of_previous_turn=True,
                )
            )
        elif len(planning_product_cards) > 1:
            plan = complete_exclusive_plan(
                ExclusiveAgentPlan(
                    "product_search",
                    confidence=1.0,
                    missing_slots=("product_choice",),
                    continuation_of_previous_turn=True,
                    response_strategy="clarify",
                )
            )
    try:
        await checkpoint_store.write(
            run.run_no,
            "tool_planned",
            _checkpoint_state(context, intent=plan.intent),
        )
    except Exception:
        await checkpoint_store.session.rollback()
        await _handoff(session, context, settings, security, "CHECKPOINT_UNAVAILABLE")
        return
    if plan.response_strategy == "clarify" and plan.missing_slots:
        await _complete(
            session,
            context,
            _clarification_text(plan.missing_slots),
            execution_trace=_clarification_trace(plan),
        )
        await _finish_checkpoint(checkpoint_store, context, plan.intent)
        return
    tools = ExclusiveToolGateway(session, settings, security)
    try:
        if plan.intent == "human_handoff":
            await _handoff(session, context, settings, security, "USER_REQUESTED_HUMAN")
            await _finish_checkpoint(checkpoint_store, context, plan.intent)
            return
        if plan.intent == "general_chat":
            result = StoreToolResult(
                "succeeded",
                {
                    "assistant_scope": (
                        "可以协助平台规则、全平台商品搜索与推荐、用户本人订单、物流和售后，"
                        "只有用户明确要求时才转平台人工客服。"
                    )
                },
            )
        elif plan.intent == "policy_qa":
            result = await _platform_policy(session, context)
        elif plan.intent in {"product_search", "personalized_recommendation"}:
            if "product" in context.context_refs:
                # Conversational focus can override which card a pronoun means,
                # but it cannot bypass optimistic validation of the page context
                # captured with this message.
                await builder.require_active_context(context, "product")
            reference_index = product_card_reference_index(trigger_text)
            recent_cards = await recent_agent_product_cards(
                session,
                context.conversation,
                before_sequence=context.trigger.sequence_no,
                minimum_count=max(1, reference_index + 1) if reference_index is not None else 1,
            )
            recent_reference = referenced_product_card(
                trigger_text,
                recent_cards,
            )
            card_name = _trigger_payload_value(
                context.trigger, "product_card", "product_name"
            )
            referenced_name = (
                card_name
                if card_name is not None
                else recent_reference.get("product_name")
                if recent_reference is not None
                else None
            )
            result = await tools.search_products(
                context,
                str(referenced_name) if isinstance(referenced_name, str) else plan.search_text,
                fallback_query=trigger_text,
            )
            if result.status == "succeeded":
                result.data["presentation"] = "product_cards"
                if any(
                    term in re.sub(r"\s+", "", trigger_text).casefold()
                    for term in ("库存", "有货", "缺货", "款式", "规格", "尺码", "码数")
                ):
                    result.data["catalog_focus"] = "sku_availability"
        elif plan.intent == "product_compare":
            recent_cards = await recent_agent_product_cards(
                session,
                context.conversation,
                before_sequence=context.trigger.sequence_no,
                minimum_count=1,
            )
            product_nos = [
                str(card["product_id"])
                for card in recent_cards[:2]
                if isinstance(card.get("product_id"), str)
            ]
            result = (
                await tools.compare_products(context, product_nos)
                if len(product_nos) >= 2
                else StoreToolResult(
                    "succeeded",
                    {"items": [], "comparison_source_count": len(product_nos)},
                )
            )
            if result.status == "succeeded":
                result.data["presentation"] = "product_comparison"
        elif plan.intent == "order_lookup":
            explicit_order_no = (
                _trigger_payload_value(context.trigger, "order_card", "order_id")
                or _resource_no(trigger_text, "ord")
            )
            ref = context.context_refs.get("order")
            result = (
                await tools.order_detail(context, explicit_order_no)
                if explicit_order_no is not None
                else await tools.order_detail(
                    context, (await builder.require_active_context(context, "order")).resource_no
                )
                if ref is not None
                else await tools.list_orders(context)
            )
            if result.status == "succeeded":
                result.data["presentation"] = (
                    "order_card" if "order_id" in result.data else "order_cards"
                )
        elif plan.intent == "cart_lookup":
            result = await tools.get_cart(context)
            if result.status == "succeeded":
                result.data["presentation"] = "cart_card"
        elif plan.intent == "logistics_lookup":
            order_no = await _read_order_no(
                trigger_text,
                context=context,
                builder=builder,
                tools=tools,
            )
            result = await tools.shipments(context, order_no)
        elif plan.intent == "refund_precheck":
            order_no = await _read_order_no(
                trigger_text,
                context=context,
                builder=builder,
                tools=tools,
            )
            result = await tools.refund_precheck(context, order_no)
        elif plan.intent == "refund_progress":
            explicit_refund_no = _resource_no(trigger_text, "ref")
            ref = context.context_refs.get("refund")
            result = (
                await tools.refund_detail(context, explicit_refund_no)
                if explicit_refund_no is not None
                else await tools.refund_detail(
                    context, (await builder.require_active_context(context, "refund")).resource_no
                )
                if ref is not None
                else await tools.list_refunds(context)
            )
        else:
            explicit_order_no = _resource_no(trigger_text, "ord")
            refund_order_no: str | None = explicit_order_no
            if refund_order_no is None and context.context_refs.get("order") is not None:
                refund_order_no = (
                    await builder.require_active_context(context, "order")
                ).resource_no
            if refund_order_no is None:
                recent_order_nos = await recent_agent_order_nos(
                    session,
                    context.conversation,
                    before_sequence=context.trigger.sequence_no,
                )
                refund_order_no = referenced_order_no(trigger_text, recent_order_nos)
                if refund_order_no is None and len(recent_order_nos) == 1:
                    refund_order_no = recent_order_nos[0]
            if refund_order_no is None:
                result = await tools.list_orders(context)
                visible_items = result.data.get("items")
                refundable_items = (
                    [
                        item
                        for item in visible_items
                        if isinstance(item, Mapping)
                        and isinstance(item.get("available_actions"), list)
                        and "apply_after_sale" in item["available_actions"]
                    ]
                    if isinstance(visible_items, list)
                    else []
                )
                if result.status == "succeeded":
                    result.data["items"] = refundable_items
                candidate_nos = order_nos_from_result(result.data)
                if result.status == "succeeded" and len(candidate_nos) == 1:
                    refund_order_no = candidate_nos[0]
                elif result.status == "succeeded":
                    result.data["selection_required"] = "refund"
                    result.data["presentation"] = "order_cards"
            if refund_order_no is not None:
                approval_service = AgentApprovalService(session, settings, security)

                async def build_draft() -> dict[str, object]:
                    return await approval_service.build_refund_draft(
                        context,
                        refund_order_no,
                        context.trigger.text_content or "申请退款",
                    )

                result = await tools.execute(
                    context,
                    "after_sale.build_refund_draft",
                    {"order_id": refund_order_no},
                    build_draft,
                )
                if result.status == "succeeded":
                    await checkpoint_store.write(
                        run.run_no,
                        "waiting_confirmation",
                        _checkpoint_state(context, intent=plan.intent),
                        status="waiting",
                    )
                    return
        if result.status == "succeeded" and plan.intent in {
            "logistics_lookup",
            "refund_precheck",
            "refund_progress",
            "policy_qa",
        }:
            result.data["presentation"] = "detail_cards"
    except ApplicationError as exc:
        if exc.code == "AGENT_RESOURCE_NOT_ACCESSIBLE":
            message = "没有找到你有权查看的对应订单或售后记录，请核对编号。"
            reason = "resource_not_accessible"
        else:
            message = "当前选择的订单或售后上下文已变化，请重新从对应详情页选择后再试。"
            reason = "context_unavailable"
        await _complete(
            session,
            context,
            message,
            error_code=exc.code,
            degraded_reason=reason,
        )
        await _finish_checkpoint(checkpoint_store, context, plan.intent)
        return

    if result.status == "succeeded":
        _attach_conversation_window(context_window, context.context_refs, result.data)
        await _attach_platform_knowledge(
            session,
            checkpoint_store,
            context,
            plan.intent,
            result.data,
        )
        await _attach_exclusive_memories(
            session,
            checkpoint_store,
            security,
            context,
            plan.intent,
            result.data,
        )
        answer, trace = await _grounded_answer(
            context,
            gateway,
            plan,
            result.data,
            stream_callback=stream_callback,
        )
        order_cards = await build_order_cards(
            session,
            context.user,
            context.conversation,
            order_nos_from_result(result.data),
        )
        product_cards = (
            await build_product_cards(
                session,
                context.conversation,
                product_nos_from_result(result.data),
            )
            if plan.intent
            in {"product_search", "personalized_recommendation", "product_compare"}
            else []
        )
        rich_content: dict[str, object] = {}
        if order_cards:
            rich_content["order_cards"] = order_cards
        if product_cards:
            rich_content["product_cards"] = product_cards
        if plan.intent == "cart_lookup":
            rich_content["cart_card"] = _cart_card(result.data)
        detail_cards = _exclusive_detail_cards(plan, result.data)
        if detail_cards:
            rich_content["detail_cards"] = detail_cards
        await _complete(
            session,
            context,
            answer,
            data=result.data,
            execution_trace=trace,
            extra_content=rich_content or None,
        )
    elif result.error_code == "AI_CONSENT_REQUIRED":
        await _complete(
            session,
            context,
            "提交退款草稿需要你先明确授权“售后协助”。授权不会自动提交退款，提交前仍会显示确认卡片。",
            error_code=result.error_code,
            degraded_reason="consent_required",
        )
    elif result.error_code in {"TOOL_TIMEOUT_UNKNOWN", "TOOL_EXECUTION_FAILED"}:
        await _handoff(session, context, settings, security, result.error_code)
    else:
        await _complete(
            session,
            context,
            "我无法在当前用户范围内可靠完成这项查询，请重新选择资源或转平台人工客服。",
            error_code=result.error_code,
            degraded_reason="tool_denied",
        )
    await _finish_checkpoint(checkpoint_store, context, plan.intent)


async def _resume_approval(
    session: AsyncSession,
    context: TrustedExclusiveAgentContext,
    approval: AgentToolApproval,
    settings: Settings,
    security: SecurityService,
    checkpoint_store: AgentCheckpointStore,
) -> None:
    service = AgentApprovalService(session, settings, security)
    if approval.approval_status == "rejected":
        await service.execute_approved(context)
        await _complete(session, context, "已取消本次退款申请草稿，没有创建售后单。")
        await _finish_checkpoint(checkpoint_store, context, "refund_eligibility")
        return
    if approval.approval_status == "expired":
        await _complete(
            session,
            context,
            "退款确认已过期，没有创建售后单。请重新进行资格检查。",
            error_code="AGENT_APPROVAL_EXPIRED",
        )
        await _finish_checkpoint(checkpoint_store, context, "refund_eligibility")
        return
    tools = ExclusiveToolGateway(session, settings, security)

    async def execute() -> dict[str, object]:
        status, refund_no, error_code = await service.execute_approved(context)
        if status == "succeeded" and refund_no:
            return {"status": status, "refund_id": refund_no}
        if status == "outcome_unknown":
            return {"status": status, "error_code": error_code}
        raise ApplicationError(
            status=409,
            code=error_code or "AGENT_APPROVED_ACTION_FAILED",
            title="Approved action failed",
            detail="退款申请条件已经变化，未创建售后单。",
        )

    result = await tools.execute(
        context,
        "after_sale.submit_refund_application",
        {"approval_id": approval.approval_no},
        execute,
        trusted_approval_no=approval.approval_no,
    )
    if result.status == "succeeded" and result.data.get("status") == "succeeded":
        await _complete(
            session,
            context,
            f"退款申请已成功提交，售后单号: {result.data['refund_id']}。"
            "你可以在“我的售后”查看进度。",
            data=result.data,
        )
    elif result.status == "succeeded" and result.data.get("status") == "outcome_unknown":
        context.run.run_status = "waiting"
        context.run.current_phase = "outcome_unknown"
        context.run.error_code = "TOOL_TIMEOUT_UNKNOWN"
        context.run.version += 1
        await checkpoint_store.write(
            context.run.run_no,
            "outcome_unknown",
            _checkpoint_state(context, intent="refund_eligibility"),
            status="waiting",
        )
        return
    else:
        await _complete(
            session,
            context,
            "退款提交前的资格或资源状态已经变化，没有创建售后单。请重新检查。",
            error_code=result.error_code,
        )
    await _finish_checkpoint(checkpoint_store, context, "refund_eligibility")


async def _platform_policy(
    session: AsyncSession, context: TrustedExclusiveAgentContext
) -> StoreToolResult:
    now = utc_now()
    query = (context.trigger.text_content or "").strip()
    statement = (
        select(PlatformContentEntry, PlatformContentVersion)
        .join(PlatformContentVersion, PlatformContentVersion.entry_id == PlatformContentEntry.id)
        .where(
            PlatformContentEntry.content_status == "active",
            PlatformContentVersion.publish_status == "published",
            PlatformContentVersion.effective_at <= now,
            or_(
                PlatformContentVersion.expires_at.is_(None),
                PlatformContentVersion.expires_at > now,
            ),
        )
    )
    if query:
        terms = [term for term in ("退款", "隐私", "物流", "支付", "账号") if term in query]
        if terms:
            statement = statement.where(
                or_(
                    *(
                        condition
                        for term in terms
                        for condition in (
                            PlatformContentEntry.title.contains(term),
                            PlatformContentVersion.safe_content.contains(term),
                        )
                    )
                )
            )
    rows = list(
        (
            await session.execute(
                statement.order_by(PlatformContentVersion.effective_at.desc()).limit(3)
            )
        ).all()
    )
    return StoreToolResult(
        "succeeded",
        {
            "items": [
                {
                    "content_id": entry.content_no,
                    "title": entry.title,
                    "version": version.document_version,
                    "content": version.safe_content[:1000],
                    "effective_at": version.effective_at,
                }
                for entry, version in rows
            ]
        },
    )


async def _handoff(
    session: AsyncSession,
    context: TrustedExclusiveAgentContext,
    settings: Settings,
    security: SecurityService,
    reason_code: str,
) -> None:
    result = await ExclusiveToolGateway(session, settings, security).handoff(context, reason_code)
    if result.status == "succeeded":
        await _complete(
            session,
            context,
            "我正在帮你转接平台人工客服。转接期间我会暂停回复，人工服务结束后我会继续协助你。",
            data=result.data,
            degraded_reason=(
                None if reason_code == "USER_REQUESTED_HUMAN" else reason_code.casefold()
            ),
        )
    else:
        await _complete(
            session,
            context,
            "平台智能客服暂时不可用，自动转人工也未成功。请稍后直接告诉我“转人工”，我会再次尝试。",
            error_code=result.error_code or reason_code,
            degraded_reason="handoff_failed",
        )


async def _complete(
    session: AsyncSession,
    context: TrustedExclusiveAgentContext,
    text: str,
    *,
    data: Mapping[str, Any] | None = None,
    error_code: str | None = None,
    degraded_reason: str | None = None,
    execution_trace: Mapping[str, Any] | None = None,
    message_type: str = "text",
    extra_content: Mapping[str, Any] | None = None,
) -> None:
    now = utc_now()
    conversation = context.conversation
    trace = ensure_public_trace(
        execution_trace,
        run_id=context.run.run_no,
        agent="专属客服",
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
        message_type=message_type,
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
            *_stream_events(context, message, text, now),  # type: ignore[arg-type]
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
    context: TrustedExclusiveAgentContext,
    gateway: ExclusiveModelGateway | None,
    plan: ExclusiveAgentPlan,
    data: Mapping[str, Any],
    *,
    stream_callback: AgentStreamCallback | None = None,
) -> tuple[str, dict[str, object]]:
    fallback = _render(plan, data, agent_trigger_text(context.trigger))
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
        {"kind": "plan", "label": "分析用户诉求", "status": "completed"},
    ]
    if plan.intent != "general_chat":
        steps.append(
            {
                "kind": "tool",
                "label": "查询用户范围内的可信数据",
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
                "label": "检索平台公开知识",
                "status": "completed",
                "degraded": bool(rag.get("degraded")),
            },
        )
    if isinstance(data.get("memory"), dict):
        memory = data["memory"]
        steps.insert(
            1,
            {
                "kind": "memory",
                "label": "读取已授权的购物偏好",
                "status": "completed",
                "used_count": int(memory.get("used_count", 0)),
                "degraded": bool(memory.get("degraded")),
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
        agent="专属客服",
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
        "product_comparison",
        "cart_card",
        "detail_cards",
    }:
        trace["answer_mode"] = "structured_ui"
        return fallback, trace
    if not isinstance(gateway, ProviderExclusiveModelGateway):
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
    answer_text = answer.text
    if plan.intent == "refund_precheck" and not any(
        marker in answer_text for marker in ("没有创建退款草稿", "未创建退款草稿")
    ):
        answer_text += "\n\n本次仅完成只读资格检查，没有创建退款草稿或售后单。"
    return answer_text, trace


def _requires_exact_catalog_rendering(intent: str) -> bool:
    """Retain the legacy classification contract for callers and regression tests.

    These intents still require exact, grounded catalog or policy evidence.  The
    final wording is now produced by the streaming model gateway and verified
    against that evidence instead of bypassing the model with a fixed template.
    """

    return intent in {"personalized_recommendation", "product_search", "policy_qa"}


def _tool_for_intent(intent: str) -> str:
    return {
        "general_chat": "none",
        "human_handoff": "support.create_platform_ticket",
        "policy_qa": "rag.policy.search",
        "product_search": "catalog.search_products",
        "product_compare": "catalog.compare_products",
        "personalized_recommendation": "catalog.search_products",
        "order_lookup": "order.list_user_orders",
        "cart_lookup": "cart.get_mine",
        "logistics_lookup": "logistics.get_user_order_shipments",
        "refund_precheck": "after_sale.check_refund_eligibility",
        "refund_eligibility": "after_sale.build_refund_draft",
        "refund_progress": "after_sale.list_user_refunds",
    }.get(intent, "unknown")


async def _read_order_no(
    trigger_text: str,
    *,
    context: TrustedExclusiveAgentContext,
    builder: ExclusiveContextBuilder,
    tools: ExclusiveToolGateway,
) -> str:
    card_order_no = _trigger_payload_value(context.trigger, "order_card", "order_id")
    if card_order_no is not None:
        return card_order_no
    explicit_order_no = _resource_no(trigger_text, "ord")
    if explicit_order_no is not None:
        return explicit_order_no
    recent_order_nos = await recent_agent_order_nos(
        tools.session,
        context.conversation,
        before_sequence=context.trigger.sequence_no,
    )
    referenced = referenced_order_no(trigger_text, recent_order_nos)
    if referenced is not None:
        return referenced
    if _requests_latest_order(trigger_text) or context.context_refs.get("order") is None:
        return await tools.latest_order_no(context)
    return (await builder.require_active_context(context, "order")).resource_no


def _trigger_payload_value(message: Message, message_type: str, key: str) -> str | None:
    """Read one trusted value from the current ACL-validated structured card."""

    if message.message_type != message_type or not isinstance(message.content_payload, Mapping):
        return None
    value = message.content_payload.get(key)
    return value if isinstance(value, str) else None


def _requests_latest_order(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return any(
        marker in normalized
        for marker in ("最近订单", "最近一笔", "最新订单", "上一笔订单", "刚买", "刚下单")
    )


def _exclusive_detail_cards(
    plan: ExclusiveAgentPlan,
    data: Mapping[str, Any],
) -> list[dict[str, object]]:
    if data.get("catalog_focus") == "sku_availability":
        values = data.get("items")
        item = values[0] if isinstance(values, list) and values else None
        if isinstance(item, Mapping):
            sku_values = item.get("skus")
            rows = []
            for sku in (sku_values if isinstance(sku_values, list) else [])[:10]:
                if not isinstance(sku, Mapping):
                    continue
                price = sku.get("price")
                availability = safe_untrusted_excerpt(
                    sku.get("availability_label") or "库存待确认", 30
                )
                rows.append(
                    {
                        "label": safe_untrusted_excerpt(
                            sku.get("sku_name") or "默认款式", 80
                        ),
                        "value": (
                            _money_object_display(price)
                            if isinstance(price, Mapping)
                            else "价格待确认"
                        ),
                        "meta": f"{availability} · 可售 "
                        f"{max(0, int(sku.get('available_stock') or 0))} 件",
                    }
                )
            product_no = item.get("product_id")
            return [
                {
                    "kind": "sku_availability",
                    "icon": "库",
                    "eyebrow": "款式与库存",
                    "title": safe_untrusted_excerpt(item.get("name") or "当前商品", 100),
                    "badge": "实时查询",
                    "summary": "按当前公开款式展示价格和可售数量。",
                    "rows": rows,
                    "action": (
                        {
                            "resource_type": "product",
                            "resource_id": product_no,
                            "label": "打开商品详情",
                        }
                        if isinstance(product_no, str)
                        else None
                    ),
                }
            ]
    if plan.intent == "product_compare":
        values = data.get("items")
        rows = []
        for item in (values if isinstance(values, list) else [])[:3]:
            if not isinstance(item, Mapping):
                continue
            price = item.get("price")
            price_text = "价格待确认"
            if isinstance(price, Mapping):
                price_text = _money_object_display(
                    {
                        "minor_units": str(price.get("min_amount") or 0),
                        "currency": str(price.get("currency") or "CNY"),
                    }
                )
            rows.append(
                {
                    "label": safe_untrusted_excerpt(item.get("name") or "商品", 100),
                    "value": price_text,
                    "meta": (
                        f"{safe_untrusted_excerpt(item.get('store_name') or '店铺', 50)} · "
                        f"{max(0, int(item.get('sku_count') or 0))} 个款式 · "
                        f"库存 {max(0, int(item.get('available_stock') or 0))} · "
                        f"已售 {max(0, int(item.get('sales_count') or 0))} · "
                        f"评分 {safe_untrusted_excerpt(item.get('rating') or '暂无', 12)}"
                    ),
                }
            )
        if rows:
            return [
                {
                    "kind": "product_compare",
                    "icon": "比",
                    "eyebrow": "商品对比",
                    "title": "关键购买信息",
                    "badge": f"{len(rows)} 件商品",
                    "summary": "同一口径比较当前公开价格、款式、库存、销量与评分。",
                    "rows": rows,
                }
            ]
    if plan.intent == "logistics_lookup":
        values = data.get("items")
        rows: list[dict[str, object]] = []
        for item in (values if isinstance(values, list) else [])[:5]:
            if not isinstance(item, Mapping):
                continue
            last_track = item.get("last_track")
            track: Mapping[str, Any] = last_track if isinstance(last_track, Mapping) else {}
            location = safe_untrusted_excerpt(track.get("location_text") or "位置更新中", 80)
            description = safe_untrusted_excerpt(track.get("description") or "暂无最新轨迹", 120)
            tracking_no = _compact_tracking_no(item.get("tracking_no_masked"))
            rows.append(
                {
                    "label": safe_untrusted_excerpt(item.get("carrier_name") or "物流包裹", 80),
                    "value": _status_label("shipment", item.get("shipment_status")),
                    "meta": f"{tracking_no} · {location} · {description}",
                }
            )
        order_no = data.get("order_id")
        return [
            {
                "kind": "logistics",
                "icon": "运",
                "eyebrow": "订单物流",
                "title": "包裹最新进度",
                "badge": "实时轨迹",
                "summary": "物流节点按承运商最近一次同步结果展示。",
                "rows": rows,
                "action": (
                    {"resource_type": "order", "resource_id": order_no, "label": "查看完整物流"}
                    if isinstance(order_no, str)
                    else None
                ),
            }
        ]
    if plan.intent == "refund_precheck":
        eligibility_value = data.get("refund_eligibility")
        eligibility: Mapping[str, Any] = (
            eligibility_value if isinstance(eligibility_value, Mapping) else {}
        )
        eligible = eligibility.get("eligible") is True
        refund_rows: list[dict[str, object]] = []
        suggested = eligibility.get("suggested_refund_amount")
        if isinstance(suggested, Mapping):
            refund_rows.append(
                {
                    "label": "建议申请金额",
                    "value": _money_object_display(suggested),
                    "meta": "最终以售后申请确认为准",
                }
            )
        allowed = eligibility.get("allowed_types")
        if isinstance(allowed, list) and allowed:
            labels = {"refund_only": "仅退款", "return_and_refund": "退货退款"}
            refund_rows.append(
                {
                    "label": "可申请类型",
                    "value": "、".join(labels.get(str(value), str(value)) for value in allowed),
                    "meta": "提交前仍需核对并确认",
                }
            )
        blocking = eligibility.get("blocking_reasons")
        if not eligible and isinstance(blocking, list) and blocking:
            refund_rows.append(
                {
                    "label": "暂不可申请",
                    "value": "需要处理",
                    "meta": safe_untrusted_excerpt(
                        "、".join(str(value) for value in blocking), 180
                    ),
                }
            )
        order_no = data.get("order_id")
        return [
            {
                "kind": "refund_eligibility",
                "icon": "售",
                "eyebrow": "售后资格预检",
                "title": "当前可申请" if eligible else "当前暂不可申请",
                "badge": "符合资格" if eligible else "受限",
                "tone": "" if eligible else "warning",
                "summary": "本次仅检查资格, 尚未创建或提交退款申请。",
                "rows": refund_rows,
                "action": (
                    {"resource_type": "order", "resource_id": order_no, "label": "查看订单售后"}
                    if isinstance(order_no, str)
                    else None
                ),
            }
        ]
    if plan.intent == "refund_progress":
        values = data.get("items")
        if "refund_id" in data:
            values = [data]
        cards: list[dict[str, object]] = []
        for item in (values if isinstance(values, list) else [])[:5]:
            if not isinstance(item, Mapping):
                continue
            refund_no = item.get("refund_id")
            if not isinstance(refund_no, str):
                continue
            requested = item.get("requested_amount")
            progress_rows: list[dict[str, object]] = []
            if isinstance(requested, Mapping):
                progress_rows.append(
                    {
                        "label": "申请金额",
                        "value": _money_object_display(requested),
                        "meta": "售后申请金额",
                    }
                )
            cards.append(
                {
                    "kind": "refund_progress",
                    "icon": "退",
                    "eyebrow": "售后进度",
                    "title": "退款申请",
                    "badge": _status_label("refund", item.get("refund_status")),
                    "summary": "点击查看处理记录、当前节点和可执行操作。",
                    "rows": progress_rows,
                    "action": {
                        "resource_type": "refund",
                        "resource_id": refund_no,
                        "label": "查看售后详情",
                    },
                }
            )
        return cards
    if plan.intent == "policy_qa":
        values = data.get("knowledge_sources")
        query = safe_untrusted_excerpt(data.get("policy_query") or "", 300)
        query_terms = {
            term
            for term in (
                "充值",
                "微信",
                "支付宝",
                "余额",
                "支付",
                "退款",
                "退货",
                "售后",
                "到账",
                "多久",
                "时效",
                "物流",
                "发货",
                "签收",
                "运费",
                "包邮",
                "隐私",
                "账号",
                "密码",
                "人工",
                "客服",
            )
            if term in query
        }
        ranked: list[tuple[int, int, Mapping[str, Any]]] = []
        for position, item in enumerate(values if isinstance(values, list) else []):
            if not isinstance(item, Mapping):
                continue
            searchable = " ".join(
                (
                    safe_untrusted_excerpt(item.get("title") or "", 160),
                    safe_untrusted_excerpt(item.get("excerpt") or item.get("content") or "", 500),
                )
            )
            score = sum(1 for term in query_terms if term in searchable)
            ranked.append((score, -position, item))
        ranked.sort(key=lambda value: (value[0], value[1]), reverse=True)
        rows: list[dict[str, object]] = []
        seen_documents: set[str] = set()
        for score, _, item in ranked:
            if query_terms and score == 0:
                continue
            identity = str(item.get("document_id") or item.get("title") or "")
            if identity in seen_documents:
                continue
            seen_documents.add(identity)
            source_text = safe_untrusted_excerpt(
                item.get("excerpt") or item.get("content") or "", 800
            )
            title = re.sub(
                r"^\[(?:系统|平台)\]\s*",
                "",
                safe_untrusted_excerpt(item.get("title") or "平台规则", 80),
            )
            rows.append(
                {
                    "label": title,
                    "value": "已发布",
                    "meta": _best_policy_excerpt(source_text, query_terms),
                }
            )
            if len(rows) >= 1:
                break
        if rows:
            return [
                {
                    "kind": "platform_policy",
                    "icon": "规",
                    "eyebrow": "平台规则",
                    "title": "本次回答依据",
                    "badge": "知识库已核验",
                    "summary": "只展示与当前问题相关的已发布规则来源。",
                    "rows": rows,
                }
            ]
    return []


def _best_policy_excerpt(source_text: str, query_terms: set[str]) -> str:
    candidates: list[tuple[int, int, str]] = []
    for position, raw_part in enumerate(
        re.split(r"(?<=[\u3002\uff01\uff1f\uff1b])|\n+", source_text)
    ):
        part = re.sub(r"^(?:#+\s*|[-*•>]\s*|\d+[.)、]\s*)", "", raw_part.strip())
        if not part:
            continue
        score = sum(1 for term in query_terms if term in part)
        candidates.append((score, -position, safe_untrusted_excerpt(part, 120)))
    if not candidates:
        return "查看本次回答引用的已发布规则。"
    candidates.sort(reverse=True)
    return candidates[0][2]


def _cart_card(data: Mapping[str, Any]) -> dict[str, object]:
    groups_value = data.get("groups")
    groups: list[dict[str, object]] = []
    for value in groups_value if isinstance(groups_value, list) else []:
        if not isinstance(value, Mapping):
            continue
        items_value = value.get("items")
        items = [
            {
                "product_id": item.get("product_id"),
                "product_name": safe_untrusted_excerpt(item.get("product_name"), 100),
                "sku_name": safe_untrusted_excerpt(item.get("sku_name"), 80),
                "image_url": item.get("image_url"),
                "quantity": item.get("quantity"),
                "is_selected": item.get("is_selected"),
                "is_valid": item.get("is_valid"),
                "current_price": item.get("current_price"),
            }
            for item in (items_value if isinstance(items_value, list) else [])[:4]
            if isinstance(item, Mapping)
        ]
        groups.append(
            {
                "store_id": value.get("store_id"),
                "store_name": safe_untrusted_excerpt(value.get("store_name"), 80),
                "store_logo_url": value.get("store_logo_url"),
                "selected_quantity": value.get("selected_quantity"),
                "selected_amount": value.get("selected_amount"),
                "items": items,
            }
        )
    amount_summary = data.get("amount_summary")
    return {
        "total_quantity": data.get("cart_total_quantity"),
        "selected_quantity": data.get("selected_quantity"),
        "valid_item_count": data.get("valid_item_count"),
        "selected_amount": (
            amount_summary.get("selected_goods_amount")
            if isinstance(amount_summary, Mapping)
            else None
        ),
        "groups": groups[:4],
    }


def _render(plan: ExclusiveAgentPlan, data: Mapping[str, Any], user_text: str = "") -> str:
    if plan.intent == "general_chat":
        return "你好，我是你的专属客服。你可以问我平台规则、商品推荐、本人订单、物流或售后问题。"
    items = data.get("items")
    if data.get("selection_required") == "refund":
        if not isinstance(items, list) or not items:
            return "你的账号下暂时没有可申请退款的可见订单。"
        return "请选择需要退款的订单。点击卡片进入订单后，我可以继续帮你检查资格并准备退款申请。"
    if plan.intent in {"product_search", "personalized_recommendation"}:
        if not isinstance(items, list) or not items:
            return "暂未找到符合当前条件的公开在售商品。你可以补充品类、用途或预算。"
        prefix = ""
        memories = data.get("recalled_memories")
        if isinstance(memories, list) and memories:
            remembered = [
                safe_untrusted_excerpt(item.get("value"), 160)
                for item in memories[:3]
                if isinstance(item, dict)
            ]
            if remembered:
                prefix = "我参考了你已授权的偏好: " + "、".join(remembered) + "。"
        if "用户发送了商品卡片" in user_text and len(items) == 1:
            return (
                "我看到你发来的商品了，关键信息已放在下方卡片中。"
                "你更想了解款式或规格、库存、发货，还是它是否适合你的使用场景?"
            )
        if data.get("catalog_focus") == "sku_availability" and len(items) == 1:
            item = items[0] if isinstance(items[0], Mapping) else {}
            name = safe_untrusted_excerpt(item.get("name") or "当前商品", 100)
            stock = max(0, int(item.get("available_stock") or 0))
            return (
                f"已查到“{name}”的在售款式与实时库存，共可售 {stock} 件。"
                "各款式已整理在下方，最终库存以结算页为准。"
            )
        if len(items) == 1:
            return (
                f"{prefix}按你给出的条件，全平台在售商品中目前只找到 1 件匹配结果，已放在卡片中。"
                "点击卡片可以查看详情。我没有用不相关商品凑数; 你可以放宽一个条件，我再继续筛选。"
            )
        return (
            f"{prefix}为你找到 {min(len(items), 5)} 件全平台在售商品。"
            "点击卡片可以直接查看; 价格和库存以商品详情与结算页的实时结果为准。"
        )
    if plan.intent == "product_compare":
        if not isinstance(items, list) or len(items) < 2:
            return "请先把至少两件想比较的商品发给我，或先让我搜索商品，再说“对比前两个”。"
        conclusion = _comparison_purchase_conclusion(user_text, items)
        return conclusion or (
            "我把价格、在售款式、实时库存、销量和评分整理成了对比卡片。"
            "点击商品卡可继续查看详情。"
        )
    if plan.intent == "order_lookup":
        if "order_id" in data:
            if "用户发送了订单卡片" in user_text:
                return (
                    "我已经看到这笔订单了。你想查付款、发货、物流、收货，"
                    "还是退款售后? 我会沿着这笔订单继续帮你处理。"
                )
            return "已找到这笔订单。点击卡片可查看详情或继续处理。"
        if not isinstance(items, list) or not items:
            return "你的账号下暂未查询到可见订单。"
        if requests_direct_transaction_action(user_text):
            return (
                "为了避免误操作，我不能代你付款、取消订单或确认收货。"
                "请从下方订单卡片进入详情页自行核对并操作。"
            )
        return f"找到你的 {len(items)} 笔最近订单。点击卡片可查看详情或继续处理。"
    if plan.intent == "cart_lookup":
        quantity = data.get("cart_total_quantity")
        if not isinstance(quantity, int) or quantity <= 0:
            return "你的购物车还是空的。可以先去逛逛，遇到喜欢的商品再加入购物车。"
        selected = data.get("selected_quantity")
        return (
            f"购物车里共有 {quantity} 件商品"
            + (f"，已选 {selected} 件" if isinstance(selected, int) else "")
            + "。我已整理在卡片里，点击即可检查商品并结算。"
        )
    if plan.intent == "logistics_lookup":
        if not isinstance(items, list) or not items:
            return "该订单当前没有可见物流包裹。"
        return f"已更新 {len(items)} 个物流包裹的最新进度。点击卡片可以查看完整物流。"
    if plan.intent == "refund_precheck":
        eligibility_value = data.get("refund_eligibility")
        eligibility: Mapping[str, Any] = (
            eligibility_value if isinstance(eligibility_value, Mapping) else {}
        )
        eligible = eligibility.get("eligible") is True
        lines = [
            (
                "资格检查完成: 当前可以申请售后。金额、类型和下一步入口已整理在卡片中。"
                if eligible
                else "资格检查完成: 当前暂不能申请售后，具体原因已整理在卡片中。"
            )
        ]
        lines.append("本次只完成资格检查，没有创建退款草稿或售后单。")
        return "\n".join(lines)
    if plan.intent == "refund_progress":
        if "refund_id" in data:
            return "已找到这笔售后申请。点击卡片可以查看当前节点和处理记录。"
        if not isinstance(items, list) or not items:
            return "你的账号下暂未查询到售后申请。"
        return f"找到 {len(items)} 笔最近售后申请。点击卡片可以查看处理进度。"
    if plan.intent == "policy_qa":
        knowledge = data.get("knowledge_sources")
        if (not isinstance(items, list) or not items) and not (
            isinstance(knowledge, list) and knowledge
        ):
            return "暂未找到可可靠引用的已发布平台规则，请前往帮助中心或转平台人工客服。"
        sources = [
            (item.get("title"), item.get("content"))
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict)
        ]
        sources.extend(
            (item.get("title"), item.get("excerpt"))
            for item in (knowledge if isinstance(knowledge, list) else [])
            if isinstance(item, dict)
        )
        return concise_policy_answer(
            user_text,
            sources,
            intro="根据当前已发布平台规则",
        )
    return "已完成查询。"


def _comparison_purchase_conclusion(
    user_text: str, items: list[object]
) -> str | None:
    """Give a bounded recommendation only when public evidence separates options."""

    normalized = re.sub(r"\s+", "", user_text).casefold()
    if "考试" not in normalized:
        return None
    evidence_terms = ("考试", "铅笔", "中性笔", "笔芯", "橡皮", "直尺", "答题", "书写")
    ranked: list[tuple[int, str]] = []
    for item in items[:2]:
        if not isinstance(item, Mapping):
            continue
        name = safe_untrusted_excerpt(item.get("name") or "商品", 100)
        evidence = " ".join(
            (
                name,
                safe_untrusted_excerpt(item.get("subtitle") or "", 160),
                safe_untrusted_excerpt(item.get("description") or "", 240),
            )
        ).casefold()
        ranked.append((sum(term in evidence for term in evidence_terms), name))
    if len(ranked) != 2 or ranked[0][0] == ranked[1][0] or max(ranked)[0] <= 0:
        return None
    winner = max(ranked, key=lambda value: value[0])[1]
    return (
        f"按商家公开信息，“{winner}”更贴近考试使用场景。"
        "价格、款式和实时库存已放在下方对比卡片中，能否携带仍以具体考试规定为准。"
    )


def _source_refs(data: Mapping[str, Any]) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    for key, resource_type in (
        ("order_id", "order"),
        ("refund_id", "refund"),
        ("product_id", "product"),
    ):
        value = data.get(key)
        if isinstance(value, str):
            refs.append({"type": resource_type, "id": value})
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
    memories = data.get("recalled_memories")
    if isinstance(memories, list):
        for item in memories[:5]:
            if not isinstance(item, dict) or not isinstance(item.get("memory_id"), str):
                continue
            refs.append(
                {
                    "type": "memory",
                    "id": item["memory_id"],
                    "memory_type": item.get("memory_type"),
                }
            )
    return refs


async def _attach_platform_knowledge(
    mysql: AsyncSession,
    checkpoint_store: AgentCheckpointStore,
    context: TrustedExclusiveAgentContext,
    intent: str,
    data: dict[str, object],
) -> None:
    if intent != "policy_qa":
        return
    context.run.current_phase = "retrieving"
    context.run.version += 1
    try:
        result = await KnowledgeService(mysql, checkpoint_store.session).search_for_agent(
            query=context.trigger.text_content or "平台规则",
            scope_type="platform",
            scope_no="platform",
            limit=6,
            trace_id=context.run.trace_id,
        )
    except SQLAlchemyError:
        await checkpoint_store.session.rollback()
        data["rag"] = {
            "scope": "platform:platform",
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
    data["policy_query"] = context.trigger.text_content or "平台规则"
    data["rag"] = {
        "scope": "platform:platform",
        "returned_count": len(result.items),
        "degraded": result.degraded,
        "retrieval_mode": "keyword_only" if result.degraded else "hybrid",
    }


async def _attach_exclusive_memories(
    mysql: AsyncSession,
    checkpoint_store: AgentCheckpointStore,
    security: SecurityService,
    context: TrustedExclusiveAgentContext,
    intent: str,
    data: dict[str, object],
) -> None:
    if intent != "personalized_recommendation":
        return
    context.run.current_phase = "recalling"
    context.run.version += 1
    try:
        settings = get_settings()
        recall = await AgentMemoryRuntime(
            mysql,
            checkpoint_store.session,
            security,
            embedding_provider(settings),
            settings.memory_min_vector_similarity,
        ).recall_exclusive(
            context.user,
            query=context.trigger.text_content or "购物偏好",
            limit=3,
        )
    except SQLAlchemyError:
        await checkpoint_store.session.rollback()
        data["memory"] = {
            "scope": "exclusive",
            "authorized": True,
            "used_count": 0,
            "degraded": True,
            "error_code": "MEMORY_RECALL_UNAVAILABLE",
        }
        return
    data["recalled_memories"] = [
        {
            "memory_id": item.memory_no,
            "memory_type": item.memory_type,
            "memory_key": item.memory_key,
            "value": item.value,
            "relevance": round(item.relevance, 4),
            "expires_at": item.expires_at.isoformat(),
            "freshness_notice": "偏好可能已变化，不作为订单、库存或价格事实",
        }
        for item in recall.items
    ]
    data["memory"] = {
        "scope": "exclusive",
        "authorized": recall.authorized,
        "used_count": len(recall.items),
        "degraded": recall.degraded,
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


def _clarification_trace(plan: ExclusiveAgentPlan) -> dict[str, object]:
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


def _nested_value(value: Mapping[str, Any], outer: str, inner: str) -> object:
    nested = value.get(outer)
    return nested.get(inner) if isinstance(nested, dict) else None


def _money_display(value: Mapping[str, Any], key: str) -> str:
    amounts = value.get("amounts")
    money = amounts.get(key) if isinstance(amounts, dict) else None
    display = money.get("display") if isinstance(money, dict) else None
    return str(display) if display else "¥0.00"


def _price_display(price: Mapping[str, Any]) -> str:
    currency = str(price.get("currency") or "CNY").upper()
    symbol = "¥" if currency == "CNY" else f"{currency} "
    return f"{symbol}{int(price.get('min_amount', 0)) / 100:.2f}"


def _money_object_display(money: Mapping[str, Any]) -> str:
    currency = str(money.get("currency") or "CNY").upper()
    symbol = "¥" if currency == "CNY" else f"{currency} "
    return f"{symbol}{int(money.get('minor_units', 0)) / 100:.2f}"


def _compact_tracking_no(value: object) -> str:
    text = safe_untrusted_excerpt(value or "物流单号同步中", 80)
    if text.count("*") <= 8:
        return text
    visible = "".join(character for character in text if character != "*")
    return f"尾号 {visible[-4:]}" if visible else "物流单号已脱敏"


_STATUS_LABELS: dict[str, dict[str, str]] = {
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
        "created": "已发货，待揽收",
        "picked_up": "已揽收",
        "in_transit": "运输中",
        "delivered": "已签收",
        "exception": "物流异常",
        "returned": "已退回",
        "closed": "已关闭",
        "voided": "已作废",
    },
    "refund": {
        "submitted": "已提交",
        "merchant_review": "商家处理中",
        "approved": "已同意",
        "waiting_return": "等待退货",
        "returning": "退货运输中",
        "received": "商家已收货",
        "refunding": "退款中",
        "succeeded": "退款成功",
        "rejected": "已拒绝",
        "cancelled": "已取消",
        "closed": "已关闭",
    },
}


def _status_label(kind: str, value: object) -> str:
    text = str(value or "未知")
    return _STATUS_LABELS.get(kind, {}).get(text, text)


def _last_track_text(item: Mapping[str, Any]) -> str:
    track = item.get("last_track")
    if not isinstance(track, dict):
        return ""
    description = safe_untrusted_excerpt(track.get("description"), 180)
    location = safe_untrusted_excerpt(track.get("location_text"), 120)
    details = "; " + description if description else ""
    if location:
        details += f"，当前位置 {location}"
    return details


def _resource_no(text: str, prefix: str) -> str | None:
    match = re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(prefix)}_([0-9A-Z]{{10,32}})(?![A-Za-z0-9_])",
        text,
        flags=re.I,
    )
    return f"{prefix}_{match.group(1).upper()}" if match is not None else None


def _requests_direct_refund_payout(user_text: str) -> bool:
    """Block requests to bypass the refund application and approval workflow."""

    compact = re.sub(r"\s+", "", user_text).casefold()
    return any(
        marker in compact
        for marker in (
            "直接把钱退给我",
            "直接退款",
            "立即退款",
            "马上退款",
            "跳过审核退款",
            "不用确认退款",
        )
    )


def _delivery_estimate_text(item: Mapping[str, Any]) -> str:
    value = item.get("delivery_estimate")
    if not isinstance(value, dict) or value.get("status") != "available":
        return "; 暂无可靠预计送达时间"
    minimum = value.get("min_at")
    maximum = value.get("max_at")
    source = value.get("source")
    if not isinstance(minimum, str) or not isinstance(maximum, str):
        return "; 暂无可靠预计送达时间"
    source_label = "承运商" if source == "carrier" else "配送模板"
    return f"; 预计送达 {minimum} 至 {maximum} (来源: {source_label}, 仅供参考)"


def _checkpoint_state(
    context: TrustedExclusiveAgentContext, *, intent: str | None
) -> dict[str, object]:
    state: dict[str, object] = {
        "run_no": context.run.run_no,
        "conversation_no": context.conversation.conversation_no,
        "trigger_message_no": context.trigger.message_no,
        "user_no": context.user.user_no,
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
    checkpoint_store: AgentCheckpointStore,
    context: TrustedExclusiveAgentContext,
    intent: str,
) -> None:
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


def _fail_run(run: AgentRun, code: str) -> None:
    run.run_status = "failed"
    run.current_phase = "failed"
    run.error_code = code
    run.version += 1
