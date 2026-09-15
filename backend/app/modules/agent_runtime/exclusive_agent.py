from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any
from zoneinfo import ZoneInfo

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
from app.modules.agent_runtime.conversation_state import ConversationStateRuntime
from app.modules.agent_runtime.conversation_summary import attach_rolling_summary
from app.modules.agent_runtime.deadline import AgentStreamGate, hard_deadline
from app.modules.agent_runtime.delegation import (
    DelegationBudget,
    DelegationPacket,
    DelegationPlan,
    DelegationTrace,
    MultiAgentOrchestrator,
    SpecialistResult,
    TrustedDelegationScope,
)
from app.modules.agent_runtime.delegation_ledger import SessionDelegationLedger
from app.modules.agent_runtime.exclusive_context import (
    ExclusiveContextBuilder,
    TrustedExclusiveAgentContext,
)
from app.modules.agent_runtime.exclusive_model_gateway import (
    DeterministicExclusiveModelGateway,
    ExclusiveAgentPlan,
    ExclusiveIntent,
    ExclusiveModelGateway,
    ExclusiveSupervisorPlan,
    ExclusiveSupervisorSubtask,
    _compound_intent_coverage,
    _requests_profile_lookup,
    _strip_negated_intent_phrases,
    complete_exclusive_plan,
)
from app.modules.agent_runtime.exclusive_tools import (
    ExclusiveToolGateway,
    _explicit_catalog_colors,
    _preferred_catalog_colors,
    _requests_order_state_overview,
    catalog_query_with_inherited_constraints,
)
from app.modules.agent_runtime.memory_runtime import (
    AgentMemoryRuntime,
    explicit_memory_request,
    is_sensitive_explicit_memory_request,
)
from app.modules.agent_runtime.model_gateway import ModelGatewayError, requests_other_user_data
from app.modules.agent_runtime.models import AgentRun, AgentToolApproval
from app.modules.agent_runtime.order_cards import (
    build_order_cards,
    order_nos_from_result,
    order_reference_index,
    order_reference_indices,
    recent_agent_order_nos,
    referenced_order_no,
    referenced_recent_order_no,
    requests_direct_transaction_action,
)
from app.modules.agent_runtime.product_cards import (
    build_product_cards,
    is_short_affirmative,
    product_card_reference_index,
    product_card_reference_indices,
    product_nos_from_result,
    recent_agent_product_cards,
    recent_named_agent_product_cards,
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
from app.modules.messaging.models import Conversation, Message
from app.modules.messaging.sequence import lock_conversation_for_append
from app.modules.system.models import OutboxEvent

# Bound individual model stages so tool-backed cards and deterministic safety replies
# remain responsive when the external provider is slow or partially unavailable.
MODEL_PLANNING_BUDGET_SECONDS = 12.0
MODEL_ANSWER_BUDGET_SECONDS = 20.0


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
    blocked_other_user_clause = requests_other_user_data(trigger_text)
    safe_self_scope_text = (
        _explicit_self_scope_fallback(trigger_text) if blocked_other_user_clause else None
    )
    if detects_prompt_injection(trigger_text):
        await _complete(
            session,
            context,
            "检测到可能要求绕过系统规则或泄露敏感信息的指令，我无法执行该请求，本次不会调用业务工具。你可以重新描述正常的平台、订单、物流或售后问题。",
            error_code="AI_PROMPT_INJECTION_BLOCKED",
            degraded_reason="prompt_injection_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    if _requests_harmful_illegal_guidance(trigger_text):
        await _complete(
            session,
            context,
            "我不能帮助购买毒品，也不能提供伤害他人的方法或操作指导。"
            "如果你正处于可能伤害自己或他人的紧急情形，请立即远离危险物品并联系当地紧急服务或可信赖的人。",
            error_code="AI_HARMFUL_REQUEST_BLOCKED",
            degraded_reason="harmful_request_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    if blocked_other_user_clause and safe_self_scope_text is None:
        await _complete(
            session,
            context,
            "我只能读取当前登录账号本人的订单、物流、售后、购物车和收藏，不能查看其他用户的数据。",
            error_code="AI_OTHER_USER_DATA_BLOCKED",
            degraded_reason="data_scope_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    if safe_self_scope_text is not None:
        # Execute only the explicitly requested current-account fallback.  The
        # original message remains in the immutable conversation and trace,
        # while no noun from the rejected other-user clause reaches planning or
        # a business tool.
        trigger_text = safe_self_scope_text
    if is_sensitive_explicit_memory_request(trigger_text):
        await _complete(
            session,
            context,
            "这类敏感信息不能写入长期记忆。银行卡号、密码、验证码、证件、邮箱、"
            "手机号和详细地址都不会保存。请只让我记住低敏、稳定的购物偏好，例如颜色、"
            "风格或预算范围。",
            error_code="AI_SENSITIVE_MEMORY_BLOCKED",
            degraded_reason="sensitive_memory_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    protected_transaction_requested = requests_direct_transaction_action(trigger_text)
    protected_transaction_has_safe_reads = len(_compound_intent_coverage(trigger_text)) >= 2
    if protected_transaction_requested and not protected_transaction_has_safe_reads:
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
    if _asks_human_service_capabilities(trigger_text):
        await _complete(
            session,
            context,
            (
                "平台人工客服可以进一步处理需要人工核实或协调的问题，例如订单争议、"
                "复杂售后、退款申诉、账号异常和平台投诉。一般的商品搜索、本人订单、"
                "物流与售后资格，我可以先替你查询。只有你明确要求转人工时才会创建工单。"
            ),
        )
        await _finish_checkpoint(checkpoint_store, context, "human_service_information")
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
            memory_content: dict[str, object] = {
                "memory_id": candidate.memory_no,
                "memory_type": candidate.memory_type,
                "memory_key": candidate.memory_key,
                "memory_value": candidate.value,
                "memory_status": "candidate",
                "memory_version": candidate.version,
                "expires_at": candidate.expires_at.isoformat(),
            }
            completion_text = (
                "我已把你明确表达的购物偏好整理为候选。它现在还不会被召回，"
                "只有你点击下方“确认记住”后才会生效。"
            )
            trace_steps: list[dict[str, object]] = [
                {"kind": "security", "label": "检查授权与敏感信息", "status": "completed"},
                {"kind": "memory", "label": "创建加密候选记忆", "status": "completed"},
            ]
            audit_data: dict[str, object] = {}
            if _requests_recommendation_with_memory(trigger_text):
                tool_gateway = ExclusiveToolGateway(session, settings, security)
                recommendation_query = _memory_recommendation_query(requested_memory, trigger_text)
                recommendation = await tool_gateway.search_products(
                    context,
                    recommendation_query,
                    fallback_query=recommendation_query,
                )
                audit_data = {
                    "recommendation": recommendation.data,
                    "_audit_tool_calls": list(tool_gateway.execution_records),
                }
                if recommendation.status == "succeeded":
                    product_cards = await build_product_cards(
                        session,
                        context.conversation,
                        product_nos_from_result(recommendation.data),
                        sku_nos_by_product=_preferred_sku_nos(
                            recommendation.data, recommendation_query
                        ),
                    )
                    if product_cards:
                        memory_content["product_cards"] = product_cards
                        completion_text += (
                            f" 同时按这次条件找到 {len(product_cards)} 件商品，"
                            "推荐结果不依赖尚未确认的长期记忆。"
                        )
                    else:
                        completion_text += (
                            " 同时执行了商品推荐，但当前没有完全满足这些条件的在售商品，"
                            "我没有用不相关商品凑数。你可以放宽颜色、预算或品类中的一个条件。"
                        )
                trace_steps.append(
                    {
                        "kind": "tool",
                        "label": "按本轮偏好查询在售商品",
                        "status": recommendation.status,
                        "tool_code": "catalog.search_products",
                    }
                )
            trace_steps.append(
                {"kind": "answer", "label": "等待用户明确确认", "status": "completed"}
            )
            await _complete(
                session,
                context,
                completion_text,
                data=audit_data,
                message_type="memory_candidate",
                extra_content=memory_content,
                execution_trace=public_trace(
                    run_id=context.run.run_no,
                    agent="专属客服",
                    model=context.agent_version.model_profile,
                    question=agent_trace_question(context.trigger),
                    intent="memory_candidate",
                    data=audit_data,
                    steps=trace_steps,
                    source_ids=(
                        ("tool:catalog.search_products",)
                        if _requests_recommendation_with_memory(trigger_text)
                        else ()
                    ),
                    tool_code=(
                        "catalog.search_products"
                        if _requests_recommendation_with_memory(trigger_text)
                        else None
                    ),
                ),
            )
        await _finish_checkpoint(checkpoint_store, context, "memory_candidate")
        return
    approval = await session.scalar(
        select(AgentToolApproval).where(AgentToolApproval.run_id == run.id)
    )
    if approval is not None:
        if approval.action_type == "cart_clear":
            await _resume_cart_clear_approval(
                session,
                context,
                approval,
                settings,
                security,
                checkpoint_store,
            )
        else:
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
    context_window = context_window.with_conversation_state(
        await ConversationStateRuntime(session).load(
            context.conversation,
            before_sequence=context.trigger.sequence_no,
        )
    )
    if is_short_affirmative(trigger_text) and await _recent_catalog_no_result(
        session,
        context.conversation,
        before_sequence=context.trigger.sequence_no,
    ):
        await _complete(
            session,
            context,
            "可以。你想放宽哪一项: 品类、季节、预算、尺码，还是先联系店铺确认? "
            "我会保留上一轮的其余条件，不会擅自更换人群或商品类型。",
            execution_trace=_clarification_trace(
                ExclusiveAgentPlan(
                    "product_search",
                    missing_slots=("constraint_to_relax",),
                    response_strategy="clarify",
                )
            ),
        )
        await _finish_checkpoint(checkpoint_store, context, "product_search")
        return
    deterministic_gateway = DeterministicExclusiveModelGateway()
    deterministic_supervisor = await deterministic_gateway.plan_tasks(trigger_text)
    fast_plan = complete_exclusive_plan(await deterministic_gateway.plan(trigger_text))
    supervisor_plan = deterministic_supervisor
    supervisor_plan_source = "deterministic_supervisor"
    planning_model_trace: dict[str, object] = {
        "status": "not_invoked",
        "provider_request_sent": False,
        "stage": "supervisor_planning",
        "reason": "当前运行使用受控本地规划器。",
    }
    provider_single_plan: ExclusiveAgentPlan | None = None
    if isinstance(gateway, ProviderExclusiveModelGateway) and not (
        _is_unambiguous_general_chat(trigger_text) and fast_plan.intent == "general_chat"
    ):
        planning_started_at = time.monotonic()
        try:
            supervisor_candidate = await hard_deadline(
                gateway.plan_tasks(context_window.planning_input(trigger_text)),
                budget_seconds=MODEL_PLANNING_BUDGET_SECONDS,
            )
            supervisor_plan = _merge_supervisor_plans(
                supervisor_candidate,
                deterministic_supervisor,
                force_deterministic_intent=(
                    fast_plan.intent
                    if (
                        _rule_must_guard_intent(fast_plan.intent)
                        or _must_guard_read_only_order_eligibility(
                            trigger_text,
                            fast_plan.intent,
                        )
                    )
                    else None
                ),
                preserve_compound_coverage=_appears_compound_request(trigger_text),
            )
            supervisor_plan_source = "provider_model_supervisor"
            planning_model_trace = {
                "status": "completed",
                "provider_request_sent": True,
                "stage": "supervisor_planning",
                "model": gateway.model_name,
                "model_latency_ms": int((time.monotonic() - planning_started_at) * 1000),
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "usage_status": "provider_usage_not_returned_by_planning_contract",
            }
            if len(supervisor_plan.tasks) == 1:
                task = supervisor_plan.tasks[0]
                provider_single_plan = complete_exclusive_plan(
                    ExclusiveAgentPlan(
                        task.intent,
                        search_text=(
                            task.objective
                            if task.intent
                            in {
                                "product_search",
                                "product_compare",
                                "personalized_recommendation",
                            }
                            else None
                        ),
                        confidence=supervisor_plan.confidence,
                        continuation_of_previous_turn=_looks_like_contextual_follow_up(
                            trigger_text
                        ),
                    )
                )
        except (ModelGatewayError, TimeoutError) as exc:
            run.degraded_reason = model_failure_code(exc, "supervisor_planning")
            planning_model_trace = {
                "status": "failed",
                "provider_request_sent": True,
                "stage": "supervisor_planning",
                "model": gateway.model_name,
                "model_latency_ms": int((time.monotonic() - planning_started_at) * 1000),
                "error_code": run.degraded_reason,
                "fallback_used": True,
            }
    if len(supervisor_plan.tasks) >= 2:
        if await _execute_exclusive_supervisor_plan(
            session,
            context,
            supervisor_plan,
            settings=settings,
            security=security,
            checkpoint_store=checkpoint_store,
            context_window=context_window,
            plan_source=supervisor_plan_source,
            planning_model_trace=planning_model_trace,
            model_gateway=gateway,
            stream_callback=stream_callback,
        ):
            return
    if fast_plan.intent == "general_chat" and _looks_like_cart_quantity_follow_up(trigger_text):
        recent_agent_intent = await _recent_agent_intent(
            session,
            context.conversation,
            before_sequence=context.trigger.sequence_no,
        )
        if recent_agent_intent in {
            "cart_lookup",
            "cart_add",
            "cart_update",
            "cart_remove",
            "checkout_preview",
        }:
            fast_plan = complete_exclusive_plan(
                ExclusiveAgentPlan(
                    "cart_lookup",
                    continuation_of_previous_turn=True,
                )
            )
    if fast_plan.intent == "order_lookup" and order_reference_index(trigger_text) is not None:
        recent_agent_intent = await _recent_agent_intent(
            session,
            context.conversation,
            before_sequence=context.trigger.sequence_no,
        )
        if recent_agent_intent == "refund_precheck" and (
            _is_implicit_refund_precheck_follow_up(trigger_text)
        ):
            fast_plan = complete_exclusive_plan(
                ExclusiveAgentPlan("refund_precheck", continuation_of_previous_turn=True)
            )
        elif recent_agent_intent == "logistics_lookup" and _is_bare_order_choice(trigger_text):
            fast_plan = complete_exclusive_plan(
                ExclusiveAgentPlan("logistics_lookup", continuation_of_previous_turn=True)
            )
    # A hypothetical cart quantity question is a read-only projection.  The
    # provider supervisor may reasonably associate words such as "总价" with a
    # checkout preview, but creating a checkout session would both ignore the
    # hypothetical quantity and introduce an unnecessary write.  Keep this
    # boundary deterministic and non-overridable by the model.
    plan, single_plan_source = _single_plan_with_non_overridable_guards(
        trigger_text,
        provider_plan=provider_single_plan,
        fallback_plan=fast_plan,
    )
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
            recalled_preference_data: dict[str, object] = {}
            if plan.intent == "personalized_recommendation":
                await _attach_exclusive_memories(
                    session,
                    checkpoint_store,
                    security,
                    context,
                    plan.intent,
                    recalled_preference_data,
                )
            if "product" in context.context_refs:
                # Conversational focus can override which card a pronoun means,
                # but it cannot bypass optimistic validation of the page context
                # captured with this message.
                await builder.require_active_context(context, "product")
            reference_index = product_card_reference_index(trigger_text)
            all_reference_indices = product_card_reference_indices(trigger_text)
            recent_cards = await recent_agent_product_cards(
                session,
                context.conversation,
                before_sequence=context.trigger.sequence_no,
                minimum_count=(
                    max(all_reference_indices) + 1
                    if len(all_reference_indices) >= 2
                    else max(1, reference_index + 1)
                    if reference_index is not None
                    else 1
                ),
            )
            recent_reference = referenced_product_card(
                trigger_text,
                recent_cards,
            )
            if recent_reference is None:
                recent_reference = _comparative_product_card(trigger_text, recent_cards)
            if (
                recent_reference is None
                and len(recent_cards) == 1
                and _requests_sku_stock_extreme(trigger_text)
            ):
                recent_reference = recent_cards[0]
            card_name = _trigger_payload_value(context.trigger, "product_card", "product_name")
            referenced_name = (
                card_name
                if card_name is not None
                else recent_reference.get("product_name")
                if recent_reference is not None
                else None
            )
            previous_catalog_text = _previous_catalog_constraint_text(context_window)
            effective_catalog_text = catalog_query_with_inherited_constraints(
                trigger_text,
                previous_catalog_text,
            )
            recalled_memory_values = recalled_preference_data.get("recalled_memories")
            recalled_values = [
                safe_untrusted_excerpt(item.get("value") or "", 160)
                for item in (
                    recalled_memory_values if isinstance(recalled_memory_values, list) else []
                )
                if isinstance(item, Mapping) and item.get("value")
            ]
            # Confirmed memories are soft ranking hints, never hard catalogue
            # constraints.  Keep the original request as the server-enforced
            # fallback so a preference such as “深蓝、静音” cannot erase an
            # explicitly named product from the result set.
            search_text = plan.search_text
            if recalled_values:
                search_text = (
                    f"{search_text or trigger_text} 已确认购物偏好: "
                    + " | ".join(recalled_values)
                )[:500]
            focused_product_no = (
                recent_reference.get("product_id")
                if isinstance(recent_reference, Mapping)
                else None
            )
            focused_sku_question = (
                isinstance(focused_product_no, str)
                and any(
                    marker in re.sub(r"\s+", "", trigger_text).casefold()
                    for marker in ("库存", "有货", "缺货", "款式", "规格", "尺码", "码数")
                )
            )
            if focused_sku_question:
                result = await tools.compare_products(context, [str(focused_product_no)])
            elif isinstance(focused_product_no, str) and _selects_existing_product_only(
                trigger_text
            ):
                result = await tools.compare_products(context, [focused_product_no])
                result.data["catalog_reselection"] = True
                referenced_sku_no = (recent_reference or {}).get("sku_id")
                if isinstance(referenced_sku_no, str):
                    result.data["_preferred_sku_nos"] = {
                        focused_product_no: referenced_sku_no
                    }
            elif _continues_catalog_constraints(trigger_text) and recent_cards:
                # A no-result correction must not erase an earlier visual SKU
                # constraint (for example blue).  The most recent product cards
                # are the user's visible conversational evidence, so carry only
                # their bounded SKU labels into the next filter operation.
                visible_sku_labels = " ".join(
                    str(card.get("sku_name") or "") for card in recent_cards if card.get("sku_name")
                )
                effective_catalog_text = catalog_query_with_inherited_constraints(
                    effective_catalog_text,
                    visible_sku_labels,
                )
                result = await tools.filter_recent_products(
                    context,
                    [
                        str(card["product_id"])
                        for card in recent_cards
                        if isinstance(card.get("product_id"), str)
                    ],
                    effective_catalog_text,
                )
                requested_colors = _explicit_catalog_colors(effective_catalog_text)
                preserved_skus = {
                    str(card["product_id"]): str(card["sku_id"])
                    for card in recent_cards
                    if isinstance(card.get("product_id"), str)
                    and isinstance(card.get("sku_id"), str)
                    and any(
                        color in str(card.get("sku_name") or "").casefold()
                        for color in requested_colors
                    )
                }
                if preserved_skus:
                    result.data["_preferred_sku_nos"] = preserved_skus
            else:
                result = await tools.search_products(
                    context,
                    str(referenced_name) if isinstance(referenced_name, str) else search_text,
                    fallback_query=effective_catalog_text,
                )
            if result.status == "succeeded":
                result.data.update(recalled_preference_data)
                result_items = result.data.get("items")
                requested_colors = _explicit_catalog_colors(trigger_text)
                if (
                    isinstance(recent_reference, Mapping)
                    and isinstance(result_items, list)
                    and not result_items
                    and requested_colors
                    and isinstance(recent_reference.get("product_id"), str)
                ):
                    result.data["product_id"] = recent_reference["product_id"]
                    result.data["focused_product_name"] = referenced_name
                    result.data["unavailable_variant_labels"] = list(requested_colors)
                result.data["effective_catalog_query"] = effective_catalog_text
                result.data["presentation"] = "product_cards"
                if any(
                    term in re.sub(r"\s+", "", trigger_text).casefold()
                    for term in ("库存", "有货", "缺货", "款式", "规格", "尺码", "码数")
                ):
                    result.data["catalog_focus"] = "sku_availability"
        elif plan.intent == "product_compare":
            reference_indices = product_card_reference_indices(trigger_text)
            minimum_count = max(reference_indices) + 1 if len(reference_indices) >= 2 else 2
            recent_cards = await recent_agent_product_cards(
                session,
                context.conversation,
                before_sequence=context.trigger.sequence_no,
                minimum_count=minimum_count,
            )
            chosen_cards = (
                [
                    recent_cards[index]
                    for index in reference_indices[:3]
                    if index < len(recent_cards)
                ]
                if len(reference_indices) >= 2
                else recent_cards[: 3 if _contains_product_collection_of_three(trigger_text) else 2]
            )
            if len(chosen_cards) < 2 and not reference_indices:
                named_cards = await recent_named_agent_product_cards(
                    session,
                    context.conversation,
                    before_sequence=context.trigger.sequence_no,
                    user_text=trigger_text,
                    maximum_count=(3 if _contains_product_collection_of_three(trigger_text) else 2),
                )
                if len(named_cards) >= 2:
                    chosen_cards = named_cards
            product_nos = [
                str(card["product_id"])
                for card in chosen_cards
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
                preserved_skus = {
                    str(card["product_id"]): str(card["sku_id"])
                    for card in chosen_cards
                    if isinstance(card.get("product_id"), str)
                    and isinstance(card.get("sku_id"), str)
                }
                if preserved_skus:
                    result.data["_preferred_sku_nos"] = preserved_skus
        elif plan.intent == "order_lookup":
            explicit_order_no = _trigger_payload_value(
                context.trigger, "order_card", "order_id"
            ) or _resource_no(trigger_text, "ord")
            recent_reference_no: str | None = None
            eligibility_actions = _requested_order_eligibility_actions(trigger_text)
            wants_order_list = (
                _requests_order_list(trigger_text)
                or bool(eligibility_actions)
                or _references_purchased_item(trigger_text)
                or _has_explicit_order_state_scope(trigger_text)
            )
            reference_index = order_reference_index(trigger_text)
            if explicit_order_no is None and reference_index is not None:
                visible_order_nos = await recent_agent_order_nos(
                    session,
                    context.conversation,
                    before_sequence=context.trigger.sequence_no,
                    minimum_count=reference_index + 1,
                )
                if not visible_order_nos:
                    await _complete(
                        session,
                        context,
                        "刚才的查询没有展示任何订单，因此不存在可选的第一笔或第二笔。请重新说明订单状态、商品或店铺，我再为你查询。",
                        execution_trace={
                            "intent": "order_lookup",
                            "steps": [
                                {
                                    "kind": "context",
                                    "label": "检测到最近订单查询为空，停止引用旧订单",
                                    "status": "completed",
                                }
                            ],
                        },
                    )
                    await _finish_checkpoint(checkpoint_store, context, "order_lookup")
                    return
            if explicit_order_no is None and not wants_order_list:
                if _requests_latest_order(trigger_text):
                    recent_reference_no = await tools.latest_order_no(context)
                else:
                    recent_order_nos = await recent_agent_order_nos(
                        session,
                        context.conversation,
                        before_sequence=context.trigger.sequence_no,
                        minimum_count=max(
                            reference_index + 1 if reference_index is not None else 1,
                            2 if _corrects_order_reference(trigger_text) else 1,
                        ),
                    )
                    recent_reference_no = referenced_order_no(trigger_text, recent_order_nos)
                    if recent_reference_no is None:
                        recent_reference_no = await referenced_recent_order_no(
                            session,
                            context.conversation,
                            before_sequence=context.trigger.sequence_no,
                            user_text=trigger_text,
                        )
            ref = context.context_refs.get("order")
            result = (
                await tools.order_detail(context, explicit_order_no)
                if explicit_order_no is not None
                else await tools.list_orders(context, trigger_text)
                if wants_order_list
                else await tools.order_detail(context, recent_reference_no)
                if recent_reference_no is not None
                else await tools.order_detail(
                    context, (await builder.require_active_context(context, "order")).resource_no
                )
                if ref is not None
                else await tools.list_orders(context, trigger_text)
            )
            if result.status == "succeeded":
                if eligibility_actions and "order_id" not in result.data:
                    raw_items = result.data.get("items")
                    items = raw_items if isinstance(raw_items, list) else []
                    matching_items = [
                        item
                        for item in items
                        if isinstance(item, Mapping)
                        and isinstance(item.get("available_actions"), list)
                        and any(
                            action in item["available_actions"] for action in eligibility_actions
                        )
                    ]
                    result.data["eligibility_match_items"] = matching_items
                    if not _asks_order_status_and_eligibility(trigger_text):
                        result.data["items"] = matching_items
                    result.data["requested_eligibility_actions"] = eligibility_actions
                result.data["presentation"] = (
                    "order_card" if "order_id" in result.data else "order_cards"
                )
        elif plan.intent == "cart_add":
            reference_index = product_card_reference_index(trigger_text)
            recent_cards = await recent_agent_product_cards(
                session,
                context.conversation,
                before_sequence=context.trigger.sequence_no,
                minimum_count=max(1, (reference_index or 0) + 1),
            )
            selected_card = referenced_product_card(trigger_text, recent_cards)
            if selected_card is None and len(recent_cards) == 1:
                selected_card = recent_cards[0]
            sku_no = selected_card.get("sku_id") if selected_card is not None else None
            requested_variant: Mapping[str, Any] | None = None
            variant_result: StoreToolResult | None = None
            selected_product_no = (
                selected_card.get("product_id") if selected_card is not None else None
            )
            if isinstance(selected_product_no, str):
                variant_result = await tools.compare_products(context, [selected_product_no])
                if variant_result.status == "succeeded":
                    requested_variant = _requested_product_sku(variant_result.data, trigger_text)
                    if requested_variant is not None:
                        variant_sku_no = requested_variant.get("sku_id")
                        if isinstance(variant_sku_no, str):
                            sku_no = variant_sku_no
                        if int(requested_variant.get("available_stock") or 0) <= 0:
                            result = variant_result
                            result.data["cart_add_unavailable_variant"] = {
                                "product_name": (selected_card or {}).get("product_name"),
                                "sku_name": requested_variant.get("sku_name"),
                            }
                            result.data["catalog_focus"] = "sku_availability"
                            result.data["presentation"] = "product_cards"
                            sku_no = None
            if not isinstance(sku_no, str):
                if not (
                    variant_result is not None
                    and variant_result.status == "succeeded"
                    and requested_variant is not None
                ):
                    await _complete(
                        session,
                        context,
                        "请先让我展示商品，或发送一张商品卡片，再告诉我要加入第几个商品和数量。",
                        execution_trace=_clarification_trace(
                            ExclusiveAgentPlan(
                                "cart_add",
                                missing_slots=("product_choice",),
                                response_strategy="clarify",
                            )
                        ),
                    )
                    await _finish_checkpoint(checkpoint_store, context, plan.intent)
                    return
            else:
                quantity = _requested_cart_add_quantity(trigger_text)
                result = await tools.add_cart_item(context, sku_no, quantity)
                if result.status == "succeeded" and selected_card is not None:
                    result.data["added_product_name"] = selected_card.get("product_name")
                    result.data["added_sku_name"] = (
                        requested_variant.get("sku_name")
                        if requested_variant is not None
                        else selected_card.get("sku_name")
                    )
                    result.data["added_quantity"] = quantity
                    result.data["presentation"] = "cart_card"
        elif plan.intent in {"cart_update", "cart_remove"}:
            cart_snapshot = await tools.get_cart(context)
            cart_selection_text = trigger_text
            recent_add_label: str | None = None
            if _references_recent_cart_add(trigger_text):
                recent_add_label = await _recent_successful_cart_add_label(
                    session,
                    context.conversation,
                    before_sequence=context.trigger.sequence_no,
                )
                if recent_add_label is not None:
                    cart_selection_text = f"{trigger_text} {recent_add_label}"
            selected_item = None
            if cart_snapshot.status == "succeeded" and (
                not _references_recent_cart_add(trigger_text) or recent_add_label is not None
            ):
                selected_item = _select_cart_item(cart_snapshot.data, cart_selection_text)
            cart_version = cart_snapshot.data.get("version")
            if selected_item is None or not isinstance(cart_version, int):
                result = cart_snapshot
                if result.status == "succeeded":
                    result.data["cart_action_clarification"] = True
                    result.data["presentation"] = "cart_card"
            else:
                item_no = selected_item.get("cart_item_id")
                if not isinstance(item_no, str):
                    result = cart_snapshot
                    result.data["cart_action_clarification"] = True
                elif plan.intent == "cart_update":
                    quantity = _requested_cart_add_quantity(trigger_text)
                    result = await tools.update_cart_quantity(
                        context,
                        item_no,
                        quantity,
                        cart_version,
                    )
                    if result.status == "succeeded":
                        result.data["updated_product_name"] = selected_item.get("product_name")
                        result.data["updated_sku_name"] = selected_item.get("sku_name")
                        result.data["updated_quantity"] = quantity
                else:
                    result = await tools.remove_cart_item(context, item_no, cart_version)
                    if result.status == "succeeded":
                        result.data["removed_product_name"] = selected_item.get("product_name")
                        result.data["removed_sku_name"] = selected_item.get("sku_name")
                if result.status == "succeeded":
                    result.data["presentation"] = "cart_card"
        elif plan.intent == "cart_lookup":
            result = await tools.get_cart(context)
            if result.status == "succeeded":
                hypothetical = _cart_hypothetical_projection(trigger_text, result.data)
                if hypothetical is not None:
                    result.data["cart_hypothetical"] = hypothetical
                    result.data["presentation"] = "detail_cards"
                else:
                    result.data["presentation"] = "cart_card"
        elif plan.intent == "checkout_preview":
            result = await tools.create_cart_checkout_preview(context)
            if result.status == "succeeded":
                result.data["presentation"] = "detail_cards"
        elif plan.intent == "favorite_update":
            compact_favorite_text = re.sub(r"\s+", "", trigger_text).casefold()
            targets_store = _targets_store_favorite_update(compact_favorite_text)
            reference_index = product_card_reference_index(trigger_text)
            recent_cards = await recent_agent_product_cards(
                session,
                context.conversation,
                before_sequence=context.trigger.sequence_no,
                minimum_count=max(1, (reference_index or 0) + 1),
            )
            selected_card = referenced_product_card(trigger_text, recent_cards)
            if selected_card is None and len(recent_cards) == 1:
                selected_card = recent_cards[0]
            enabled = _favorite_update_enabled(compact_favorite_text)
            if targets_store:
                favorites_snapshot: StoreToolResult | None = None
                selected_store = (
                    selected_card.get("store")
                    if selected_card is not None and isinstance(selected_card.get("store"), Mapping)
                    else selected_card
                )
                store_no = (
                    selected_store.get("store_id") if isinstance(selected_store, Mapping) else None
                )
                store_name = (
                    selected_store.get("store_name")
                    if isinstance(selected_store, Mapping)
                    else None
                )
                deictic_store_reference = any(
                    marker in compact_favorite_text
                    for marker in ("刚才这家", "刚才的店", "刚才那个店", "这家店铺")
                )
                if not enabled:
                    recent_store_name = (
                        await _recent_store_favorite_action_name(
                            session,
                            context.conversation,
                            before_sequence=context.trigger.sequence_no,
                        )
                        if deictic_store_reference
                        else None
                    )
                    favorites = await tools.list_favorites(context)
                    favorites_snapshot = favorites
                    followed = favorites.data.get("followed_stores")
                    followed_rows = [
                        item
                        for item in (followed if isinstance(followed, list) else [])
                        if isinstance(item, Mapping)
                    ]
                    explicit_candidates = [
                        item
                        for item in followed_rows
                        if str(item.get("store_name") or "") in trigger_text
                    ]
                    recent_candidates = [
                        item
                        for item in followed_rows
                        if recent_store_name is not None
                        and str(item.get("store_name") or "") == recent_store_name
                    ]
                    resolved_candidates = explicit_candidates or recent_candidates
                    if len(resolved_candidates) == 1:
                        store_no = resolved_candidates[0].get("store_id")
                        store_name = resolved_candidates[0].get("store_name")
                    elif not any(item.get("store_id") == store_no for item in followed_rows):
                        # Cancelling an unrelated recent product's store is not
                        # a harmless no-op: it is a false success message. Stop
                        # and ask the user to choose instead.
                        store_no = None
                        store_name = None
                if not isinstance(store_no, str):
                    favorites = favorites_snapshot or await tools.list_favorites(context)
                    favorites_snapshot = favorites
                    followed_value = favorites.data.get("followed_stores")
                    followed = followed_value if isinstance(followed_value, list) else []
                    candidates = [
                        item
                        for item in followed
                        if isinstance(item, Mapping)
                        and (
                            str(item.get("store_name") or "") in trigger_text
                            or (
                                len(followed) == 1
                                and any(
                                    marker in compact_favorite_text
                                    for marker in ("这家店", "这个店铺", "该店铺")
                                )
                            )
                        )
                    ]
                    if len(candidates) == 1:
                        store_no = candidates[0].get("store_id")
                        store_name = candidates[0].get("store_name")
                if not isinstance(store_no, str):
                    result = (
                        favorites_snapshot
                        if favorites_snapshot is not None
                        and favorites_snapshot.status == "succeeded"
                        else StoreToolResult("succeeded", {})
                    )
                    named_store = re.search(
                        r"(?P<name>[\u4e00-\u9fffa-z0-9]{2,}(?:专卖店|旗舰店|商店|店铺))",
                        compact_favorite_text,
                    )
                    if not enabled and named_store is not None:
                        result.data["favorite_store_already_absent"] = named_store.group("name")
                    else:
                        result.data["favorite_action_clarification"] = True
                        result.data["favorite_target_type"] = "store"
                    result.data["presentation"] = "detail_cards"
                else:
                    mutation = await tools.set_store_favorite(
                        context,
                        store_no,
                        enabled=enabled,
                    )
                    if mutation.status != "succeeded":
                        result = mutation
                    else:
                        result = await tools.list_favorites(context)
                        if result.status == "succeeded":
                            result.data["favorite_updated"] = {
                                "store_id": store_no,
                                "store_name": store_name,
                                "target_type": "store",
                                "enabled": enabled,
                            }
                            result.data["presentation"] = "detail_cards"
                selected_card = None
            product_no = selected_card.get("product_id") if selected_card is not None else None
            if targets_store:
                pass
            elif not isinstance(product_no, str):
                result = StoreToolResult(
                    "succeeded",
                    {"favorite_action_clarification": True},
                )
            else:
                mutation = await tools.set_product_favorite(
                    context,
                    product_no,
                    enabled=enabled,
                )
                if mutation.status != "succeeded":
                    result = mutation
                else:
                    result = await tools.list_favorites(context)
                    if result.status == "succeeded":
                        result.data["favorite_updated"] = {
                            "product_id": product_no,
                            "product_name": (
                                selected_card.get("product_name")
                                if selected_card is not None
                                else None
                            ),
                            "enabled": enabled,
                        }
                        result.data["presentation"] = "detail_cards"
        elif plan.intent == "review_draft":
            result = await tools.list_orders(context, "最近一笔待评价订单")
            if result.status == "succeeded":
                result.data["review_draft"] = _review_draft_from_request(trigger_text)
                result.data["presentation"] = "detail_cards"
        elif plan.intent == "cart_clear":
            result = await tools.get_cart(context)
            if result.status == "succeeded":
                cart_clear_quantity = result.data.get("cart_total_quantity")
                if not isinstance(cart_clear_quantity, int) or cart_clear_quantity <= 0:
                    result.data["presentation"] = "cart_card"
                else:
                    service = AgentApprovalService(session, settings, security)
                    approval_data = await service.build_cart_clear_approval(
                        context,
                        result.data,
                    )
                    await _attach_waiting_approval_trace(
                        session,
                        context,
                        intent="cart_clear",
                        data={**result.data, **approval_data},
                        tool_records=tools.execution_records,
                        tool_code="cart.get_mine",
                    )
                    await checkpoint_store.write(
                        run.run_no,
                        "waiting_confirmation",
                        _checkpoint_state(context, intent=plan.intent),
                        status="waiting",
                    )
                    return
        elif plan.intent == "address_lookup":
            wants_profile = _requests_profile_lookup(trigger_text)
            wants_addresses = any(
                marker in re.sub(r"\s+", "", trigger_text).casefold()
                for marker in ("收货地址", "地址簿", "默认地址", "我的地址")
            )
            result = (
                await tools.get_profile(context)
                if wants_profile
                else await tools.list_addresses(context)
            )
            if wants_profile and wants_addresses and result.status == "succeeded":
                address_result = await tools.list_addresses(context)
                if address_result.status == "succeeded":
                    result.data.update(address_result.data)
            if result.status == "succeeded":
                result.data["presentation"] = "address_cards"
        elif plan.intent == "wallet_lookup":
            result = await tools.get_wallet(context)
            if result.status == "succeeded":
                result.data["transaction_limit"] = _requested_wallet_transaction_limit(trigger_text)
                result.data["presentation"] = "detail_cards"
        elif plan.intent == "favorites_lookup":
            result = await tools.list_favorites(context)
            if result.status == "succeeded":
                result.data["presentation"] = "detail_cards"
        elif plan.intent == "memory_lookup":

            async def recall_memories() -> dict[str, object]:
                recall = await AgentMemoryRuntime(
                    session,
                    checkpoint_store.session,
                    security,
                    embedding_provider(settings),
                    settings.memory_min_vector_similarity,
                ).recall_exclusive(
                    context.user,
                    query=trigger_text or "购物偏好",
                    limit=5,
                )
                return {
                    "recalled_memories": [
                        {
                            "memory_id": item.memory_no,
                            "memory_type": item.memory_type,
                            "memory_key": item.memory_key,
                            "value": item.value,
                            "relevance": round(item.relevance, 4),
                            "expires_at": item.expires_at.isoformat(),
                        }
                        for item in recall.items
                    ],
                    "memory": {
                        "scope": "exclusive",
                        "authorized": recall.authorized,
                        "used_count": len(recall.items),
                        "degraded": recall.degraded,
                    },
                    "presentation": "detail_cards",
                }

            result = await tools.execute(
                context,
                "memory.list_mine",
                {},
                recall_memories,
            )
        elif plan.intent == "logistics_lookup":
            unscoped_logistics_order_no: str | None = None
            if _requests_unscoped_logistics_lookup(trigger_text):
                visible_orders = await tools.list_orders(context)
                visible_items = visible_orders.data.get("items")
                shipped_order_nos = order_nos_from_result(
                    {
                        "items": [
                            item
                            for item in (visible_items if isinstance(visible_items, list) else [])
                            if isinstance(item, Mapping)
                            and isinstance(item.get("status"), Mapping)
                            and item["status"].get("order") == "shipped"
                        ]
                    }
                )
                if len(shipped_order_nos) > 1:
                    await _complete_order_choice(
                        session,
                        context,
                        checkpoint_store,
                        plan,
                        shipped_order_nos,
                    )
                    return
                if len(shipped_order_nos) == 1:
                    unscoped_logistics_order_no = shipped_order_nos[0]
            if _disclaims_specific_order(trigger_text):
                visible_orders = await tools.list_orders(context)
                visible_items = visible_orders.data.get("items")
                delivery_items = [
                    item
                    for item in (visible_items if isinstance(visible_items, list) else [])
                    if isinstance(item, Mapping)
                    and isinstance(item.get("status"), Mapping)
                    and item["status"].get("order") in {"pending_shipment", "shipped"}
                ]
                candidate_order_nos = order_nos_from_result({"items": delivery_items})
                if candidate_order_nos:
                    await _complete_order_choice(
                        session,
                        context,
                        checkpoint_store,
                        plan,
                        candidate_order_nos,
                    )
                else:
                    await _complete(
                        session,
                        context,
                        "你当前没有待发货或运输中的可见订单，因此没有需要选择的物流订单。",
                        execution_trace={
                            "intent": "logistics_lookup",
                            "steps": [
                                {
                                    "kind": "tool",
                                    "label": "查询当前用户待配送订单",
                                    "status": "completed",
                                }
                            ],
                        },
                    )
                    await _finish_checkpoint(checkpoint_store, context, plan.intent)
                return
            if _requests_multiple_logistics(trigger_text):
                matching_orders = await tools.list_orders(context, trigger_text)
                matching_order_nos = (
                    order_nos_from_result(matching_orders.data)
                    if matching_orders.status == "succeeded"
                    else []
                )
                result = await tools.shipments_for_orders(context, matching_order_nos)
                if result.status == "succeeded":
                    result.data["presentation"] = "logistics_cards"
                    result.data["matched_order_ids"] = matching_order_nos
            else:
                ambiguous_order_nos = (
                    []
                    if unscoped_logistics_order_no is not None
                    else await _ambiguous_recent_order_choices(
                        trigger_text,
                        context=context,
                        tools=tools,
                    )
                )
                if ambiguous_order_nos:
                    await _complete_order_choice(
                        session,
                        context,
                        checkpoint_store,
                        plan,
                        ambiguous_order_nos,
                    )
                    return
                order_no = unscoped_logistics_order_no or await _read_order_no(
                    trigger_text,
                    context=context,
                    builder=builder,
                    tools=tools,
                )
                result = await tools.shipments(context, order_no)
                if result.status == "succeeded":
                    result.data["presentation"] = "logistics_cards"
                if result.status == "succeeded" and _asks_order_logistics_status_difference(
                    trigger_text
                ):
                    order_detail = await tools.order_detail(context, order_no)
                    if order_detail.status == "succeeded":
                        result.data["order_status_detail"] = order_detail.data.get("status", {})
                if (
                    result.status == "succeeded"
                    and _payment_method_explanation({"payment": {}}, trigger_text) is not None
                ):
                    payment_order_detail = await tools.order_detail(context, order_no)
                    if payment_order_detail.status == "succeeded":
                        result.data["order_payment_detail"] = payment_order_detail.data
                if result.status == "succeeded" and _requests_logistics_and_refund_precheck(
                    trigger_text
                ):
                    eligibility = await tools.refund_precheck(context, order_no)
                    if eligibility.status == "succeeded":
                        result.data["combined_refund_precheck"] = eligibility.data
        elif plan.intent == "refund_precheck":
            ambiguous_order_nos = await _ambiguous_recent_order_choices(
                trigger_text,
                context=context,
                tools=tools,
            )
            if ambiguous_order_nos:
                await _complete_order_choice(
                    session,
                    context,
                    checkpoint_store,
                    plan,
                    ambiguous_order_nos,
                )
                return
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
            requested_refund_amounts = _requested_minor_unit_amounts(trigger_text)
            if (
                result.status == "succeeded"
                and explicit_refund_no is None
                and requested_refund_amounts
            ):
                refund_items = result.data.get("items")
                matched_refunds = [
                    item
                    for item in (refund_items if isinstance(refund_items, list) else [])
                    if isinstance(item, Mapping)
                    and isinstance(item.get("requested_amount"), Mapping)
                    and int(item["requested_amount"].get("minor_units") or -1)
                    in requested_refund_amounts
                ]
                result.data["items"] = matched_refunds
                result.data["requested_refund_amounts"] = sorted(requested_refund_amounts)
        else:
            explicit_order_no = _resource_no(trigger_text, "ord")
            refund_order_no: str | None = explicit_order_no
            requested_order_amounts = _requested_minor_unit_amounts(trigger_text)
            if refund_order_no is None and requested_order_amounts:
                amount_orders = await tools.list_orders(context)
                refund_order_no = _order_no_by_explicit_amount(
                    trigger_text,
                    amount_orders.data.get("items"),
                )
            force_order_selection = _disclaims_specific_order(trigger_text) or (
                bool(requested_order_amounts) and refund_order_no is None
            )
            if (
                refund_order_no is None
                and not force_order_selection
                and context.context_refs.get("order") is not None
            ):
                refund_order_no = (
                    await builder.require_active_context(context, "order")
                ).resource_no
            if refund_order_no is None and not force_order_selection:
                recent_order_nos = await recent_agent_order_nos(
                    session,
                    context.conversation,
                    before_sequence=context.trigger.sequence_no,
                )
                refund_order_no = referenced_order_no(trigger_text, recent_order_nos)
                if refund_order_no is None and len(recent_order_nos) == 1:
                    refund_order_no = recent_order_nos[0]
            if refund_order_no is None:
                result = await tools.list_orders(context, trigger_text)
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
                if (
                    result.status == "succeeded"
                    and len(candidate_nos) == 1
                    and not force_order_selection
                ):
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
                if result.status != "succeeded" and result.error_code in {
                    "ORDER_NOT_REFUNDABLE",
                    "REFUND_ITEM_CAPACITY_CHANGED",
                    "REFUND_NOT_ELIGIBLE",
                }:
                    unavailable_code = result.error_code
                    # Fetch the recent visible order set without feeding the phrase
                    # “可申请售后” into the natural-language state filter. That
                    # phrase means eligibility here, not “only orders already in an
                    # after-sale state”. The server projection below is the source
                    # of truth for whether a fresh application entry is available.
                    alternatives = await tools.list_orders(context)
                    if alternatives.status == "succeeded":
                        visible_items = alternatives.data.get("items")
                        alternatives.data["items"] = (
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
                        alternatives.data["selection_required"] = "refund"
                        alternatives.data["refund_context_error"] = unavailable_code
                        alternatives.data["presentation"] = "order_cards"
                        result = alternatives
                if result.status == "succeeded":
                    if result.data.get("selection_required") == "refund":
                        pass
                    else:
                        await _attach_waiting_approval_trace(
                            session,
                            context,
                            intent="refund_eligibility",
                            data=result.data,
                            tool_records=tools.execution_records,
                            tool_code="after_sale.build_refund_draft",
                        )
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
        if tools.execution_records:
            result.data["_audit_tool_calls"] = list(tools.execution_records)
        answer, trace = await _grounded_answer(
            context,
            gateway,
            plan,
            result.data,
            stream_callback=stream_callback,
        )
        trace["planning_source"] = single_plan_source
        recorded_invocation = trace.get("model_invocation")
        if (
            not isinstance(recorded_invocation, Mapping)
            or recorded_invocation.get("status") == "not_invoked"
        ):
            trace["model_invocation"] = planning_model_trace
        trace["planning_confidence"] = plan.confidence
        trace["goal_ledger"] = [
            {
                "goal_key": goal.goal_key,
                "description": goal.description,
                "assigned_task_key": goal.assigned_task_key,
            }
            for goal in supervisor_plan.goal_ledger
        ]
        trace["coverage_complete"] = supervisor_plan.coverage_complete
        if blocked_other_user_clause:
            answer = (
                "我不能查看其他用户的订单或收货地址。你明确允许的本人查询已继续完成。\n\n" + answer
            )
        order_numbers = order_nos_from_result(result.data)
        order_cards = await build_order_cards(
            session,
            context.user,
            context.conversation,
            order_numbers,
            limit=min(8, max(5, len(order_numbers))),
        )
        product_cards = (
            await build_product_cards(
                session,
                context.conversation,
                product_nos_from_result(result.data),
                sku_nos_by_product=_result_preferred_sku_nos(
                    result.data,
                    str(result.data.get("effective_catalog_query") or trigger_text),
                ),
            )
            if plan.intent
            in {
                "product_search",
                "personalized_recommendation",
                "product_compare",
                "favorites_lookup",
                "favorite_update",
                "cart_add",
            }
            else []
        )
        rich_content: dict[str, object] = {}
        if order_cards:
            rich_content["order_cards"] = order_cards
        if product_cards:
            rich_content["product_cards"] = product_cards
        cart_card_intents = {"cart_lookup", "cart_add", "cart_update", "cart_remove"}
        if (
            plan.intent in cart_card_intents
            and not isinstance(result.data.get("cart_hypothetical"), Mapping)
            and not isinstance(result.data.get("cart_add_unavailable_variant"), Mapping)
        ):
            rich_content["cart_card"] = _cart_card(result.data)
        if plan.intent == "address_lookup":
            rich_content["address_cards"] = _address_cards(result.data)
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
            data={"_audit_tool_calls": list(tools.execution_records)},
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
            data={"_audit_tool_calls": list(tools.execution_records)},
            error_code=result.error_code,
            degraded_reason="tool_denied",
        )
    await _finish_checkpoint(checkpoint_store, context, plan.intent)


async def _execute_exclusive_supervisor_plan(
    session: AsyncSession,
    context: TrustedExclusiveAgentContext,
    plan: ExclusiveSupervisorPlan,
    *,
    settings: Settings,
    security: SecurityService,
    checkpoint_store: AgentCheckpointStore,
    context_window: ContextWindow,
    plan_source: str,
    planning_model_trace: Mapping[str, object],
    model_gateway: ExclusiveModelGateway,
    stream_callback: AgentStreamCallback | None,
) -> bool:
    """Execute a model-created compound read/preview plan without a fixed graph."""

    supported = {
        "product_search",
        "personalized_recommendation",
        "policy_qa",
        "order_lookup",
        "cart_lookup",
        "cart_add",
        "cart_update",
        "cart_remove",
        "cart_clear",
        "checkout_preview",
        "address_lookup",
        "wallet_lookup",
        "favorites_lookup",
        "memory_lookup",
        "logistics_lookup",
        "refund_progress",
    }
    tasks = tuple(task for task in plan.tasks if task.intent in supported)
    if len(tasks) < 2 or len(tasks) != len(plan.tasks):
        return False

    scope = TrustedDelegationScope(
        user_no=context.user.user_no,
        conversation_no=context.conversation.conversation_no,
    )
    deadline = time.monotonic() + 8.0
    planned_tool_calls = sum(
        2
        if task.intent in {"logistics_lookup", "cart_update", "cart_remove", "address_lookup"}
        else 1
        for task in tasks
    )
    parent_budget = DelegationBudget(
        deadline_monotonic=deadline,
        token_limit=max(4_000, len(tasks) * 1_000),
        tool_call_limit=planned_tool_calls,
        model_call_limit=0,
    )
    intent_by_packet: dict[str, tuple[str, str]] = {}
    packets: list[DelegationPacket] = []
    specialists: dict[str, Any] = {}
    session_lock = asyncio.Lock()
    tool_gateway = ExclusiveToolGateway(session, settings, security)

    def task_binding(intent: str) -> tuple[str, str]:
        return {
            "product_search": ("catalog", "catalog.search_products"),
            "personalized_recommendation": ("catalog", "catalog.search_products"),
            "policy_qa": ("policy", "rag.policy.search"),
            "order_lookup": ("order", "order.list_user_orders"),
            "cart_lookup": ("cart", "cart.get_mine"),
            "cart_add": ("cart", "cart.add_item"),
            "cart_update": ("cart", "cart.update_quantity"),
            "cart_remove": ("cart", "cart.remove_item"),
            "cart_clear": ("cart", "cart.get_mine"),
            "checkout_preview": ("cart", "checkout.create_session"),
            "address_lookup": ("account", "address.list_mine"),
            "wallet_lookup": ("account", "account.wallet.get_mine"),
            "favorites_lookup": ("account", "account.favorites.list_mine"),
            "memory_lookup": ("account", "memory.list_mine"),
            "logistics_lookup": ("logistics", "logistics.get_user_order_shipments"),
            "refund_progress": ("after_sales", "after_sale.list_user_refunds"),
        }[intent]

    def task_tools(intent: str) -> frozenset[str]:
        if intent == "logistics_lookup":
            return frozenset({"order.list_user_orders", "logistics.get_user_order_shipments"})
        if intent == "cart_update":
            return frozenset({"cart.get_mine", "cart.update_quantity"})
        if intent == "cart_remove":
            return frozenset({"cart.get_mine", "cart.remove_item"})
        if intent == "cart_add":
            return frozenset({"catalog.compare_products", "cart.add_item"})
        if intent == "address_lookup":
            return frozenset({"address.list_mine", "account.profile.get_mine"})
        return frozenset({task_binding(intent)[1]})

    async def invoke(packet: DelegationPacket, budget: DelegationBudget) -> SpecialistResult:
        budget.validate()
        intent, objective = intent_by_packet[packet.delegation_no]
        async with session_lock:
            if intent in {"product_search", "personalized_recommendation"}:
                result = await tool_gateway.search_products(
                    context,
                    objective,
                    fallback_query=objective,
                )
            elif intent == "policy_qa":

                async def search_policy() -> dict[str, object]:
                    policy_result = await _platform_policy(
                        session, context, query_override=objective
                    )
                    await _attach_platform_knowledge(
                        session,
                        checkpoint_store,
                        context,
                        "policy_qa",
                        policy_result.data,
                        query_override=objective,
                    )
                    return policy_result.data

                result = await tool_gateway.execute(
                    context,
                    "rag.policy.search",
                    {"query": objective},
                    search_policy,
                )
            elif intent == "order_lookup":
                result = await tool_gateway.list_orders(context, objective)
            elif intent in {"cart_lookup", "cart_clear"}:
                result = await tool_gateway.get_cart(context)
            elif intent == "checkout_preview":
                result = await tool_gateway.create_cart_checkout_preview(context)
            elif intent == "cart_add":
                reference_index = product_card_reference_index(objective)
                recent_cards = await recent_agent_product_cards(
                    session,
                    context.conversation,
                    before_sequence=context.trigger.sequence_no,
                    minimum_count=max(1, (reference_index or 0) + 1),
                )
                selected_card = referenced_product_card(objective, recent_cards)
                if selected_card is None and len(recent_cards) == 1:
                    selected_card = recent_cards[0]
                sku_no = selected_card.get("sku_id") if selected_card is not None else None
                requested_variant: Mapping[str, Any] | None = None
                selected_product_no = (
                    selected_card.get("product_id") if selected_card is not None else None
                )
                if isinstance(selected_product_no, str):
                    variant_result = await tool_gateway.compare_products(
                        context, [selected_product_no]
                    )
                    if variant_result.status == "succeeded":
                        requested_variant = _requested_product_sku(variant_result.data, objective)
                        if requested_variant is not None:
                            variant_sku_no = requested_variant.get("sku_id")
                            if isinstance(variant_sku_no, str):
                                sku_no = variant_sku_no
                            if int(requested_variant.get("available_stock") or 0) <= 0:
                                result = variant_result
                                result.data["cart_add_unavailable_variant"] = {
                                    "product_name": (selected_card or {}).get("product_name"),
                                    "sku_name": requested_variant.get("sku_name"),
                                }
                                result.data["catalog_focus"] = "sku_availability"
                                result.data["presentation"] = "product_cards"
                                sku_no = None
                if not isinstance(sku_no, str):
                    if requested_variant is None:
                        result = await tool_gateway.get_cart(context)
                        if result.status == "succeeded":
                            result.data["cart_action_clarification"] = True
                else:
                    quantity = _requested_cart_add_quantity(objective)
                    result = await tool_gateway.add_cart_item(context, sku_no, quantity)
                    if result.status == "succeeded":
                        result.data["added_product_name"] = (selected_card or {}).get(
                            "product_name"
                        )
                        result.data["added_sku_name"] = (
                            requested_variant.get("sku_name")
                            if requested_variant is not None
                            else (selected_card or {}).get("sku_name")
                        )
                        result.data["added_quantity"] = quantity
            elif intent in {"cart_update", "cart_remove"}:
                cart_snapshot = await tool_gateway.get_cart(context)
                cart_selection_text = objective
                recent_add_label: str | None = None
                if _references_recent_cart_add(objective):
                    recent_add_label = await _recent_successful_cart_add_label(
                        session,
                        context.conversation,
                        before_sequence=context.trigger.sequence_no,
                    )
                    if recent_add_label is not None:
                        cart_selection_text = f"{objective} {recent_add_label}"
                selected_item = None
                if cart_snapshot.status == "succeeded" and (
                    not _references_recent_cart_add(objective) or recent_add_label is not None
                ):
                    selected_item = _select_cart_item(cart_snapshot.data, cart_selection_text)
                cart_version = cart_snapshot.data.get("version")
                item_no = selected_item.get("cart_item_id") if selected_item else None
                if (
                    selected_item is None
                    or not isinstance(item_no, str)
                    or not isinstance(cart_version, int)
                ):
                    result = cart_snapshot
                    if result.status == "succeeded":
                        result.data["cart_action_clarification"] = True
                elif intent == "cart_update":
                    quantity = _requested_cart_add_quantity(objective)
                    result = await tool_gateway.update_cart_quantity(
                        context, item_no, quantity, cart_version
                    )
                    if result.status == "succeeded":
                        result.data["updated_product_name"] = selected_item.get("product_name")
                        result.data["updated_sku_name"] = selected_item.get("sku_name")
                        result.data["updated_quantity"] = quantity
                else:
                    result = await tool_gateway.remove_cart_item(context, item_no, cart_version)
                    if result.status == "succeeded":
                        result.data["removed_product_name"] = selected_item.get("product_name")
                        result.data["removed_sku_name"] = selected_item.get("sku_name")
            elif intent == "address_lookup":
                wants_profile = _requests_profile_lookup(objective)
                wants_addresses = any(
                    marker in re.sub(r"\s+", "", objective).casefold()
                    for marker in ("收货地址", "地址簿", "默认地址", "我的地址")
                )
                result = (
                    await tool_gateway.get_profile(context)
                    if wants_profile
                    else await tool_gateway.list_addresses(context)
                )
                if wants_profile and wants_addresses and result.status == "succeeded":
                    address_result = await tool_gateway.list_addresses(context)
                    if address_result.status == "succeeded":
                        result.data.update(address_result.data)
            elif intent == "wallet_lookup":
                result = await tool_gateway.get_wallet(context)
                if result.status == "succeeded":
                    result.data["transaction_limit"] = _requested_wallet_transaction_limit(
                        objective
                    )
            elif intent == "favorites_lookup":
                result = await tool_gateway.list_favorites(context)
            elif intent == "memory_lookup":

                async def recall_memories() -> dict[str, object]:
                    recall = await AgentMemoryRuntime(
                        session,
                        checkpoint_store.session,
                        security,
                        embedding_provider(settings),
                        settings.memory_min_vector_similarity,
                    ).recall_exclusive(
                        context.user,
                        query=objective or "购物偏好",
                        limit=5,
                    )
                    return {
                        "recalled_memories": [
                            {
                                "memory_id": item.memory_no,
                                "memory_type": item.memory_type,
                                "memory_key": item.memory_key,
                                "value": item.value,
                                "relevance": round(item.relevance, 4),
                                "expires_at": item.expires_at.isoformat(),
                            }
                            for item in recall.items
                        ],
                        "memory": {
                            "scope": "exclusive",
                            "authorized": recall.authorized,
                            "used_count": len(recall.items),
                            "degraded": recall.degraded,
                        },
                    }

                result = await tool_gateway.execute(
                    context,
                    "memory.list_mine",
                    {},
                    recall_memories,
                )
            elif intent == "logistics_lookup":
                # The logistics specialist first resolves an order inside the
                # current authenticated user's scope, then reads that order's
                # packages.  It cannot accept a user-supplied owner or store id.
                reference_index = order_reference_index(objective)
                order_nos: list[str] = []
                if reference_index is not None:
                    visible_order_nos = await recent_agent_order_nos(
                        session,
                        context.conversation,
                        before_sequence=context.trigger.sequence_no,
                        minimum_count=reference_index + 1,
                    )
                    selected_order_no = referenced_order_no(objective, visible_order_nos)
                    if selected_order_no is not None:
                        order_nos = [selected_order_no]
                orders = await tool_gateway.list_orders(context, objective)
                if not order_nos:
                    listed_order_nos = (
                        order_nos_from_result(orders.data)
                        if orders.status == "succeeded"
                        else []
                    )
                    selected_order_no = referenced_order_no(objective, listed_order_nos)
                    order_nos = (
                        [selected_order_no]
                        if selected_order_no is not None
                        else listed_order_nos
                    )
                if not order_nos:
                    result = orders
                else:
                    result = await tool_gateway.shipments_for_orders(context, order_nos)
                    if result.status == "succeeded":
                        result.data["matched_order_ids"] = order_nos
            else:
                result = await tool_gateway.list_refunds(context)
        return SpecialistResult(
            specialist_code=packet.specialist_code,
            status=(
                "succeeded"
                if result.status == "succeeded"
                else "denied"
                if result.status == "denied"
                else "unknown"
                if result.status == "unknown"
                else "failed"
            ),
            safe_data=result.data,
            tokens_used=0,
            tool_calls=len(task_tools(intent)),
            model_calls=0,
            scope=scope,
            error_code=result.error_code,
        )

    for task in tasks:
        specialist_code, _tool_code = task_binding(task.intent)
        packet = DelegationPacket(
            delegation_no=new_prefixed_ulid("dlg_"),
            parent_run_no=context.run.run_no,
            subtask_key=task.subtask_key,
            specialist_code=specialist_code,
            specialist_version="v1",
            objective=task.objective,
            depth=1,
            trusted_scope=scope,
            resource_refs=(),
            user_constraints=(),
            allowed_tools=task_tools(task.intent),
            budget=parent_budget.child(
                token_limit=1_000,
                tool_call_limit=len(task_tools(task.intent)),
                model_call_limit=0,
            ),
            ancestor_agents=("user_exclusive_support_supervisor",),
        )
        packets.append(packet)
        intent_by_packet[packet.delegation_no] = (task.intent, task.objective)
        specialists[specialist_code] = invoke

    has_direct_write = any(
        task.intent in {"cart_add", "cart_update", "cart_remove", "checkout_preview"}
        for task in tasks
    )
    if has_direct_write:
        # Reversible writes must never fan out concurrently. The Supervisor runs
        # each scoped specialist in plan order so later reads observe the write.
        ledger = SessionDelegationLedger(session, session_lock)
        reduced = {}
        traces: list[DelegationTrace] = []
        dependencies: tuple[str, ...] = ()
        for packet in packets:
            started = time.monotonic()
            await ledger.start(packet, dependency_nos=dependencies)
            specialist_result = await invoke(packet, packet.budget)
            await ledger.put(packet, specialist_result, dependency_nos=dependencies)
            if specialist_result.status in {"succeeded", "partial", "reused"}:
                reduced[packet.delegation_no] = {
                    "specialist": specialist_result.specialist_code,
                    "data": dict(specialist_result.safe_data),
                }
            span_source = f"{packet.parent_run_no}:{packet.delegation_no}".encode()
            traces.append(
                DelegationTrace(
                    delegation_no=packet.delegation_no,
                    parent_run_no=packet.parent_run_no,
                    specialist_code=packet.specialist_code,
                    specialist_version=packet.specialist_version,
                    fingerprint=packet.fingerprint,
                    depth=packet.depth,
                    status=specialist_result.status,
                    elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
                    tokens_used=specialist_result.tokens_used,
                    tool_calls=specialist_result.tool_calls,
                    model_calls=specialist_result.model_calls,
                    span_id=hashlib.sha256(span_source).hexdigest()[:16],
                    dependency_nos=dependencies,
                    error_code=specialist_result.error_code,
                )
            )
            dependencies = (packet.delegation_no,)
    else:
        orchestrator = MultiAgentOrchestrator(
            specialists,
            ledger=SessionDelegationLedger(session, session_lock),
            max_parallel=min(4, len(packets)),
        )
        reduced, traces = await orchestrator.execute(
            DelegationPlan(tuple(packets)),
            parent_tools=context.allowed_tools,
            parent_scope=scope,
            parent_resource_refs=frozenset(),
            budget=parent_budget,
        )
    results: dict[str, dict[str, object]] = {}
    task_results: list[tuple[str, str, dict[str, object]]] = []
    for packet in packets:
        value = reduced.get(packet.delegation_no)
        if not isinstance(value, Mapping):
            continue
        data = value.get("data")
        if isinstance(data, Mapping):
            intent, objective = intent_by_packet[packet.delegation_no]
            task_data = dict(data)
            task_results.append((intent, objective, task_data))
            results.setdefault(intent, task_data)

    if not results:
        return False
    rich_content: dict[str, object] = {}
    combined_detail_cards: list[dict[str, object]] = []
    cart_data = (
        results.get("cart_add")
        or results.get("cart_update")
        or results.get("cart_remove")
        or results.get("cart_clear")
        or results.get("cart_lookup")
    )
    if cart_data is not None:
        cart_hypothetical = (
            _cart_hypothetical_projection(context.trigger.text_content or "", cart_data)
            if "cart_lookup" in results
            else None
        )
        if cart_hypothetical is not None:
            cart_data["cart_hypothetical"] = cart_hypothetical
            combined_detail_cards.extend(
                _exclusive_detail_cards(ExclusiveAgentPlan("cart_lookup"), cart_data)
            )
        else:
            rich_content["cart_card"] = _cart_card(cart_data)
    address_data = results.get("address_lookup")
    if address_data is not None:
        rich_content["address_cards"] = _address_cards(address_data)
    order_data = results.get("order_lookup")
    if order_data is not None:
        order_cards = await build_order_cards(
            session,
            context.user,
            context.conversation,
            order_nos_from_result(order_data),
            limit=min(8, max(5, len(order_nos_from_result(order_data)))),
        )
        if order_cards:
            rich_content["order_cards"] = order_cards
    if address_data is not None:
        profile_cards = _exclusive_detail_cards(ExclusiveAgentPlan("address_lookup"), address_data)
        if profile_cards:
            combined_detail_cards.extend(profile_cards)
    checkout_data = results.get("checkout_preview")
    if checkout_data is not None:
        checkout_cards = _exclusive_detail_cards(
            ExclusiveAgentPlan("checkout_preview"), checkout_data
        )
        if checkout_cards:
            combined_detail_cards.extend(checkout_cards)
    logistics_data = results.get("logistics_lookup")
    if logistics_data is not None:
        selected_order_no = logistics_data.get("selected_order_id")
        if isinstance(selected_order_no, str) and "order_cards" not in rich_content:
            logistics_order_cards = await build_order_cards(
                session,
                context.user,
                context.conversation,
                [selected_order_no],
            )
            if logistics_order_cards:
                rich_content["order_cards"] = logistics_order_cards
        logistics_cards = _exclusive_detail_cards(
            ExclusiveAgentPlan("logistics_lookup"), logistics_data
        )
        if logistics_cards:
            combined_detail_cards.extend(logistics_cards)
    refund_data = results.get("refund_progress")
    if refund_data is not None:
        refund_cards = _exclusive_detail_cards(ExclusiveAgentPlan("refund_progress"), refund_data)
        if refund_cards:
            combined_detail_cards.extend(refund_cards)
    policy_data = results.get("policy_qa")
    if policy_data is not None:
        policy_cards = _exclusive_detail_cards(ExclusiveAgentPlan("policy_qa"), policy_data)
        if policy_cards:
            combined_detail_cards.extend(policy_cards)
    for account_intent in ("wallet_lookup", "favorites_lookup", "memory_lookup"):
        account_data = results.get(account_intent)
        if account_data is None:
            continue
        account_cards = _exclusive_detail_cards(ExclusiveAgentPlan(account_intent), account_data)
        if account_cards:
            combined_detail_cards.extend(account_cards)
    if combined_detail_cards:
        rich_content["detail_cards"] = combined_detail_cards
    catalog_task_results = [
        item
        for item in task_results
        if item[0] in {"product_search", "personalized_recommendation"}
    ]
    if len(catalog_task_results) >= 2:
        product_groups: list[dict[str, object]] = []
        flattened_cards: list[dict[str, object]] = []
        seen_product_ids: set[str] = set()
        for _intent, objective, task_data in catalog_task_results:
            cards = await build_product_cards(
                session,
                context.conversation,
                product_nos_from_result(task_data),
                sku_nos_by_product=_preferred_sku_nos(task_data, objective),
            )
            product_groups.append(
                {
                    "title": re.sub(r"^(?:推荐|搜索|查找)", "", objective).strip()
                    or "商品推荐",
                    "objective": objective,
                    "cards": cards,
                    "empty": not cards,
                }
            )
            for card in cards:
                product_id = str(card.get("product_id") or "")
                if product_id and product_id not in seen_product_ids:
                    seen_product_ids.add(product_id)
                    flattened_cards.append(card)
        rich_content["product_card_groups"] = product_groups
        # Keep one ordered flattened collection for later ordinal follow-ups;
        # the UI renders the grouped collection and suppresses this duplicate.
        if flattened_cards:
            rich_content["product_cards"] = flattened_cards
    product_data = results.get("product_search") or results.get("personalized_recommendation")
    if product_data is not None and len(catalog_task_results) < 2:
        product_cards = await build_product_cards(
            session,
            context.conversation,
            product_nos_from_result(product_data),
            sku_nos_by_product=_preferred_sku_nos(product_data, context.trigger.text_content or ""),
        )
        if product_cards:
            rich_content["product_cards"] = product_cards
    favorite_data = results.get("favorites_lookup")
    if favorite_data is not None:
        favorite_product_cards = await build_product_cards(
            session,
            context.conversation,
            product_nos_from_result(favorite_data),
        )
        if favorite_product_cards:
            rich_content["product_cards"] = favorite_product_cards

    audit_data: dict[str, object] = {
        "compound_results": results,
        "compound_task_results": [
            {"intent": intent, "objective": objective, "data": data}
            for intent, objective, data in task_results
        ],
        "_audit_tool_calls": list(tool_gateway.execution_records),
        "conversation_window": context_window.model_projection(context.context_refs),
        "model_invocation": dict(planning_model_trace),
    }
    if isinstance(policy_data, Mapping):
        for key in (
            "rag",
            "knowledge_sources",
            "policy_query",
            "policy_retrieval_query",
        ):
            if key in policy_data:
                audit_data[key] = policy_data[key]
    memory_trace_data = results.get("memory_lookup")
    if isinstance(memory_trace_data, Mapping):
        for key in ("memory", "recalled_memories"):
            if key in memory_trace_data:
                audit_data[key] = memory_trace_data[key]
    trace = public_trace(
        run_id=context.run.run_no,
        agent="专属客服 Supervisor Agent (用户服务总管)",
        model=context.agent_version.model_profile,
        question=agent_trace_question(context.trigger),
        intent="compound_request",
        data=audit_data,
        steps=[
            {
                "kind": "delegation",
                "label": f"委派给 {item.specialist_code} Agent",
                "status": item.status,
                "delegation_id": item.delegation_no,
                "specialist": item.specialist_code,
                "objective": intent_by_packet[item.delegation_no][1],
                "tool_code": task_binding(intent_by_packet[item.delegation_no][0])[1],
                "allowed_tools": sorted(task_tools(intent_by_packet[item.delegation_no][0])),
                "depth": item.depth,
                "elapsed_ms": item.elapsed_ms,
                "tool_calls": item.tool_calls,
                "tokens_used": item.tokens_used,
                "error_code": item.error_code,
            }
            for item in traces
        ],
        source_ids=tuple(
            f"tool:{tool_code}" for task in tasks for tool_code in sorted(task_tools(task.intent))
        ),
        tool_code="multi_agent",
        extra={
            "planning_confidence": plan.confidence,
            "planning_source": plan_source,
            "goal_ledger": [
                {
                    "goal_key": goal.goal_key,
                    "description": goal.description,
                    "assigned_task_key": goal.assigned_task_key,
                }
                for goal in plan.goal_ledger
            ],
            "coverage_complete": plan.coverage_complete,
            "execution_strategy": (
                "serial_write_then_read"
                if any(task.intent in {"cart_update", "cart_remove"} for task in tasks)
                else "parallel_read_only"
            ),
            "subtasks": [
                {
                    "key": task.subtask_key,
                    "intent": task.intent,
                    "objective": task.objective,
                    "specialist": task_binding(task.intent)[0],
                    "allowed_tools": sorted(task_tools(task.intent)),
                }
                for task in tasks
            ],
            "delegation_count": len(traces),
        },
    )
    trace["answer_mode"] = "structured_ui"
    trace["grounding_verified"] = True
    trace["confidence"] = "high"

    if "cart_clear" in results:
        total_quantity = results["cart_clear"].get("cart_total_quantity")
        if not isinstance(total_quantity, int) or total_quantity <= 0:
            await _complete(
                session,
                context,
                "你的购物车已经是空的，其他查询结果已整理在下方。",
                data=audit_data,
                execution_trace=trace,
                extra_content=rich_content or None,
            )
            await _finish_checkpoint(checkpoint_store, context, "compound_request")
            return True
        await AgentApprovalService(session, settings, security).build_cart_clear_approval(
            context,
            results["cart_clear"],
            extra_content={**rich_content, "execution_trace": trace},
        )
        await checkpoint_store.write(
            context.run.run_no,
            "waiting_confirmation",
            _checkpoint_state(context, intent="cart_clear"),
            status="waiting",
        )
        return True

    state_counts = (
        order_data.get("requested_state_counts") if isinstance(order_data, Mapping) else None
    )
    missing_states = (
        [str(label) for label, count in state_counts.items() if int(count or 0) == 0]
        if isinstance(state_counts, Mapping)
        else []
    )
    completion_text = f"我把你的请求拆成了 {len(tasks)} 项，并已分别查询。结果都整理在下方卡片中。"
    if isinstance(cart_data, Mapping) and isinstance(cart_data.get("cart_hypothetical"), Mapping):
        completion_text = _render(
            ExclusiveAgentPlan("cart_lookup"),
            cart_data,
            context.trigger.text_content or "",
        )
    if address_data is not None and _asks_address_postcode(context.trigger.text_content or ""):
        completion_text += " " + _render(
            ExclusiveAgentPlan("address_lookup"),
            address_data,
            context.trigger.text_content or "",
        )
    if requests_other_user_data(context.trigger.text_content or ""):
        completion_text = (
            "我不能查看其他用户的订单或收货地址。"
            "你明确允许的本人查询已继续完成。\n\n" + completion_text
        )
    affordability_text = _wallet_cart_affordability_text(
        results, context.trigger.text_content or ""
    )
    if affordability_text:
        completion_text = affordability_text
    cart_mutation_data = (
        results.get("cart_add") or results.get("cart_update") or results.get("cart_remove")
    )
    if isinstance(cart_mutation_data, Mapping):
        if cart_mutation_data.get("cart_action_clarification") is True:
            completion_text = (
                "我无法唯一确定要修改的购物车商品，因此没有改动。"
                "其余请求已经完成。请补充商品名、款式或序号。"
            )
        else:
            cart_mutation_intent: ExclusiveIntent = (
                "cart_add"
                if "cart_add" in results
                else "cart_update"
                if "cart_update" in results
                else "cart_remove"
            )
            completion_text = (
                _render(
                    ExclusiveAgentPlan(cart_mutation_intent),
                    cart_mutation_data,
                )
                + f" 另外 {len(tasks) - 1} 项查询结果也已放在下方卡片中。"
            )
    if requests_direct_transaction_action(context.trigger.text_content or ""):
        completion_text = (
            "我不能代你付款、取消订单或确认收货，因此没有执行这项操作，"
            "其余只读请求已完成，你可以通过下方购物车或订单卡片自行核对和操作。\n\n"
            + completion_text
        )
    recharge_text = re.sub(r"\s+", "", context.trigger.text_content or "").casefold()
    recharge_history_question = "充值" in recharge_text and any(
        marker in recharge_text
        for marker in ("哪笔", "每笔", "流水", "变动", "记录", "历史", "明细", "消费")
    )
    if "充值" in recharge_text and not recharge_history_question:
        completion_text = (
            "我不能在聊天中代你发起充值，本次没有增加余额。"
            "当前商城只提供模拟充值，你可以从下方余额卡片进入账户页，"
            "选择微信或支付宝模拟渠道后自行确认。\n\n" + completion_text
        )
    if policy_data is not None:
        policy_objective = next(task.objective for task in tasks if task.intent == "policy_qa")
        policy_text = _render(
            ExclusiveAgentPlan("policy_qa"),
            policy_data,
            policy_objective,
        )
        completion_text = policy_text + "\n\n" + completion_text
        if refund_data is not None:
            completion_text = (
                _render(ExclusiveAgentPlan("refund_progress"), refund_data) + "\n\n" + policy_text
            )
    if missing_states:
        completion_text += f" 当前没有{'、'.join(missing_states)}订单，其余状态结果已展示。"
    if isinstance(state_counts, Mapping) and _requests_order_state_overview(
        context.trigger.text_content or ""
    ):
        overview = "、".join(
            f"{safe_untrusted_excerpt(label, 16)} {int(count or 0)} 笔"
            for label, count in state_counts.items()
        )
        if overview:
            completion_text += f" 订单状态统计: {overview}。"
    if logistics_data is not None:
        logistics_items = logistics_data.get("items")
        if not isinstance(logistics_items, list) or not logistics_items:
            completion_text += " 这笔订单当前还没有可见物流包裹，通常是尚未发货。"
        else:
            logistics_objective = next(
                task.objective for task in tasks if task.intent == "logistics_lookup"
            )
            logistics_text = _render(
                ExclusiveAgentPlan("logistics_lookup"),
                logistics_data,
                logistics_objective,
            )
            completion_text = logistics_text + "\n\n" + completion_text
    memory_data = results.get("memory_lookup")
    if memory_data is not None:
        recalled_memories = memory_data.get("recalled_memories")
        if not isinstance(recalled_memories, list) or not recalled_memories:
            completion_text += " " + _render(ExclusiveAgentPlan("memory_lookup"), memory_data)
    if address_data is not None:
        address_items = address_data.get("items")
        if not isinstance(address_items, list) or not address_items:
            completion_text += " 你的账号目前还没有收货地址。"
        else:
            completion_text += f" 当前账号共有 {len(address_items)} 个收货地址，已全部展示。"
    if _requests_compound_advice(context.trigger.text_content or ""):
        counts = state_counts if isinstance(state_counts, Mapping) else {}
        if int(counts.get("运输中", 0) or 0) > 0:
            completion_text += " 建议先关注运输中订单的最新物流，再检查待发货订单是否按期出库。"
        elif int(counts.get("待发货", 0) or 0) > 0:
            completion_text += " 建议先关注待发货订单的预计发货时间。"
    if isinstance(model_gateway, ProviderExclusiveModelGateway) and _requests_compound_advice(
        context.trigger.text_content or ""
    ):
        stream_gate = AgentStreamGate(stream_callback)
        try:
            grounded = await hard_deadline(
                model_gateway.synthesize(
                    agent_prompt=context.agent_version.system_prompt,
                    user_text=context.trigger.text_content or "",
                    intent="compound_advice",
                    evidence={"subtask_results": results},
                    source_ids=tuple(
                        f"tool:{tool_code}"
                        for task in tasks
                        for tool_code in sorted(task_tools(task.intent))
                    ),
                    stream_callback=stream_gate.publish,
                ),
                budget_seconds=25.0,
            )
            stream_gate.close()
            completion_text = grounded.text
            trace["answer_mode"] = "model_grounded"
            trace["confidence"] = grounded.confidence
            trace["grounding_verified"] = grounded.grounding_verified
            trace["model_invocation"] = _model_invocation_trace(grounded)
            if grounded.analysis_summary and _is_chinese_trace_text(grounded.analysis_summary):
                trace["analysis_summary"] = grounded.analysis_summary
            chinese_analysis_details = [
                item for item in grounded.analysis_details if _is_chinese_trace_text(item)
            ]
            if chinese_analysis_details:
                trace["analysis_details"] = chinese_analysis_details
        except (ModelGatewayError, TimeoutError) as exc:
            stream_gate.close()
            trace["answer_mode"] = "deterministic_fallback"
            trace["degraded_reason"] = model_failure_code(exc, "compound_answer")
    await _complete(
        session,
        context,
        completion_text,
        data=audit_data,
        execution_trace=trace,
        extra_content=rich_content or None,
    )
    await _finish_checkpoint(checkpoint_store, context, "compound_request")
    return True


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
    result.data["_audit_tool_calls"] = list(tools.execution_records)
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


async def _resume_cart_clear_approval(
    session: AsyncSession,
    context: TrustedExclusiveAgentContext,
    approval: AgentToolApproval,
    settings: Settings,
    security: SecurityService,
    checkpoint_store: AgentCheckpointStore,
) -> None:
    service = AgentApprovalService(session, settings, security)
    if approval.approval_status == "rejected":
        await _complete(session, context, "已取消清空购物车，购物车内容没有变化。")
        await _finish_checkpoint(checkpoint_store, context, "cart_clear")
        return
    if approval.approval_status == "expired":
        await _complete(
            session,
            context,
            "清空购物车的确认已过期，没有删除任何商品。需要时请重新告诉我。",
            error_code="AGENT_APPROVAL_EXPIRED",
        )
        await _finish_checkpoint(checkpoint_store, context, "cart_clear")
        return
    tools = ExclusiveToolGateway(session, settings, security)

    async def execute() -> dict[str, object]:
        status, cart_data, error_code = await service.execute_cart_clear(context)
        if status == "succeeded" and cart_data is not None:
            return {"status": status, **cart_data}
        if status == "rejected":
            return {"status": status}
        raise ApplicationError(
            status=409,
            code=error_code or "AGENT_APPROVED_ACTION_FAILED",
            title="Approved action failed",
            detail="购物车已经变化，没有执行清空。",
        )

    result = await tools.execute(
        context,
        "cart.clear.commit",
        {"approval_id": approval.approval_no},
        execute,
        trusted_approval_no=approval.approval_no,
    )
    result.data["_audit_tool_calls"] = list(tools.execution_records)
    if result.status == "succeeded":
        await _complete(
            session,
            context,
            "购物车已经清空。我只删除了当前账号购物车中的商品，没有影响订单或收藏。",
            data=result.data,
            execution_trace=public_trace(
                run_id=context.run.run_no,
                agent="用户购物车与结算 Agent",
                model=context.agent_version.model_profile,
                question=agent_trace_question(context.trigger),
                intent="cart_clear",
                data=result.data,
                steps=[
                    {
                        "kind": "confirmation",
                        "label": "验证用户对清空购物车的明确确认",
                        "status": "completed",
                    },
                    {
                        "kind": "tool",
                        "label": "按确认版本清空当前账号购物车",
                        "tool_code": "cart.clear.commit",
                        "status": "completed",
                    },
                    {
                        "kind": "verification",
                        "label": "回读购物车并确认剩余商品为零",
                        "status": "completed",
                    },
                ],
                source_ids=("tool:cart.clear.commit",),
                tool_code="cart.clear.commit",
                extra={"approval_id": approval.approval_no},
            ),
            extra_content={"cart_card": _cart_card(result.data)},
        )
    else:
        await _complete(
            session,
            context,
            "购物车在确认后发生了变化，本次没有删除商品。请让我重新读取后再确认。",
            error_code=result.error_code,
        )
    await _finish_checkpoint(checkpoint_store, context, "cart_clear")


async def _platform_policy(
    session: AsyncSession,
    context: TrustedExclusiveAgentContext,
    *,
    query_override: str | None = None,
) -> StoreToolResult:
    now = utc_now()
    query = _strip_negated_intent_phrases(
        (
            query_override if query_override is not None else context.trigger.text_content or ""
        ).strip()
    )
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
            "policy_query": query,
            "items": [
                {
                    "content_id": entry.content_no,
                    "title": entry.title,
                    "version": version.document_version,
                    "content": version.safe_content[:1000],
                    "effective_at": version.effective_at,
                }
                for entry, version in rows
            ],
        },
    )


async def _handoff(
    session: AsyncSession,
    context: TrustedExclusiveAgentContext,
    settings: Settings,
    security: SecurityService,
    reason_code: str,
) -> None:
    tools = ExclusiveToolGateway(session, settings, security)
    result = await tools.handoff(context, reason_code)
    result.data["_audit_tool_calls"] = list(tools.execution_records)
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


async def _attach_waiting_approval_trace(
    session: AsyncSession,
    context: TrustedExclusiveAgentContext,
    *,
    intent: str,
    data: Mapping[str, Any],
    tool_records: list[dict[str, object]],
    tool_code: str,
) -> None:
    """Attach real pre-confirmation evidence to the approval message for this Run."""

    message = await session.scalar(
        select(Message)
        .where(
            Message.conversation_id == context.conversation.id,
            Message.ai_run_no == context.run.run_no,
        )
        .order_by(Message.sequence_no.desc())
        .limit(1)
    )
    if message is None:
        return
    trace_data = {**dict(data), "_audit_tool_calls": list(tool_records)}
    trace = public_trace(
        run_id=context.run.run_no,
        agent="专属客服 Supervisor Agent (用户服务总管)",
        model=context.agent_version.model_profile,
        question=agent_trace_question(context.trigger),
        intent=intent,
        data=trace_data,
        steps=[
            {
                "kind": "context",
                "label": "读取当前会话焦点与登录用户范围",
                "status": "completed",
            },
            {
                "kind": "tool",
                "label": "校验业务对象并生成待确认操作",
                "status": "completed",
                "tool_code": tool_code,
            },
            {
                "kind": "checkpoint",
                "label": "暂停执行并等待用户点击确认或拒绝",
                "status": "waiting",
            },
        ],
        source_ids=(f"tool:{tool_code}",),
        tool_code=tool_code,
        extra={
            "planning_source": "agent_runtime",
            "execution_strategy": "preview_then_explicit_confirmation",
            "delegation_count": 0,
        },
    )
    trace["status"] = "waiting_confirmation"
    trace["answer_mode"] = "approval_required"
    trace["grounding_verified"] = True
    payload = dict(message.content_payload or {})
    payload["execution_trace"] = trace
    message.content_payload = payload


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
    conversation = await lock_conversation_for_append(session, context.conversation.id)
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
    await ConversationStateRuntime(session).record_agent_response(
        conversation,
        context.trigger,
        message,
        trace,
    )
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
    if isinstance(data.get("combined_refund_precheck"), Mapping):
        steps.append(
            {
                "kind": "tool",
                "label": "检查同一订单的售后资格",
                "tool_code": "after_sale.check_refund_eligibility",
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
    if plan.intent == "general_chat" and _is_unambiguous_general_chat(
        agent_trigger_text(context.trigger)
    ):
        trace["answer_mode"] = "deterministic_social_reply"
        return fallback, trace
    if data.get("presentation") in {
        "order_card",
        "order_cards",
        "product_cards",
        "product_comparison",
        "cart_card",
        "address_cards",
        "detail_cards",
        "logistics_cards",
    }:
        trace["answer_mode"] = "structured_ui"
        return fallback, trace
    if not isinstance(gateway, ProviderExclusiveModelGateway):
        trace["answer_mode"] = "deterministic_fallback"
        return fallback, trace
    context.run.current_phase = "answering"
    context.run.version += 1
    stream_gate = AgentStreamGate(stream_callback)
    try:
        answer = await hard_deadline(
            gateway.synthesize(
                agent_prompt=context.agent_version.system_prompt,
                user_text=agent_trigger_text(context.trigger),
                intent=plan.intent,
                evidence=data,
                source_ids=source_ids,
                stream_callback=stream_gate.publish,
            ),
            budget_seconds=MODEL_ANSWER_BUDGET_SECONDS,
        )
    except (ModelGatewayError, TimeoutError) as exc:
        stream_gate.close()
        reason = model_failure_code(exc, "answer")
        trace["answer_mode"] = "deterministic_fallback"
        trace["degraded_reason"] = reason
        trace["model_invocation"] = {
            "status": "failed",
            "provider_request_sent": True,
            "model": context.agent_version.model_profile,
            "stage": "answer_or_grounding_verification",
            "error_code": reason,
            "fallback_used": True,
        }
        context.run.degraded_reason = reason
        if stream_callback is not None:
            await stream_callback("answer_replace", fallback)
        return fallback, trace
    stream_gate.close()
    trace["answer_mode"] = "model_grounded"
    trace["confidence"] = answer.confidence
    trace["cited_source_ids"] = list(answer.cited_source_ids)
    trace["thinking_mode"] = "enabled" if answer.thinking_used else "not_reported"
    trace["grounding_verified"] = answer.grounding_verified
    trace["evidence_truncated"] = answer.evidence_truncated
    trace["truncated_evidence_fields"] = list(answer.truncated_evidence_fields)
    trace["model_invocation"] = _model_invocation_trace(answer)
    if answer.analysis_summary and _is_chinese_trace_text(answer.analysis_summary):
        trace["analysis_summary"] = answer.analysis_summary
    chinese_analysis_details = [
        item for item in answer.analysis_details if _is_chinese_trace_text(item)
    ]
    if chinese_analysis_details:
        trace["analysis_details"] = chinese_analysis_details
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
        "cart_add": "cart.add_item",
        "cart_update": "cart.update_quantity",
        "cart_remove": "cart.remove_item",
        "cart_clear": "cart.clear.commit",
        "checkout_preview": "checkout.create_session",
        "address_lookup": "address.list_mine",
        "wallet_lookup": "account.wallet.get_mine",
        "favorites_lookup": "account.favorites.list_mine",
        "favorite_update": "account.favorites.update_mine",
        "review_draft": "order.list_user_orders",
        "memory_lookup": "memory.list_mine",
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
    if re.search(r"(?:¥|￥)?\d+(?:\.\d{1,2})?元", trigger_text):
        amount_orders = await tools.list_orders(context, trigger_text)
        amount_items = (
            amount_orders.data.get("items") if amount_orders.status == "succeeded" else None
        )
        amount_match = _order_no_by_explicit_amount(trigger_text, amount_items)
        if amount_match is not None:
            return amount_match
    reference_index = order_reference_index(trigger_text)
    if reference_index is not None and _has_explicit_order_state_scope(trigger_text):
        scoped_orders = await tools.list_orders(context, trigger_text)
        scoped_order_nos = (
            order_nos_from_result(scoped_orders.data) if scoped_orders.status == "succeeded" else []
        )
        if reference_index < len(scoped_order_nos):
            return scoped_order_nos[reference_index]
    recent_order_nos = await recent_agent_order_nos(
        tools.session,
        context.conversation,
        before_sequence=context.trigger.sequence_no,
        minimum_count=max(
            reference_index + 1 if reference_index is not None else 1,
            2 if _corrects_order_reference(trigger_text) else 1,
        ),
    )
    referenced = referenced_order_no(trigger_text, recent_order_nos)
    if referenced is not None:
        return referenced
    natural_reference = await referenced_recent_order_no(
        tools.session,
        context.conversation,
        before_sequence=context.trigger.sequence_no,
        user_text=trigger_text,
    )
    if natural_reference is not None:
        return natural_reference
    matched_orders = await tools.list_orders(context, trigger_text)
    matched_items = (
        matched_orders.data.get("items") if matched_orders.status == "succeeded" else None
    )
    amount_match = _order_no_by_explicit_amount(trigger_text, matched_items)
    if amount_match is not None:
        return amount_match
    if (
        isinstance(matched_items, list)
        and len(matched_items) == 1
        and isinstance(matched_items[0], Mapping)
    ):
        matched_order_no = matched_items[0].get("order_id")
        if isinstance(matched_order_no, str):
            return matched_order_no
    if _requests_latest_order(trigger_text) or context.context_refs.get("order") is None:
        return await tools.latest_order_no(context)
    return (await builder.require_active_context(context, "order")).resource_no


def _order_no_by_explicit_amount(user_text: str, items: object) -> str | None:
    """Resolve one live user order by an amount explicitly written by the shopper."""

    if not isinstance(items, list):
        return None
    requested = _requested_minor_unit_amounts(user_text)
    if not requested:
        return None
    matched: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        amount_values = item.get("amounts")
        paid = amount_values.get("paid") if isinstance(amount_values, Mapping) else None
        try:
            minor_units = int(paid.get("minor_units") or 0) if isinstance(paid, Mapping) else -1
        except (TypeError, ValueError):
            continue
        order_no = item.get("order_id")
        if minor_units in requested and isinstance(order_no, str):
            matched.append(order_no)
    unique = list(dict.fromkeys(matched))
    return unique[0] if len(unique) == 1 else None


def _requested_minor_unit_amounts(user_text: str) -> set[int]:
    requested: set[int] = set()
    for raw in re.findall(r"(?:¥|￥)?(\d+(?:\.\d{1,2})?)元", user_text):
        try:
            requested.add(int(Decimal(raw) * 100))
        except (InvalidOperation, ValueError):
            continue
    return requested


async def _ambiguous_recent_order_choices(
    trigger_text: str,
    *,
    context: TrustedExclusiveAgentContext,
    tools: ExclusiveToolGateway,
) -> list[str]:
    """Return the latest visible order set when a singular reference is unresolved."""

    if (
        _trigger_payload_value(context.trigger, "order_card", "order_id") is not None
        or _resource_no(trigger_text, "ord") is not None
        or order_reference_index(trigger_text) is not None
        or _requests_latest_order(trigger_text)
        or _has_explicit_order_state_scope(trigger_text)
    ):
        return []
    recent_order_nos = await recent_agent_order_nos(
        tools.session,
        context.conversation,
        before_sequence=context.trigger.sequence_no,
        minimum_count=1,
    )
    if len(recent_order_nos) < 2:
        return []
    natural_reference = await referenced_recent_order_no(
        tools.session,
        context.conversation,
        before_sequence=context.trigger.sequence_no,
        user_text=trigger_text,
    )
    if natural_reference in recent_order_nos:
        return []
    normalized = re.sub(r"\s+", "", trigger_text).casefold()
    if any(marker in normalized for marker in ("物流", "快递", "包裹", "送达", "到哪")):
        # Old visible card sets may contain completed or after-sale orders.  A
        # bare logistics question must only offer orders that can actually have
        # an active delivery journey.
        current_orders = await tools.list_orders(context)
        values = current_orders.data.get("items") if current_orders.status == "succeeded" else None
        relevant_order_nos = [
            str(item["order_id"])
            for item in (values if isinstance(values, list) else [])
            if isinstance(item, Mapping)
            and isinstance(item.get("status"), Mapping)
            and item["status"].get("order") in {"pending_shipment", "shipped"}
            and isinstance(item.get("order_id"), str)
        ]
        if len(relevant_order_nos) >= 2:
            return relevant_order_nos[:5]
    return recent_order_nos[:5]


async def _complete_order_choice(
    session: AsyncSession,
    context: TrustedExclusiveAgentContext,
    checkpoint_store: AgentCheckpointStore,
    plan: ExclusiveAgentPlan,
    order_nos: list[str],
) -> None:
    cards = await build_order_cards(
        session,
        context.user,
        context.conversation,
        order_nos,
        limit=len(order_nos),
    )
    choice_message = (
        "当前只找到 1 笔可能相关的订单，我没有直接代你选定。请回复“第一笔”，"
        "也可以点击订单卡片后继续问。"
        if len(order_nos) == 1
        else _order_choice_prompt(len(order_nos))
    )
    await _complete(
        session,
        context,
        choice_message,
        execution_trace={
            "intent": plan.intent,
            "steps": [
                {
                    "kind": "context",
                    "label": "发现当前有多个可选订单，停止猜测",
                    "status": "completed",
                },
                {
                    "kind": "answer",
                    "label": "请求用户明确选择订单",
                    "status": "completed",
                },
            ],
            "missing_slots": ["order_choice"],
        },
        extra_content={"order_cards": cards},
    )
    await _finish_checkpoint(checkpoint_store, context, plan.intent)


def _order_choice_prompt(count: int) -> str:
    labels = ("第一笔", "第二笔", "第三笔", "第四笔", "第五笔")[:count]
    choices = "、".join(f"“{label}”" for label in labels)
    return (
        "你当前有多笔可能相关的订单，我还不能确定你指哪一笔。"
        f"请回复 {choices} 中的一个，也可以点击对应订单卡片后继续问。"
    )


def _has_explicit_order_state_scope(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    return any(
        marker in compact
        for marker in (
            "待付款",
            "未付款",
            "待发货",
            "未发货",
            "运输中",
            "配送中",
            "待收货",
            "待评价",
            "已完成",
            "售后中",
            "退款中",
            "已取消",
        )
    )


def _appears_compound_request(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    connectors = ("并且", "同时", "另外", "然后", "以及", "和我的", "与我的", "跟我的")
    domain_hits = sum(
        any(marker in compact for marker in markers)
        for markers in (
            ("商品", "推荐", "搜索", "对比"),
            ("订单", "买过", "购买记录"),
            ("购物车", "购物袋"),
            ("收货地址", "地址簿", "默认地址", "我的地址"),
            ("物流", "快递", "包裹"),
            ("退款", "售后", "退货"),
        )
    )
    return any(connector in compact for connector in connectors) and domain_hits >= 2


def _rule_must_guard_intent(intent: ExclusiveIntent) -> bool:
    """Keep deterministic authority only at mutation and handoff boundaries."""

    return intent in {
        "cart_add",
        "cart_update",
        "cart_remove",
        "cart_clear",
        "favorite_update",
        # Preparing a refund draft enters the server-owned confirmation and
        # after-sale state-machine boundary.  A provider must not reinterpret
        # the generic word “草稿” as a review draft and execute the wrong domain.
        "refund_eligibility",
        "human_handoff",
    }


def _must_guard_read_only_order_eligibility(
    user_text: str,
    deterministic_intent: ExclusiveIntent,
) -> bool:
    """Keep an order-action comparison read-only even if the model sees "refund".

    Questions such as ``哪一笔还能申请售后`` ask for a comparison of the
    server-projected ``available_actions`` on several orders.  They do not ask
    us to create a refund draft.  A semantic planner may otherwise over-weight
    the words ``退款/售后`` and turn a read into a confirmation workflow.
    """

    return (
        deterministic_intent == "order_lookup"
        and bool(_requested_order_eligibility_actions(user_text))
        and any(
            marker in re.sub(r"\s+", "", user_text).casefold()
            for marker in ("哪些", "哪几笔", "哪一笔", "哪笔", "分别", "各自")
        )
    )


def _looks_like_contextual_follow_up(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    return len(compact) <= 12 or any(
        marker in compact
        for marker in (
            "刚才",
            "上一个",
            "这个",
            "那个",
            "它",
            "第一",
            "第二",
            "第三",
            "不是",
            "我说的是",
            "继续",
        )
    )


def _selects_existing_product_only(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    has_ordinal = product_card_reference_index(user_text) is not None
    correction = any(
        marker in compact
        for marker in ("不是", "我说的是", "应该是", "改成", "刚才推荐", "回到刚才")
    )
    new_search = any(
        marker in compact
        for marker in ("元以内", "预算", "颜色", "季节", "适合", "帮我找", "重新搜索")
    )
    return has_ordinal and correction and not new_search


def _merge_supervisor_plans(
    provider: ExclusiveSupervisorPlan,
    deterministic: ExclusiveSupervisorPlan,
    *,
    force_deterministic_intent: ExclusiveIntent | None,
    preserve_compound_coverage: bool,
) -> ExclusiveSupervisorPlan:
    """Keep a valid model plan authoritative and add only hard safety operations.

    Read-only keyword coverage is deliberately not merged into a provider plan.
    The deterministic planner takes over only when the provider plan is absent or
    invalid; explicit mutation and handoff intents remain security contracts.
    """

    if not provider.tasks or not provider.coverage_complete:
        return deterministic
    chosen = list(provider.tasks)
    changed = False
    fallback_by_intent = {task.intent: task for task in deterministic.tasks}
    if force_deterministic_intent is not None:
        guarded = fallback_by_intent.get(force_deterministic_intent)
        if guarded is not None and all(task.intent != guarded.intent for task in chosen):
            if len(chosen) == 1 and not preserve_compound_coverage:
                chosen = [guarded]
            else:
                chosen.append(guarded)
            changed = True
    del preserve_compound_coverage
    if not changed:
        return provider
    normalized = tuple(
        ExclusiveSupervisorSubtask(
            subtask_key=f"task_{index}",
            intent=task.intent,
            objective=task.objective,
        )
        for index, task in enumerate(chosen[:4], start=1)
    )
    return ExclusiveSupervisorPlan(normalized, provider.confidence)


async def _recent_agent_intent(
    session: AsyncSession,
    conversation: Conversation,
    *,
    before_sequence: int,
) -> str | None:
    state = await ConversationStateRuntime(session).load(
        conversation,
        before_sequence=before_sequence,
    )
    if state is not None:
        state_intent = state.payload.get("active_intent")
        if isinstance(state_intent, str) and state_intent:
            return state_intent
    rows = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.sender_type == "agent",
                    Message.sequence_no < before_sequence,
                )
                .order_by(Message.sequence_no.desc())
                .limit(5)
            )
        ).all()
    )
    for message in rows:
        payload = message.content_payload
        trace = payload.get("execution_trace") if isinstance(payload, Mapping) else None
        intent = trace.get("intent") if isinstance(trace, Mapping) else None
        if isinstance(intent, str) and intent:
            return intent
    return None


def _references_recent_cart_add(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    return any(marker in compact for marker in ("刚加入", "刚加的", "刚放进"))


async def _recent_successful_cart_add_label(
    session: AsyncSession,
    conversation: Conversation,
    *,
    before_sequence: int,
) -> str | None:
    """Resolve “刚加入的商品” only from the newest cart-add turn.

    A failed or ambiguous cart-add turn is a hard boundary.  Falling through to
    an older add can mutate an unrelated item that merely happens to be the only
    current cart line.
    """

    rows = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.sender_type == "agent",
                    Message.sequence_no < before_sequence,
                )
                .order_by(Message.sequence_no.desc())
                .limit(20)
            )
        ).all()
    )
    for message in rows:
        payload = message.content_payload
        trace = payload.get("execution_trace") if isinstance(payload, Mapping) else None
        intent = trace.get("intent") if isinstance(trace, Mapping) else None
        if intent != "cart_add":
            continue
        match = re.search(
            r"已将“(?P<product>.+?)”的“(?P<sku>.+?)”加入购物车",
            message.text_content or "",
        )
        if match is None:
            return None
        return f"{match.group('product')} {match.group('sku')}"
    return None


def _store_name_from_favorite_update_message(text: str) -> str | None:
    match = re.search(r"已(?:取消)?收藏店铺“(?P<name>[^”]+)”", text)
    return match.group("name") if match is not None else None


async def _recent_store_favorite_action_name(
    session: AsyncSession,
    conversation: Conversation,
    *,
    before_sequence: int,
) -> str | None:
    rows = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.sender_type == "agent",
                    Message.sequence_no < before_sequence,
                )
                .order_by(Message.sequence_no.desc())
                .limit(20)
            )
        ).all()
    )
    for message in rows:
        payload = message.content_payload
        trace = payload.get("execution_trace") if isinstance(payload, Mapping) else None
        intent = trace.get("intent") if isinstance(trace, Mapping) else None
        if intent == "favorite_update":
            return _store_name_from_favorite_update_message(message.text_content or "")
    return None


async def _recent_catalog_no_result(
    session: AsyncSession,
    conversation: Conversation,
    *,
    before_sequence: int,
) -> bool:
    rows = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.sender_type == "agent",
                    Message.sequence_no < before_sequence,
                )
                .order_by(Message.sequence_no.desc())
                .limit(5)
            )
        ).all()
    )
    for message in rows:
        payload = message.content_payload if isinstance(message.content_payload, Mapping) else {}
        trace = payload.get("execution_trace")
        intent = trace.get("intent") if isinstance(trace, Mapping) else None
        if intent not in {"product_search", "personalized_recommendation"}:
            continue
        cards = payload.get("product_cards")
        text = message.text_content or ""
        return not isinstance(cards, list) and any(
            marker in text for marker in ("没有找到", "暂未找到", "没有完全满足")
        )
    return False


def _is_implicit_refund_precheck_follow_up(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    if order_reference_index(value) is None:
        return False
    return _is_bare_order_choice(value) or any(
        marker in normalized
        for marker in ("只检查", "也检查", "同样检查", "也看看", "也看下", "那这笔呢")
    )


def _is_bare_order_choice(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    bare_value = re.sub(r"[^\w]+", "", normalized).replace("_", "")
    return bare_value in {
        "第一笔",
        "第二笔",
        "第三笔",
        "第四笔",
        "第五笔",
        "第1笔",
        "第2笔",
        "第3笔",
        "第4笔",
        "第5笔",
    }


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
        for marker in (
            "最近订单",
            "最近一笔",
            "最近一单",
            "最近下的那一单",
            "最近下的订单",
            "最近那笔",
            "最新订单",
            "最新那笔",
            "上一笔订单",
            "刚买",
            "刚买那笔",
            "刚下单",
        )
    )


def _requests_order_list(value: str) -> bool:
    """Distinguish a fresh order-list request from a focused-order follow-up."""

    normalized = re.sub(r"\s+", "", value).casefold()
    if order_reference_index(value) is not None or _resource_no(value, "ord") is not None:
        return False
    explicit_list_request = any(
        marker in normalized
        for marker in (
            "我都买过什么",
            "最近买过什么",
            "买过哪些",
            "购买记录",
            "有哪些订单",
            "哪些订单",
            "所有订单",
            "全部订单",
            "列出订单",
            "订单列表",
            "分别列出",
            "按订单卡片",
        )
    )
    quantity_list_request = (
        "订单" in normalized
        and re.search(
            r"(?:最近)?(?:[一二两三四五六七八九十]|\d+)笔(?:有效|可见)?订单",
            normalized,
        )
        is not None
    )
    state_markers = (
        "待付款",
        "待支付",
        "待发货",
        "运输中",
        "已发货",
        "待评价",
        "售后中",
        "退款中",
        "已完成",
    )
    requested_state_count = sum(marker in normalized for marker in state_markers)
    state_list_request = "订单" in normalized and (
        requested_state_count >= 2
        or (
            requested_state_count >= 1
            and any(
                marker in normalized
                for marker in (
                    "发来",
                    "给我",
                    "展示",
                    "列出",
                    "看看",
                    "查一下",
                    "有多少",
                    "几笔",
                    "哪些",
                )
            )
        )
    )
    return explicit_list_request or quantity_list_request or state_list_request


def _requested_order_eligibility_actions(value: str) -> list[str]:
    """Return actions from an explicit 'which orders can...' list question."""

    normalized = re.sub(r"\s+", "", value).casefold()
    if "订单" not in normalized or not any(
        marker in normalized
        for marker in ("哪些", "哪几笔", "哪一笔", "哪笔", "有什么", "可以", "能否", "还能")
    ):
        return []
    actions: list[str] = []
    for action, markers in (
        ("cancel_order", ("取消",)),
        ("confirm_receipt", ("确认收货",)),
        ("review", ("评价", "评论")),
        ("apply_after_sale", ("申请售后", "退款", "退货")),
    ):
        if any(marker in normalized for marker in markers):
            actions.append(action)
    return actions


def _asks_order_status_and_eligibility(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return (
        "状态" in normalized
        and any(marker in normalized for marker in ("分别", "各自", "都是什么", "都是"))
        and bool(_requested_order_eligibility_actions(value))
    )


def _references_purchased_item(value: str) -> bool:
    """Recognize a live-order query named by store or product rather than an ID."""

    normalized = re.sub(r"\s+", "", value).casefold()
    return any(
        marker in normalized for marker in ("我买的", "我买过的", "买过的", "我在你们店买", "我在")
    ) and any(
        marker in normalized
        for marker in ("发货", "签收", "收货", "物流", "快递", "评价", "售后", "订单")
    )


def _corrects_order_reference(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return any(
        marker in normalized for marker in ("列表", "刚才那些", "不是", "不对", "说的是", "改成")
    )


def _requests_address_mutation(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    if any(
        marker in normalized
        for marker in ("修改地址", "更改地址", "新增地址", "添加地址", "删除地址")
    ):
        return True
    return (
        re.search(
            r"(?:把|将)?(?:我的|默认|当前|这个|刚才的)?(?:收货)?地址"
            r"(?:直接|马上|立即|帮我)?(?:改成|改为|修改为|换成)",
            normalized,
        )
        is not None
    )


def _requests_harmful_illegal_guidance(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    illegal_goods = any(marker in normalized for marker in ("毒品", "冰毒", "海洛因")) and any(
        marker in normalized for marker in ("买", "卖", "找", "购买", "获取")
    )
    violent_guidance = any(
        marker in normalized for marker in ("杀人", "伤害别人", "怎么下毒")
    ) and any(marker in normalized for marker in ("怎么", "如何", "教我", "方法", "步骤"))
    return illegal_goods or violent_guidance


def _requests_compound_advice(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return any(
        marker in normalized
        for marker in (
            "建议我",
            "你建议",
            "建议",
            "下一步",
            "先做什么",
            "优先处理",
            "怎么安排",
            "怎么办",
            "帮我分析",
            "综合分析",
            "最应该先关注",
            "应该先关注",
            "先关注哪",
            "先看哪",
            "先处理哪",
        )
    )


def _requests_favorite_mutation(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return "收藏" in normalized and any(
        marker in normalized for marker in ("取消", "移除", "删除", "不再收藏", "解除收藏")
    )


def _asks_address_postcode(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return any(marker in normalized for marker in ("邮编", "邮政编码"))


def _asks_order_spend_summary(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return "订单" in normalized and any(
        marker in normalized
        for marker in (
            "一共花",
            "总共花",
            "累计花",
            "花了多少钱",
            "总金额",
            "合计金额",
            "累计实付",
            "总共实付",
            "一共实付",
            "累计消费",
            "已退款多少",
            "退款总额",
            "净支出",
        )
    )


def _requested_wallet_transaction_limit(value: str) -> int:
    normalized = re.sub(r"\s+", "", value).casefold()
    chinese_numbers = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5}
    match = re.search(r"(?:最近)?([一两二三四五\d]+)笔(?:余额)?(?:变动|流水|记录)", normalized)
    if match is None:
        return 3
    raw = match.group(1)
    parsed = int(raw) if raw.isdigit() else chinese_numbers.get(raw, 3)
    return max(1, min(parsed, 10))


def _is_chinese_trace_text(value: str) -> bool:
    """Only replace the Chinese public trace with provider text users can read."""

    return bool(re.search(r"[\u3400-\u9fff]", value))


def _requests_recommendation_with_memory(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return explicit_memory_request(value) is not None and any(
        marker in normalized for marker in ("推荐", "找几款", "找几件", "搜几款", "搜几件")
    )


def _memory_recommendation_query(memory_value: str, user_text: str) -> str:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    match = re.search(r"(?:推荐|找|搜索)([一两二三四五六七八\d]+)(?:款|件|个)", normalized)
    count_text = match.group(1) if match is not None else "5"
    return f"{memory_value} 推荐{count_text}款"


def _continues_catalog_constraints(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    has_changed_constraint = any(
        marker in normalized
        for marker in (
            "预算改",
            "价格改",
            "放宽到",
            "提高到",
            "调整到",
            "改到",
            "收紧到",
            "以内",
            "以下",
            "不超过",
            "从低到高",
            "从高到低",
        )
    )
    refers_to_previous = any(
        marker in normalized
        for marker in (
            "其他条件不变",
            "其他条件还是不变",
            "其余条件不变",
            "其余条件还是不变",
            "刚才那些",
            "刚才的结果",
            "上面的结果",
            "这些里面",
        )
    )
    return has_changed_constraint and refers_to_previous


def _previous_catalog_constraint_text(window: ContextWindow) -> str | None:
    """Return the latest substantive shopper catalogue request.

    Short acknowledgements and constraint-correction prompts are dialogue glue,
    not the source of the original audience, size or budget constraints.  The
    returned text is still untrusted and only feeds bounded catalogue parsers.
    """

    for turn in reversed(window.recent_turns):
        if turn.role != "用户":
            continue
        normalized = re.sub(r"\s+", "", turn.text).casefold()
        if is_short_affirmative(turn.text):
            continue
        if "不变" in normalized and len(normalized) <= 40:
            continue
        if any(
            marker in normalized
            for marker in (
                "推荐",
                "搜索",
                "查找",
                "找",
                "商品",
                "女装",
                "男装",
                "文具",
                "元以内",
                "斤",
                "深色",
                "浅色",
            )
        ) and not any(marker in normalized for marker in ("订单", "退款", "物流")):
            return turn.text
    return None


def _requests_sku_stock_extreme(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return "库存" in normalized and any(
        marker in normalized
        for marker in ("库存最少", "库存最低", "库存最多", "库存最高", "哪一款", "哪款")
    )


def _sku_stock_extreme_answer(user_text: str, item: Mapping[str, Any]) -> str | None:
    if not _requests_sku_stock_extreme(user_text):
        return None
    sku_values = item.get("skus")
    rows = (
        [row for row in sku_values if isinstance(row, Mapping)]
        if isinstance(sku_values, list)
        else []
    )
    if not rows:
        return "当前商品没有可核验的在售款式库存。"
    wants_maximum = any(
        marker in re.sub(r"\s+", "", user_text).casefold() for marker in ("库存最多", "库存最高")
    )
    quantities = [max(0, int(row.get("available_stock") or 0)) for row in rows]
    extreme = (max if wants_maximum else min)(quantities)
    names = [
        safe_untrusted_excerpt(row.get("sku_name") or "默认款式", 80)
        for row, quantity in zip(rows, quantities, strict=True)
        if quantity == extreme
    ]
    direction = "最高" if wants_maximum else "最低"
    if len(names) == len(rows):
        return (
            f"当前 {len(rows)} 个在售款式的实时库存相同，均为 {extreme} 件，"
            f"所以没有唯一的库存{direction}款式。完整款式已放在下方卡片中。"
        )
    label = "、".join(names[:4])
    suffix = f"等 {len(names)} 款" if len(names) > 4 else ""
    return (
        f"当前库存{direction}的是 {label}{suffix}，每款可售 {extreme} 件。"
        "完整款式和价格已放在下方卡片中。"
    )


def _contains_product_collection_of_three(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return any(
        marker in normalized
        for marker in (
            "这三件",
            "这三个",
            "三件里",
            "三个里",
            "刚才这三件",
            "这几件",
            "这几个",
            "刚才这些",
        )
    )


def _favorite_update_enabled(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).casefold()
    if any(
        marker in compact for marker in ("重新收藏", "恢复收藏", "再次收藏", "再收藏", "重新关注")
    ):
        return True
    return not any(marker in compact for marker in ("取消收藏", "移出收藏", "删除收藏", "取消关注"))


def _targets_store_favorite_update(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return any(marker in normalized for marker in ("收藏", "关注")) and any(
        marker in normalized
        for marker in (
            "店铺",
            "商家",
            "这家店",
            "这个店",
            "该店",
            "专卖店",
            "旗舰店",
            "商店",
        )
    )


def _review_draft_from_request(value: str) -> str:
    """Keep only the shopper-authored review content, never operation instructions."""

    candidate = value
    if match := re.search(r"(?:评价草稿|评价内容|草稿)[\uff1a:]\s*(.+)", value):
        candidate = match.group(1)
    else:
        candidate = re.sub(
            r"^.*?(?:帮我写(?:一段)?(?:评价|好评|差评)|写一段(?:五星)?(?:评价|好评|差评)|"
            r"拟一段(?:评价|好评|差评)|整理成评价)[\uff1a:,，\s]*",
            "",
            value,
        )
    candidate = re.split(
        r"[。\uff1b;，,]?\s*(?:不要提交|先不提交|别提交|不发布|先不发布|直接提交|帮我提交)",
        candidate,
        maxsplit=1,
    )[0]
    candidate = candidate.strip(" ，,。;:")
    candidate = re.sub(r"(?:并且|并|然后|随后)$", "", candidate).strip()
    if any(
        marker in candidate
        for marker in (
            "找一笔",
            "待评价的订单",
            "评价草稿",
            "帮我准备",
            "替我选星级",
            "选星级",
        )
    ):
        candidate = ""
    if candidate in {"", "五星", "五星好评", "好评"}:
        candidate = (
            "整体体验很好，商品符合预期，我很满意。"
            if any(marker in value for marker in ("五星", "好评"))
            else "商品已收到，具体质量和使用感受会根据真实体验补充。"
        )
    elif candidate in {"差评", "一星", "一星差评"}:
        candidate = "这次购物体验没有达到预期，具体问题建议按真实情况补充。"
    return safe_untrusted_excerpt(candidate or "请根据我的真实体验补充评价内容", 300)


def _product_stock_extreme_answer(user_text: str, items: list[object]) -> str | None:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    wants_minimum = any(marker in normalized for marker in ("库存最少", "库存最低"))
    wants_maximum = any(marker in normalized for marker in ("库存最多", "库存最高"))
    if not (wants_minimum or wants_maximum):
        return None
    candidates: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        try:
            stock = max(0, int(item.get("available_stock") or 0))
        except (TypeError, ValueError):
            continue
        candidates.append((stock, safe_untrusted_excerpt(item.get("name") or "商品", 80)))
    if len(candidates) < 2:
        return None
    extreme = (min if wants_minimum else max)(stock for stock, _name in candidates)
    names = [name for stock, name in candidates if stock == extreme]
    direction = "最少" if wants_minimum else "最多"
    answer = (
        f"刚才这些商品中，库存{direction}的是“{'、'.join(names)}”，"
        f"全部款式合计可售 {extreme} 件。实时库存已重新核验，点击商品卡可以继续查看款式。"
    )
    if any(marker in normalized for marker in ("为什么推荐", "为什么选", "为何推荐")):
        answer += (
            "这里是按库存指标找出的结果，不代表它一定更值得买。"
            "是否适合还要结合你的用途、预算和款式偏好判断。"
        )
    return answer


def _comparative_product_card(
    user_text: str, cards: list[dict[str, object]]
) -> dict[str, object] | None:
    """Resolve `the cheaper one` against the latest displayed comparison cards."""

    normalized = re.sub(r"\s+", "", user_text).casefold()
    wants_cheapest = any(
        marker in normalized for marker in ("便宜的那个", "更便宜的", "最便宜的", "价格低的那个")
    )
    wants_priciest = any(
        marker in normalized for marker in ("贵的那个", "更贵的", "最贵的", "价格高的那个")
    )
    if not (wants_cheapest or wants_priciest) or len(cards) < 2:
        return None
    priced: list[tuple[int, dict[str, object]]] = []
    for card in cards:
        price = card.get("price")
        if not isinstance(price, Mapping):
            continue
        try:
            minor_units = max(0, int(price.get("minor_units") or 0))
        except (TypeError, ValueError):
            continue
        priced.append((minor_units, card))
    if len(priced) < 2:
        return None
    selector = min if wants_cheapest else max
    return selector(priced, key=lambda value: value[0])[1]


def _preferred_sku_nos(data: Mapping[str, Any], user_text: str) -> dict[str, str] | None:
    """Select a SKU that satisfies an explicitly requested color for chat cards."""

    colors = _explicit_catalog_colors(user_text) or _preferred_catalog_colors(user_text)
    weight_match = re.search(r"(?P<weight>\d{2,3})斤", user_text)
    requested_weight = int(weight_match.group("weight")) if weight_match is not None else None
    items = data.get("items")
    if (not colors and requested_weight is None) or not isinstance(items, list):
        return None
    selected: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("product_id"), str):
            continue
        skus = item.get("skus")
        for sku in skus if isinstance(skus, list) else []:
            if not isinstance(sku, Mapping) or not isinstance(sku.get("sku_id"), str):
                continue
            sku_name = str(sku.get("sku_name") or "").casefold()
            weight_limits = [
                int(value) for value in re.findall(r"(\d{2,3})斤(?:以下|以内)?", sku_name)
            ]
            color_matches = not colors or any(color in sku_name for color in colors)
            weight_matches = requested_weight is None or (
                bool(weight_limits) and max(weight_limits) >= requested_weight
            )
            if color_matches and weight_matches:
                selected[str(item["product_id"])] = str(sku["sku_id"])
                break
    return selected or None


def _result_preferred_sku_nos(data: Mapping[str, Any], user_text: str) -> dict[str, str] | None:
    """Prefer a SKU preserved from the user's visible result set.

    Constraint-only follow-ups filter existing product cards and therefore do
    not need to repeat the full SKU catalogue in their tool result.  The
    preserved mapping keeps the visible variant stable without trusting model
    output or weakening the catalogue scope check.
    """

    preserved = data.get("_preferred_sku_nos")
    if isinstance(preserved, Mapping):
        values = {
            str(product_no): str(sku_no)
            for product_no, sku_no in preserved.items()
            if isinstance(product_no, str) and isinstance(sku_no, str)
        }
        if values:
            return values
    return _preferred_sku_nos(data, user_text)


def _requests_logistics_and_refund_precheck(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    asks_logistics = any(term in normalized for term in ("物流", "快递", "包裹", "到哪"))
    asks_after_sale = any(term in normalized for term in ("售后", "退款", "退货"))
    asks_check = any(
        term in normalized
        for term in (
            "检查",
            "能不能",
            "能否",
            "是否",
            "资格",
            "不要提交",
            "别提交",
            "先不提交",
        )
    )
    return asks_logistics and asks_after_sale and asks_check


def _requests_multiple_logistics(value: str) -> bool:
    """Recognize a collection-level shipment request without guessing an order."""

    normalized = re.sub(r"\s+", "", value).casefold()
    asks_logistics = any(marker in normalized for marker in ("物流", "快递", "包裹", "运输中"))
    asks_collection = any(
        marker in normalized
        for marker in (
            "所有",
            "全部",
            "每一笔",
            "每笔",
            "分别",
            "都到哪",
            "有哪些快递",
            "几个包裹",
        )
    )
    return asks_logistics and asks_collection


def _requests_unscoped_logistics_lookup(value: str) -> bool:
    """Recognize a generic personal delivery question without a chosen order."""

    normalized = re.sub(r"\s+", "", value).casefold()
    asks_logistics = any(marker in normalized for marker in ("我的快递", "我的物流", "包裹到哪"))
    has_explicit_choice = any(
        marker in normalized
        for marker in (
            "第一笔",
            "第二笔",
            "第三笔",
            "第四笔",
            "第五笔",
            "这笔",
            "这个订单",
            "刚才那笔",
            "最近一笔",
        )
    )
    return asks_logistics and not has_explicit_choice and not _requests_multiple_logistics(value)


def _requests_catalog_then_cart_choice(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    asks_add = any(
        marker in normalized for marker in ("加入购物车", "加到购物车", "放进购物车", "放到购物车")
    )
    asks_search = any(marker in normalized for marker in ("搜索", "查找", "帮我找", "找一", "推荐"))
    return asks_add and asks_search


def _has_signed_shipment(data: Mapping[str, Any]) -> bool:
    items = data.get("items")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, Mapping)
        and str(item.get("shipment_status") or "").casefold()
        in {"delivered", "signed", "received", "completed"}
        for item in items
    )


def _disclaims_specific_order(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    if re.search(
        r"(?:没有|没|尚未|还没)(?:明确)?(?:指定|选择|选|说)(?:是)?哪(?:一)?(?:笔|个)?(?:订单)?",
        normalized,
    ):
        return True
    return any(
        marker in normalized
        for marker in (
            "没有指定哪一笔",
            "没指定哪一笔",
            "没有说哪一笔",
            "没说哪一笔",
            "不知道哪一笔",
            "不确定哪一笔",
            "先让我选订单",
            "先给我选订单",
            "不要默认订单",
            "别默认订单",
        )
    )


def _asks_human_service_capabilities(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    has_human = any(term in normalized for term in ("人工客服", "平台客服", "人工", "真人"))
    asks_information = any(
        term in normalized
        for term in ("能处理什么", "可以处理什么", "能做什么", "可以做什么", "服务范围")
    )
    return has_human and asks_information


def _is_unambiguous_general_chat(value: str) -> bool:
    """Avoid a slow model-planning round trip for clear social/capability turns.

    The answer is still generated by the configured model with dialogue context;
    this only prevents old policy/order turns from contaminating the new intent.
    """

    normalized = re.sub(r"\s+", "", value).casefold().strip("\u3002\uff01\uff1f!?\uff0c,")
    if normalized in {
        "你好",
        "您好",
        "哈喽",
        "hello",
        "hi",
        "谢谢",
        "谢谢你",
        "辛苦了",
        "你是谁",
    }:
        return True
    if re.search(r"你.*(?:能|可以).*帮我.*(?:做|干).*什么", normalized):
        return True
    if "搜索框" in normalized and any(
        marker in normalized for marker in ("你和", "区别", "不同", "比搜索")
    ):
        return True
    return any(
        marker in normalized
        for marker in (
            "你能做什么",
            "你可以做什么",
            "你能帮我什么",
            "可以帮我干什么",
            "服务范围",
            "怎么用你",
        )
    )


def _exclusive_detail_cards(
    plan: ExclusiveAgentPlan,
    data: Mapping[str, Any],
) -> list[dict[str, object]]:
    hypothetical = data.get("cart_hypothetical")
    if plan.intent == "checkout_preview" and isinstance(data.get("checkout_id"), str):
        store_values = data.get("store_groups")
        store_rows = []
        for group in (store_values if isinstance(store_values, list) else [])[:8]:
            if not isinstance(group, Mapping):
                continue
            goods = group.get("goods_amount")
            freight = group.get("freight_amount")
            store_rows.append(
                {
                    "label": safe_untrusted_excerpt(group.get("store_name") or "店铺", 80),
                    "value": (
                        _money_object_display(goods) if isinstance(goods, Mapping) else "金额待确认"
                    ),
                    "meta": (
                        f"运费 {_money_object_display(freight)}"
                        if isinstance(freight, Mapping)
                        else "运费待确认"
                    ),
                }
            )
        amounts = data.get("amounts")
        payable = amounts.get("payable_amount") if isinstance(amounts, Mapping) else None
        checkout_no = str(data["checkout_id"])
        return [
            {
                "kind": "checkout_preview",
                "icon": "结",
                "eyebrow": "结算预览",
                "title": (
                    f"应付 {_money_object_display(payable)}"
                    if isinstance(payable, Mapping)
                    else "结算金额待确认"
                ),
                "badge": "尚未创建订单",
                "summary": "已校验当前选中商品、默认地址、库存和金额，没有付款。",
                "rows": store_rows,
                "action": {
                    "label": "打开结算弹窗",
                    "path": f"/checkout/{checkout_no}",
                },
            }
        ]
    if plan.intent == "cart_lookup" and isinstance(hypothetical, Mapping):
        return [
            {
                "kind": "cart_hypothetical",
                "icon": "算",
                "eyebrow": "购物车试算",
                "title": "仅试算，不修改购物车",
                "badge": "预计金额",
                "summary": "按当前购物车价格和选中状态计算，实际结算以结算页为准。",
                "rows": [
                    {
                        "label": safe_untrusted_excerpt(
                            hypothetical.get("item_label") or "目标商品", 100
                        ),
                        "value": (
                            f"{int(hypothetical.get('from_quantity') or 0)} 件 → "
                            f"{int(hypothetical.get('to_quantity') or 0)} 件"
                        ),
                        "meta": f"单价 {hypothetical.get('unit_price_display', '¥0.00')}",
                    },
                    {
                        "label": "当前已选金额",
                        "value": str(hypothetical.get("current_total_display") or "¥0.00"),
                        "meta": "修改前",
                    },
                    {
                        "label": "预计已选金额",
                        "value": str(hypothetical.get("projected_total_display") or "¥0.00"),
                        "meta": "本次没有修改购物车",
                    },
                ],
                "action": {"label": "打开购物车", "path": "/cart"},
            }
        ]
    if data.get("catalog_focus") == "sku_availability":
        values = data.get("items")
        item = values[0] if isinstance(values, list) and values else None
        if isinstance(item, Mapping):
            sku_values = item.get("skus")
            sku_rows = []
            for sku in (sku_values if isinstance(sku_values, list) else [])[:10]:
                if not isinstance(sku, Mapping):
                    continue
                price = sku.get("price")
                available_stock = max(0, int(sku.get("available_stock") or 0))
                availability = safe_untrusted_excerpt(
                    sku.get("availability_label")
                    or ("有货" if available_stock > 0 else "缺货"),
                    30,
                )
                sku_rows.append(
                    {
                        "label": safe_untrusted_excerpt(sku.get("sku_name") or "默认款式", 80),
                        "value": (
                            _money_object_display(price)
                            if isinstance(price, Mapping)
                            else "价格待确认"
                        ),
                        "meta": f"{availability} · 可售 "
                        f"{available_stock} 件",
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
                    "rows": sku_rows,
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
    profile_value = data.get("profile")
    if plan.intent == "address_lookup" and isinstance(profile_value, Mapping):
        email = safe_untrusted_excerpt(profile_value.get("email") or "未设置", 120)
        return [
            {
                "kind": "account_profile",
                "icon": "我",
                "eyebrow": "账号资料",
                "title": safe_untrusted_excerpt(profile_value.get("username") or "当前用户", 64),
                "badge": "当前账号",
                "summary": "账号和邮箱来自当前登录用户的实时资料。",
                "rows": [
                    {
                        "label": "用户名",
                        "value": safe_untrusted_excerpt(
                            profile_value.get("username") or "未设置", 64
                        ),
                        "meta": "登录账号",
                    },
                    {"label": "邮箱", "value": email, "meta": "可在账号与安全中换绑"},
                ],
                "action": {"label": "账号与安全", "path": "/me/settings/security"},
            }
        ]
    if plan.intent == "wallet_lookup":
        balance = data.get("balance")
        transaction_values = data.get("transactions")
        transaction_limit = max(1, min(int(data.get("transaction_limit") or 3), 10))
        transaction_rows = []
        transaction_labels = {
            "recharge": "余额充值",
            "payment": "订单支付",
            "refund": "退款入账",
        }
        for item in (transaction_values if isinstance(transaction_values, list) else [])[
            :transaction_limit
        ]:
            if not isinstance(item, Mapping):
                continue
            amount_value = item.get("amount")
            amount_text = (
                _money_object_display(amount_value)
                if isinstance(amount_value, Mapping)
                else "¥0.00"
            )
            direction = str(item.get("direction") or "")
            transaction_rows.append(
                {
                    "label": transaction_labels.get(
                        str(item.get("transaction_type") or ""),
                        safe_untrusted_excerpt(item.get("description") or "余额变动", 60),
                    ),
                    "value": f"{'+' if direction == 'credit' else '-'}{amount_text}",
                    "meta": _localized_agent_time(
                        safe_untrusted_excerpt(item.get("occurred_at") or "", 40)
                    ),
                }
            )
        return [
            {
                "kind": "wallet_summary",
                "icon": "钱",
                "eyebrow": "账户资产",
                "title": "我的余额",
                "badge": "实时余额",
                "summary": "余额来自当前登录账号，付款后会按实际支付金额更新。",
                "rows": [
                    {
                        "label": "可用余额",
                        "value": (
                            _money_object_display(balance)
                            if isinstance(balance, Mapping)
                            else "¥0.00"
                        ),
                        "meta": "人民币",
                    },
                    *transaction_rows,
                ],
                "action": {"label": "查看账户", "path": "/me"},
            }
        ]
    if plan.intent in {"favorites_lookup", "favorite_update"}:
        stores = data.get("followed_stores")
        favorite_count = int(data.get("favorite_product_count") or 0)
        followed_count = int(data.get("followed_store_count") or 0)
        store_rows = [
            {
                "label": safe_untrusted_excerpt(item.get("store_name") or "店铺", 80),
                "value": "查看店铺",
                "meta": (
                    f"评分 {safe_untrusted_excerpt(item.get('rating') or '暂无', 20)}"
                    if isinstance(item, Mapping)
                    else ""
                ),
            }
            for item in (stores if isinstance(stores, list) else [])[:8]
            if isinstance(item, Mapping)
        ]
        return [
            {
                "kind": "favorite_stores",
                "icon": "藏",
                "eyebrow": "我的收藏",
                "title": f"收藏商品 {favorite_count} 件 · 店铺 {followed_count} 家",
                "badge": "当前账号",
                "summary": "收藏商品以商品卡片展示。收藏店铺可从下方入口统一管理。",
                "rows": store_rows,
                "action": {"label": "管理店铺收藏", "path": "/me/favorites/stores"},
            }
        ]
    if plan.intent == "review_draft":
        draft = safe_untrusted_excerpt(data.get("review_draft") or "", 300)
        order_values = data.get("items")
        order = order_values[0] if isinstance(order_values, list) and order_values else None
        order_no = order.get("order_id") if isinstance(order, Mapping) else None
        return [
            {
                "kind": "review_draft",
                "icon": "评",
                "eyebrow": "评价草稿",
                "title": "请先核对评价内容",
                "badge": "尚未提交",
                "summary": draft or "尚未提供具体评价内容。",
                "rows": [
                    {
                        "label": "评价内容",
                        "value": draft or "待补充",
                        "meta": "不会替你捏造使用体验",
                    },
                    {"label": "星级", "value": "未设置", "meta": "提交前由你选择"},
                ],
                "action": (
                    {
                        "resource_type": "order",
                        "resource_id": order_no,
                        "label": "打开订单评价",
                    }
                    if isinstance(order_no, str)
                    else {"label": "查看待评价订单", "path": "/me/orders"}
                ),
            }
        ]
    if plan.intent == "memory_lookup":
        memory = data.get("memory")
        authorized = isinstance(memory, Mapping) and memory.get("authorized") is True
        values = data.get("recalled_memories")
        rows = [
            {
                "label": safe_untrusted_excerpt(item.get("value") or "购物偏好", 160),
                "value": "已记住",
                "meta": safe_untrusted_excerpt(item.get("memory_type") or "偏好", 30),
            }
            for item in (values if isinstance(values, list) else [])[:5]
            if isinstance(item, Mapping)
        ]
        return [
            {
                "kind": "memory_summary",
                "icon": "忆",
                "eyebrow": "长期记忆",
                "title": "我记得的购物偏好",
                "badge": "已授权" if authorized else "未授权",
                "summary": (
                    "只展示你确认保存且仍在有效期内的购物偏好。"
                    if authorized
                    else "尚未开启个性化记忆授权，因此没有读取长期偏好。"
                ),
                "rows": rows,
                "action": {"label": "管理记忆", "path": "/me/settings/ai-personalization"},
            }
        ]
    if plan.intent == "product_compare":
        values = data.get("items")
        comparison_rows = []
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
            comparison_rows.append(
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
        if comparison_rows:
            return [
                {
                    "kind": "product_compare",
                    "icon": "比",
                    "eyebrow": "商品对比",
                    "title": "关键购买信息",
                    "badge": f"{len(comparison_rows)} 件商品",
                    "summary": "同一口径比较当前公开价格、款式、库存、销量与评分。",
                    "rows": comparison_rows,
                }
            ]
    if plan.intent == "logistics_lookup":
        values = data.get("items")
        order_values = data.get("matched_order_ids") or data.get("order_ids")
        order_numbers = (
            [str(value) for value in order_values if isinstance(value, str)]
            if isinstance(order_values, list)
            else []
        )
        logistics_rows: list[dict[str, object]] = []
        for item in (values if isinstance(values, list) else [])[:5]:
            if not isinstance(item, Mapping):
                continue
            last_track = item.get("last_track")
            track: Mapping[str, Any] = last_track if isinstance(last_track, Mapping) else {}
            location = safe_untrusted_excerpt(track.get("location_text") or "位置更新中", 80)
            description = safe_untrusted_excerpt(track.get("description") or "暂无最新轨迹", 120)
            tracking_no = _compact_tracking_no(item.get("tracking_no_masked"))
            item_order_no = item.get("order_id")
            order_prefix = (
                f"第 {order_numbers.index(item_order_no) + 1} 笔订单 · "
                if len(order_numbers) > 1
                and isinstance(item_order_no, str)
                and item_order_no in order_numbers
                else ""
            )
            logistics_rows.append(
                {
                    "label": order_prefix
                    + safe_untrusted_excerpt(item.get("carrier_name") or "物流包裹", 80),
                    "value": _status_label("shipment", item.get("shipment_status")),
                    "meta": f"{tracking_no} · {location} · {description}",
                }
            )
        if not logistics_rows:
            return []
        order_no = data.get("order_id")
        if not isinstance(order_no, str) and len(order_numbers) == 1:
            order_no = order_numbers[0]
        logistics_cards: list[dict[str, object]] = [
            {
                "kind": "logistics",
                "icon": "运",
                "eyebrow": "订单物流",
                "title": "包裹最新进度",
                "badge": "实时轨迹",
                "summary": "物流节点按承运商最近一次同步结果展示。",
                "rows": logistics_rows,
                "action": (
                    {"resource_type": "order", "resource_id": order_no, "label": "查看完整物流"}
                    if isinstance(order_no, str)
                    else None
                ),
            }
        ]
        combined = data.get("combined_refund_precheck")
        if isinstance(combined, Mapping):
            logistics_cards.extend(
                _exclusive_detail_cards(ExclusiveAgentPlan("refund_precheck"), combined)
            )
        return logistics_cards
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
            blocking_labels = {
                "REFUND_ITEM_CAPACITY_CHANGED": (
                    "商品可售后数量已被现有申请占用，请查看进行中的售后"
                ),
                "ORDER_NOT_REFUNDABLE": "订单当前支付或交易状态不支持再次发起售后",
            }
            refund_rows.append(
                {
                    "label": "暂不可申请",
                    "value": "需要处理",
                    "meta": safe_untrusted_excerpt(
                        "、".join(
                            blocking_labels.get(str(value), "订单当前不满足售后条件")
                            for value in blocking
                        ),
                        180,
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
        refund_progress_cards: list[dict[str, object]] = []
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
            reason_detail = safe_untrusted_excerpt(item.get("reason_detail") or "", 180)
            reason_code = safe_untrusted_excerpt(item.get("reason_code") or "", 64)
            if reason_detail or reason_code:
                reason_labels = {
                    "NO_LONGER_NEEDED": "不再需要",
                    "NOT_AS_DESCRIBED": "与商品描述不符",
                    "QUALITY_ISSUE": "商品质量问题",
                    "WRONG_OR_MISSING_ITEM": "错发或漏发",
                    "DAMAGED": "商品破损",
                    "OTHER": "其他原因",
                }
                progress_rows.append(
                    {
                        "label": "申请原因",
                        "value": reason_detail or reason_labels.get(reason_code, reason_code),
                        "meta": "用户提交的售后原因",
                    }
                )
            refund_progress_cards.append(
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
        return refund_progress_cards
    if plan.intent == "policy_qa":
        values = data.get("knowledge_sources")
        query = safe_untrusted_excerpt(
            data.get("policy_retrieval_query") or data.get("policy_query") or "", 500
        )
        query_terms = {
            term
            for term in (
                "充值",
                "微信",
                "支付宝",
                "余额",
                "提现",
                "转账",
                "支付",
                "退款",
                "原路",
                "原支付渠道",
                "退货",
                "售后",
                "到账",
                "多久",
                "时效",
                "自动更新",
                "自动推进",
                "更新机制",
                "更新规则",
                "固定",
                "每5秒",
                "每五秒",
                "每隔5秒",
                "每隔五秒",
                "几秒",
                "更新",
                "物流",
                "快递",
                "包裹",
                "未收到",
                "没收到",
                "新轨迹",
                "丢件",
                "异常",
                "发货",
                "签收",
                "运费",
                "包邮",
                "暂停营业",
                "恢复营业",
                "购物车",
                "不可购买",
                "结算",
                "新订单",
                "隐私",
                "账号",
                "密码",
                "人工",
                "客服",
            )
            if term in query
        }
        required_domain_terms: set[str] = set()
        if query_terms.intersection({"提现", "转账", "原路", "原支付渠道"}):
            required_domain_terms = {
                "余额",
                "提现",
                "转账",
                "原路",
                "原支付渠道",
            }
        elif query_terms.intersection({"退款", "退货", "售后"}):
            required_domain_terms = {"退款", "退货", "售后"}
        elif query_terms.intersection(
            {
                "物流",
                "快递",
                "包裹",
                "未收到",
                "没收到",
                "新轨迹",
                "丢件",
                "异常",
                "发货",
                "签收",
                "运费",
                "包邮",
            }
        ):
            required_domain_terms = {
                "物流",
                "快递",
                "包裹",
                "轨迹",
                "丢件",
                "发货",
                "签收",
                "运费",
                "包邮",
            }
        elif query_terms.intersection(
            {"暂停营业", "恢复营业", "购物车", "不可购买", "结算", "新订单"}
        ):
            required_domain_terms = {
                "暂停营业",
                "恢复营业",
                "购物车",
                "不可购买",
                "结算",
                "新订单",
            }
        elif query_terms.intersection({"充值", "微信", "支付宝"}):
            required_domain_terms = {"充值", "微信", "支付宝"}
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
            if required_domain_terms and not any(
                term in searchable for term in required_domain_terms
            ):
                continue
            score = sum(1 for term in query_terms if term in searchable)
            ranked.append((score, -position, item))
        ranked.sort(key=lambda value: (value[0], value[1]), reverse=True)
        best_score = ranked[0][0] if ranked else 0
        policy_rows: list[dict[str, object]] = []
        seen_documents: set[str] = set()
        for score, _, item in ranked:
            if query_terms and score == 0:
                continue
            if query_terms and best_score >= 2 and score < best_score - 1:
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
            best_excerpt = _best_policy_excerpt(source_text, query_terms)
            if required_domain_terms and not any(
                term in best_excerpt for term in required_domain_terms
            ):
                continue
            policy_rows.append(
                {
                    "label": title,
                    "value": "已发布",
                    "meta": best_excerpt,
                }
            )
            if len(policy_rows) >= 2:
                break
        if policy_rows:
            return [
                {
                    "kind": "platform_policy",
                    "icon": "规",
                    "eyebrow": "平台规则",
                    "title": "本次回答依据",
                    "badge": "知识库已核验",
                    "summary": "只展示与当前问题相关的已发布规则来源。",
                    "rows": policy_rows,
                }
            ]
    return []


def _best_policy_excerpt(source_text: str, query_terms: set[str]) -> str:
    candidates: list[tuple[int, int, str]] = []
    for position, raw_part in enumerate(
        re.split(r"(?<=[\u3002\uff01\uff1f\uff1b])|\n+", source_text)
    ):
        for fragment_position, fragment in enumerate(raw_part.split(" - ")):
            part = re.sub(r"^(?:#+\s*|[-*•>]\s*|\d+[.)、]\s*)", "", fragment.strip())
            part = part.replace(";", "\uff1b").replace(",", "\uff0c")
            if not part:
                continue
            score = sum(1 for term in query_terms if term in part)
            candidates.append(
                (
                    score,
                    -(position * 100 + fragment_position),
                    safe_untrusted_excerpt(part, 120),
                )
            )
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


def _address_cards(data: Mapping[str, Any]) -> list[dict[str, object]]:
    values = data.get("items")
    cards: list[dict[str, object]] = []
    for item in (values if isinstance(values, list) else [])[:8]:
        if not isinstance(item, Mapping):
            continue
        cards.append(
            {
                "address_id": item.get("address_id"),
                "recipient_name": safe_untrusted_excerpt(item.get("recipient_name"), 64),
                "phone": safe_untrusted_excerpt(item.get("phone"), 32),
                "country_code": item.get("country_code"),
                "province_code": item.get("province_code"),
                "city_code": item.get("city_code"),
                "district_code": item.get("district_code"),
                "address": safe_untrusted_excerpt(item.get("address"), 500),
                "is_default": item.get("is_default") is True,
            }
        )
    return cards


def _requested_cart_add_quantity(value: str) -> int:
    compact = re.sub(r"\s+", "", value).casefold()
    for pattern in (
        r"数量(?:改成|改为|改回|是|为)?(\d{1,2})",
        r"(?:改成|改为|改回|调成|调整为)(\d{1,2})件?",
        r"(?:加入|加到|放进|放到)(?:购物车|购物袋)?(?:里|中)?[，,]?(\d{1,2})件",
        r"(?:加入|加到|放进|放到)(\d{1,2})件",
    ):
        match = re.search(pattern, compact)
        if match is not None:
            return min(99, max(1, int(match.group(1))))
    return 1


def _requested_product_sku(
    data: Mapping[str, Any],
    user_text: str,
) -> Mapping[str, Any] | None:
    """Return one explicitly named SKU from a trusted product projection."""

    compact = re.sub(r"\s+", "", user_text).casefold()
    items = data.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], Mapping):
        return None
    skus = items[0].get("skus")
    matches: list[Mapping[str, Any]] = []
    for sku in skus if isinstance(skus, list) else []:
        if not isinstance(sku, Mapping):
            continue
        sku_name = re.sub(r"\s+", "", str(sku.get("sku_name") or "")).casefold()
        if sku_name and sku_name in compact:
            matches.append(sku)
    return matches[0] if len(matches) == 1 else None


def _cart_add_verification_text(
    data: Mapping[str, Any],
    user_text: str,
) -> str | None:
    compact = re.sub(r"\s+", "", user_text).casefold()
    if not any(marker in compact for marker in ("是不是", "是否", "对不对", "正确吗", "核对")):
        return None
    variant_match = re.search(r"(?P<label>\d+支)", compact)
    if variant_match is None:
        return None
    requested_label = variant_match.group("label")
    cart_items: list[Mapping[str, Any]] = []
    groups = data.get("groups")
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, Mapping):
            continue
        values = group.get("items")
        cart_items.extend(
            item
            for item in (values if isinstance(values, list) else [])
            if isinstance(item, Mapping)
        )
    matches = [
        item
        for item in cart_items
        if requested_label in re.sub(r"\s+", "", str(item.get("sku_name") or "")).casefold()
    ]
    if len(matches) == 1:
        item = matches[0]
        return (
            f"是，购物车中的“{safe_untrusted_excerpt(item.get('product_name') or '该商品', 100)}”"
            f"当前款式为“{safe_untrusted_excerpt(item.get('sku_name') or requested_label, 80)}”，"
            f"数量 {int(item.get('quantity') or 0)} 件。本次只是核对，没有重复加入。"
        )
    return f"不是。当前购物车中没有“{requested_label}”款，本次只是核对，没有新增商品。"


def _select_cart_item(
    data: Mapping[str, Any],
    user_text: str,
) -> Mapping[str, Any] | None:
    """Resolve exactly one existing cart line without guessing between ties."""

    compact = re.sub(r"\s+", "", user_text).casefold()
    groups = data.get("groups")
    items: list[Mapping[str, Any]] = []
    for raw_group in groups if isinstance(groups, list) else []:
        if not isinstance(raw_group, Mapping):
            continue
        raw_items = raw_group.get("items")
        items.extend(
            raw_item
            for raw_item in (raw_items if isinstance(raw_items, list) else [])
            if isinstance(raw_item, Mapping)
        )
    if not items:
        return None

    ordinal_match = re.search(
        r"第(?P<index>\d+|[一二两三四五六七八九十])(?:件|个|项)(?:商品)?",
        compact,
    )
    if ordinal_match is not None:
        ordinal = _natural_quantity(ordinal_match.group("index"))
        if ordinal is not None and 1 <= ordinal <= len(items):
            return items[ordinal - 1]
        return None
    if len(items) == 1:
        return items[0]

    target_text = re.split(
        r"(?:，|,|;|。)?(?:保留|不要删|别删|不要修改|别修改)",
        compact,
        maxsplit=1,
    )[0]
    scored: list[tuple[int, Mapping[str, Any]]] = []
    for item in items:
        fields = (
            re.sub(r"\s+", "", str(item.get("product_name") or "")).casefold(),
            re.sub(r"\s+", "", str(item.get("sku_name") or "")).casefold(),
        )
        score = max(
            (
                SequenceMatcher(None, target_text, field).find_longest_match().size
                for field in fields
                if field
            ),
            default=0,
        )
        scored.append((score, item))
    scored.sort(key=lambda entry: entry[0], reverse=True)
    best_score = scored[0][0]
    if best_score < 2 or sum(score == best_score for score, _ in scored) != 1:
        return None
    return scored[0][1]


def _cart_hypothetical_projection(
    user_text: str,
    data: Mapping[str, Any],
) -> dict[str, object] | None:
    """Conservatively calculate one quantity change without mutating the cart.

    A projection is returned only when the requested old quantity matches exactly one
    selected, valid cart line. Ambiguous requests keep the normal live cart card so the
    shopper can choose the intended item instead of receiving a guessed amount.
    """

    compact = re.sub(r"\s+", "", user_text).casefold()
    absolute_match = re.search(
        r"从(?P<old>\d+|[一二两三四五六七八九十])件?"
        r"(?:改成|改为|变成|变为|调成|调为)"
        r"(?P<new>\d+|[一二两三四五六七八九十])件?",
        compact,
    )
    ordinal_match = re.search(
        r"(?:把)?第(?P<index>\d+|[一二两三四五六七八九十])(?:件|个|项)(?:商品)?"
        r"(?:改成|改为|变成|变为|调成|调为)"
        r"(?P<new>\d+|[一二两三四五六七八九十])件?",
        compact,
    )
    quantity_match = re.search(
        r"数量(?:改成|改为|变成|变为|调成|调为)"
        r"(?P<new>\d+|[一二两三四五六七八九十])件?",
        compact,
    )
    generic_target_match = re.search(
        r"(?:改成|改为|变成|变为|调成|调为)"
        r"(?P<new>\d+|[一二两三四五六七八九十])件",
        compact,
    )
    natural_purchase_match = re.search(
        r"(?:这件|这个|这款|该商品|该款|刚加的|刚加入的)?"
        r"(?:买|购入|数量为|数量是)"
        r"(?P<new>\d+|[一二两三四五六七八九十])(?:件|个)",
        compact,
    )
    relative_match = re.search(
        r"(?P<direction>再加|增加|加上|多|减少|减去|减|少)"
        r"(?P<delta>\d+|[一二两三四五六七八九十])件",
        compact,
    )
    if (
        absolute_match is None
        and ordinal_match is None
        and quantity_match is None
        and generic_target_match is None
        and natural_purchase_match is None
        and relative_match is None
    ):
        return None
    from_quantity = (
        _natural_quantity(absolute_match.group("old")) if absolute_match is not None else None
    )
    target_quantity_match = (
        absolute_match
        if absolute_match is not None
        else ordinal_match
        if ordinal_match is not None
        else quantity_match
        if quantity_match is not None
        else generic_target_match
        if generic_target_match is not None
        else natural_purchase_match
    )
    requested_quantity = (
        _natural_quantity(target_quantity_match.group("new"))
        if target_quantity_match is not None
        else None
    )
    ordinal_number = (
        _natural_quantity(ordinal_match.group("index")) if ordinal_match is not None else None
    )
    ordinal_index = ordinal_number - 1 if ordinal_number is not None else None
    delta = _natural_quantity(relative_match.group("delta")) if relative_match is not None else None
    if (
        absolute_match is not None
        or ordinal_match is not None
        or quantity_match is not None
        or generic_target_match is not None
    ) and (
        (absolute_match is not None and from_quantity is None)
        or requested_quantity is None
        or requested_quantity < 1
        or requested_quantity > 99
    ):
        return None
    if relative_match is not None and (delta is None or delta < 1 or delta > 99):
        return None

    groups = data.get("groups")
    candidates: list[tuple[str, Mapping[str, Any]]] = []
    eligible: list[tuple[str, Mapping[str, Any]]] = []
    for raw_group in groups if isinstance(groups, list) else []:
        if not isinstance(raw_group, Mapping):
            continue
        store_name = str(raw_group.get("store_name") or "")
        normalized_store = re.sub(r"\s+", "", store_name).casefold()
        store_aliases = {
            normalized_store,
            re.sub(r"(?:旗舰店|专卖店|店铺|商店|店)$", "", normalized_store),
        }
        store_referenced = any(
            alias and len(alias) >= 2 and alias in compact for alias in store_aliases
        )
        raw_items = raw_group.get("items")
        for raw_item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(raw_item, Mapping):
                continue
            if raw_item.get("is_selected") is not True or raw_item.get("is_valid") is not True:
                continue
            eligible.append((store_name, raw_item))
            current_quantity = int(raw_item.get("quantity") or 0)
            if from_quantity is not None and current_quantity != from_quantity:
                continue
            product_name = re.sub(r"\s+", "", str(raw_item.get("product_name") or "")).casefold()
            sku_name = re.sub(r"\s+", "", str(raw_item.get("sku_name") or "")).casefold()
            item_referenced = any(
                value and len(value) >= 2 and value in compact for value in (product_name, sku_name)
            )
            if store_referenced or item_referenced:
                candidates.append((store_name, raw_item))
    if ordinal_index is not None:
        candidates = [eligible[ordinal_index]] if 0 <= ordinal_index < len(eligible) else []
    elif not candidates:
        selected_item = _select_cart_item(data, user_text)
        if selected_item is not None:
            candidates = [
                (store_name, item)
                for store_name, item in eligible
                if item is selected_item
                or (
                    item.get("cart_item_id") == selected_item.get("cart_item_id")
                    and item.get("cart_item_id") is not None
                )
            ]
    elif not candidates and len(eligible) == 1:
        candidates = eligible
    if len(candidates) != 1:
        return None

    store_name, item = candidates[0]
    actual_quantity = int(item.get("quantity") or 0)
    to_quantity: int | None
    if relative_match is not None:
        assert delta is not None
        direction = relative_match.group("direction")
        signed_delta = -delta if direction in {"减少", "减去", "减", "少"} else delta
        to_quantity = actual_quantity + signed_delta
    else:
        to_quantity = requested_quantity
    if to_quantity is None or to_quantity < 1 or to_quantity > 99:
        return None
    price = item.get("current_price")
    amount_summary = data.get("amount_summary")
    selected_amount = (
        amount_summary.get("selected_goods_amount") if isinstance(amount_summary, Mapping) else None
    )
    if not isinstance(price, Mapping) or not isinstance(selected_amount, Mapping):
        return None
    try:
        unit_minor = int(price.get("minor_units") or 0)
        current_minor = int(selected_amount.get("minor_units") or 0)
    except (TypeError, ValueError):
        return None
    if unit_minor < 0 or current_minor < 0:
        return None
    projected_minor = current_minor + (to_quantity - actual_quantity) * unit_minor
    if projected_minor < 0:
        return None
    currency = str(price.get("currency") or selected_amount.get("currency") or "CNY")
    item_name = safe_untrusted_excerpt(item.get("product_name") or "目标商品", 80)
    sku_name = safe_untrusted_excerpt(item.get("sku_name") or "", 50)
    return {
        "store_name": safe_untrusted_excerpt(store_name or "店铺", 50),
        "item_label": f"{item_name} · {sku_name}" if sku_name else item_name,
        "from_quantity": actual_quantity,
        "to_quantity": to_quantity,
        "unit_price_display": _money_object_display(
            {"minor_units": str(unit_minor), "currency": currency}
        ),
        "current_total_display": _money_object_display(
            {"minor_units": str(current_minor), "currency": currency}
        ),
        "projected_total_display": _money_object_display(
            {"minor_units": str(projected_minor), "currency": currency}
        ),
    }


def _looks_like_cart_quantity_follow_up(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).casefold()
    return bool(re.search(r"(?:改成|改为|变成|调整为)\d+件", compact)) and any(
        marker in compact for marker in ("如果", "假设", "试算", "别修改", "不要修改")
    )


def _single_plan_with_non_overridable_guards(
    trigger_text: str,
    *,
    provider_plan: ExclusiveAgentPlan | None,
    fallback_plan: ExclusiveAgentPlan,
) -> tuple[ExclusiveAgentPlan, str]:
    """Resolve one task while keeping deterministic safety semantics authoritative."""

    if _looks_like_cart_quantity_follow_up(trigger_text):
        return (
            complete_exclusive_plan(
                ExclusiveAgentPlan(
                    "cart_lookup",
                    confidence=1.0,
                    continuation_of_previous_turn=True,
                )
            ),
            "deterministic_read_only_guard",
        )
    if _must_guard_read_only_order_eligibility(trigger_text, fallback_plan.intent):
        return fallback_plan, "deterministic_read_only_guard"
    if provider_plan is not None:
        return provider_plan, "provider_model_supervisor"
    return fallback_plan, "deterministic_safety_or_fallback"


def _wallet_cart_affordability_text(
    results: Mapping[str, Mapping[str, Any]], user_text: str
) -> str | None:
    normalized = re.sub(r"\s+", "", user_text).lower()
    if not (
        ("余额" in normalized or "钱" in normalized)
        and "购物车" in normalized
        and any(
            term in normalized for term in ("够不够", "够吗", "够买", "能不能买", "差额", "还差")
        )
    ):
        return None
    wallet = results.get("wallet_lookup")
    cart = results.get("cart_lookup")
    checkout = results.get("checkout_preview")
    if not isinstance(wallet, Mapping) or not (
        isinstance(cart, Mapping) or isinstance(checkout, Mapping)
    ):
        return None
    balance = wallet.get("balance")
    if isinstance(checkout, Mapping):
        checkout_amounts = checkout.get("amounts")
        selected_amount = (
            checkout_amounts.get("payable_amount")
            if isinstance(checkout_amounts, Mapping)
            else None
        )
    else:
        amount_summary = cart.get("amount_summary") if isinstance(cart, Mapping) else None
        selected_amount = (
            amount_summary.get("selected_goods_amount")
            if isinstance(amount_summary, Mapping)
            else None
        )
    if not isinstance(balance, Mapping) or not isinstance(selected_amount, Mapping):
        return None
    try:
        balance_minor = max(0, int(balance.get("minor_units") or 0))
        selected_minor = max(0, int(selected_amount.get("minor_units") or 0))
    except (TypeError, ValueError):
        return None
    currency = str(balance.get("currency") or selected_amount.get("currency") or "CNY")
    balance_display = _money_object_display(
        {"minor_units": str(balance_minor), "currency": currency}
    )
    selected_display = _money_object_display(
        {"minor_units": str(selected_minor), "currency": currency}
    )
    selected_quantity = (
        max(0, int(cart.get("selected_quantity") or 0)) if isinstance(cart, Mapping) else 1
    )
    if selected_quantity == 0 or selected_minor == 0:
        return (
            f"你当前余额为 {balance_display}，购物车暂时没有已选商品，所以无需补差额。"
            "余额和购物车实时结果已分别放在下方卡片中。"
        )
    difference = abs(balance_minor - selected_minor)
    difference_display = _money_object_display(
        {"minor_units": str(difference), "currency": currency}
    )
    amount_label = "本次结算应付" if isinstance(checkout, Mapping) else "购物车已选商品合计"
    card_label = "余额和结算预览" if isinstance(checkout, Mapping) else "余额和购物车实时结果"
    if balance_minor >= selected_minor:
        return (
            f"够。你当前余额为 {balance_display}，{amount_label} {selected_display}，"
            f"按当前价格购买后预计还剩 {difference_display}。"
            f"{card_label}已分别放在下方卡片中。最终金额以结算页为准。"
        )
    return (
        f"暂时不够。你当前余额为 {balance_display}，{amount_label} {selected_display}，"
        f"还差 {difference_display}。"
        f"{card_label}已分别放在下方卡片中。最终金额以结算页为准。"
    )


def _order_spend_summary_text(items: Sequence[object]) -> str | None:
    paid_minor = 0
    refunded_minor = 0
    counted = 0
    currency = "CNY"
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            continue
        amounts = raw_item.get("amounts")
        if not isinstance(amounts, Mapping):
            continue
        paid = amounts.get("paid")
        refunded = amounts.get("refunded")
        if not isinstance(paid, Mapping):
            continue
        try:
            paid_minor += max(0, int(paid.get("minor_units") or 0))
            if isinstance(refunded, Mapping):
                refunded_minor += max(0, int(refunded.get("minor_units") or 0))
            currency = str(paid.get("currency") or currency)
        except (TypeError, ValueError):
            continue
        counted += 1
    if counted == 0:
        return None
    paid_display = _money_object_display({"minor_units": str(paid_minor), "currency": currency})
    refunded_display = _money_object_display(
        {"minor_units": str(refunded_minor), "currency": currency}
    )
    net_display = _money_object_display(
        {"minor_units": str(max(0, paid_minor - refunded_minor)), "currency": currency}
    )
    if refunded_minor:
        return (
            f"共 {counted} 笔订单，累计实付 {paid_display}，已退款 {refunded_display}，"
            f"当前净支出 {net_display}。明细已放在订单卡片中。"
        )
    return (
        f"共 {counted} 笔订单，累计实付 {paid_display}，当前净支出 {net_display}。"
        "明细已放在订单卡片中。"
    )


def _natural_quantity(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    return {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }.get(value)


def _render(plan: ExclusiveAgentPlan, data: Mapping[str, Any], user_text: str = "") -> str:
    if plan.intent == "general_chat":
        normalized = re.sub(r"\s+", "", user_text).casefold()
        if any(term in normalized for term in ("谢谢", "辛苦了")):
            return "不客气，有商品、订单、物流、售后或平台规则方面的问题，随时告诉我。"
        if any(
            term in normalized
            for term in (
                "能做什么",
                "可以做什么",
                "能帮我",
                "可以帮我",
                "服务范围",
                "怎么用你",
            )
        ) or re.search(r"你.*(?:能|可以).*帮我.*(?:做|干).*什么", normalized):
            return (
                "我可以帮你搜索、筛选和比较全平台商品，查询你本人的订单、购物车、余额、"
                "地址、收藏、物流与售后进度，也能检查退款资格并准备待确认的申请草稿。"
                "平台规则会从已发布知识库核验。涉及退款提交等写操作会先展示确认卡片，付款、"
                "取消订单和确认收货仍由你本人在对应页面操作。你可以直接说一个目标，也可以"
                "一次交代多个任务。"
            )
        if "搜索框" in normalized and any(
            marker in normalized for marker in ("你和", "区别", "不同", "比搜索")
        ):
            return (
                "普通搜索框主要按关键词返回商品，我会理解连续对话里的条件和指代，调用受控工具"
                "查询实时商品、订单、购物车、物流或售后数据，并把结果整理成可点击卡片。涉及改"
                "购物车、退款草稿等任务时，我还能拆给对应专业 Agent 执行，但高风险操作仍会"
                "遵守确认与权限边界。"
            )
        return (
            "你好，我是你的专属客服。想找商品、查订单物流、处理售后，"
            "或者一次交代多个任务，都可以直接告诉我。"
        )
    items = data.get("items")
    if data.get("selection_required") == "refund":
        context_error = data.get("refund_context_error")
        if not isinstance(items, list) or not items:
            if context_error == "ORDER_NOT_REFUNDABLE":
                return (
                    "刚才关联的订单尚未付款或已取消，不能申请退款。"
                    "你的账号下也暂时没有其他可申请售后的订单。"
                )
            return "你的账号下暂时没有可申请退款的可见订单。"
        prefix = (
            "刚才关联的订单当前不能申请退款。我找到了其他可申请售后的订单，"
            if context_error
            else ""
        )
        return f"{prefix}请选择需要处理的一笔。点击订单卡片后，我会继续检查资格并准备退款申请。"
    if plan.intent in {"product_search", "personalized_recommendation"}:
        if not isinstance(items, list) or not items:
            focused_product_name = data.get("focused_product_name")
            unavailable_labels = data.get("unavailable_variant_labels")
            if isinstance(focused_product_name, str) and isinstance(unavailable_labels, list):
                labels = "、".join(
                    safe_untrusted_excerpt(value, 20)
                    for value in unavailable_labels
                    if isinstance(value, str)
                )
                return (
                    f"“{safe_untrusted_excerpt(focused_product_name, 100)}”当前在售款式中"
                    f"没有找到{labels or '你指定的'}款。商品卡片仍保留在下方，"
                    "你可以点击查看其他款式。"
                )
            applied_filters = data.get("applied_filters")
            if isinstance(applied_filters, Mapping) and isinstance(
                applied_filters.get("weight_jin"), int
            ):
                weight = int(applied_filters["weight_jin"])
                seasons = applied_filters.get("seasons")
                season_text = (
                    "、".join(
                        {
                            "spring": "春季",
                            "summer": "夏季",
                            "autumn": "秋季",
                            "winter": "冬季",
                        }.get(str(season), str(season))
                        for season in seasons
                    )
                    if isinstance(seasons, list) and seasons
                    else "不限季节"
                )
                price_max = applied_filters.get("price_max")
                budget_display = (
                    _money_object_display({"minor_units": str(price_max), "currency": "CNY"})
                    if isinstance(price_max, int)
                    else None
                )
                budget_text = f"、预算 {budget_display} 以内" if budget_display else ""
                season_clause = (
                    f"、符合{season_text}"
                    if isinstance(seasons, list) and seasons
                    else "、季节不限"
                )
                return (
                    f"当前没有找到商家款式明确标注可覆盖 {weight} 斤{season_clause}"
                    f"{budget_text}的在售商品。我没有把尺码不明或上限不足的商品推荐给你。"
                    + (
                        "可以再放宽品类、预算或季节，或者先让店铺客服按身高和围度确认。"
                        if isinstance(seasons, list) and seasons
                        else "可以再放宽品类或预算，或者先让店铺客服按身高和围度确认。"
                    )
                )
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
        if data.get("catalog_reselection") is True and len(items) == 1:
            item = items[0] if isinstance(items[0], Mapping) else {}
            name = safe_untrusted_excerpt(item.get("name") or "这件商品", 100)
            return f"已按你的更正切换到“{name}”。商品卡片已更新，可以继续问款式、库存或详情。"
        if _requests_catalog_then_cart_choice(user_text):
            if len(items) == 1:
                return (
                    f"{prefix}只找到 1 件匹配商品，我先放在下方让你核对，本次还没有改购物车。"
                    "确认后回复“把第一个加入购物车”并说明数量，我会沿用这张商品卡继续处理。"
                )
            return (
                f"{prefix}找到 {min(len(items), 5)} 件候选商品，并已按你的排序条件展示。"
                "为避免把错误商品或款式加入购物车，本次还没有修改购物车。"
                "请回复第几件、具体款式和数量，我会沿用这些商品卡继续处理。"
            )
        if data.get("catalog_focus") == "sku_availability" and len(items) == 1:
            item = items[0] if isinstance(items[0], Mapping) else {}
            name = safe_untrusted_excerpt(item.get("name") or "当前商品", 100)
            stock = max(0, int(item.get("available_stock") or 0))
            stock_extreme_answer = _sku_stock_extreme_answer(user_text, item)
            if stock_extreme_answer is not None:
                return stock_extreme_answer
            return (
                f"已查到“{name}”的在售款式与实时库存，共可售 {stock} 件。"
                "各款式已整理在下方，最终库存以结算页为准。"
            )
        if len(items) == 1:
            return (
                f"{prefix}按你给出的条件，全平台在售商品中目前只找到 1 件匹配结果，已放在卡片中。"
                "点击卡片可以查看详情。我没有用不相关商品凑数。你可以放宽一个条件，我再继续筛选。"
            )
        return (
            f"{prefix}为你找到 {min(len(items), 5)} 件全平台在售商品。"
            "点击卡片可以直接查看。价格和库存以商品详情与结算页的实时结果为准。"
        )
    if plan.intent == "product_compare":
        if not isinstance(items, list) or len(items) < 2:
            return "请先把至少两件想比较的商品发给我，或先让我搜索商品，再说“对比前两个”。"
        stock_conclusion = _product_stock_extreme_answer(user_text, items)
        if stock_conclusion is not None:
            return stock_conclusion
        conclusion = _comparison_purchase_conclusion(user_text, items)
        return conclusion or (
            "我把价格、在售款式、实时库存、销量和评分整理成了对比卡片。点击商品卡可继续查看详情。"
        )
    if plan.intent == "order_lookup":
        if isinstance(items, list) and len(items) == 1 and isinstance(items[0], Mapping):
            payment_explanation = _payment_method_explanation(items[0], user_text)
            if payment_explanation is not None:
                return payment_explanation
        eligibility_values = data.get("requested_eligibility_actions")
        eligibility_actions = (
            [str(value) for value in eligibility_values]
            if isinstance(eligibility_values, list)
            else []
        )
        if eligibility_actions:
            eligibility_labels = {
                "cancel_order": "取消",
                "confirm_receipt": "确认收货",
                "review": "评价",
                "apply_after_sale": "申请售后",
            }
            requested = "并".join(
                eligibility_labels.get(action, action) for action in eligibility_actions
            )
            eligible_values = data.get("eligibility_match_items")
            eligible_items = (
                [item for item in eligible_values if isinstance(item, Mapping)]
                if isinstance(eligible_values, list)
                else []
            )
            if len(eligibility_actions) > 1:
                action_counts = {
                    action: sum(
                        1
                        for item in eligible_items
                        if isinstance(item.get("available_actions"), list)
                        and action in item["available_actions"]
                    )
                    for action in eligibility_actions
                }
                summary = "、".join(
                    f"可{eligibility_labels.get(action, action)} {action_counts[action]} 笔"
                    for action in eligibility_actions
                )
                return (
                    f"已分别核对：{summary}。"
                    + (
                        "符合任一条件的订单已用卡片展示。"
                        if eligible_items
                        else "当前没有符合这些操作条件的订单。"
                    )
                    + "本次只做资格查询，没有执行任何操作。"
                )
            if _asks_order_status_and_eligibility(user_text) and isinstance(items, list):
                if not eligible_items:
                    return (
                        f"两笔订单的当前状态已分别放在卡片中；其中没有订单可以{requested}。"
                        "本次只做资格查询，没有执行任何操作。"
                    )
                selected_eligible_item = eligible_items[0]
                product_values = selected_eligible_item.get("items")
                product = (
                    product_values[0]
                    if isinstance(product_values, list)
                    and product_values
                    and isinstance(product_values[0], Mapping)
                    else {}
                )
                product_name = safe_untrusted_excerpt(product.get("product_name") or "这笔订单", 80)
                sku_name = safe_untrusted_excerpt(product.get("sku_name") or "", 60)
                description = f"“{product_name}{' · ' + sku_name if sku_name else ''}”"
                return (
                    f"{len(items)} 笔订单的当前状态已分别放在卡片中；其中 {description} "
                    f"可以{requested}，其余订单当前不具备该入口。"
                    "本次只做资格查询，没有执行任何操作。"
                )
            if not isinstance(items, list) or not items:
                return f"你当前没有可以{requested}的订单，本次没有执行任何操作。"
            return (
                f"你当前有 {len(items)} 笔订单可以{requested}，已只保留符合条件的订单卡片。"
                "本次只做资格查询，没有执行任何操作。"
            )
        if "order_id" in data:
            if "用户发送了订单卡片" in user_text:
                return (
                    "我已经看到这笔订单了。你想查付款、发货、物流、收货，"
                    "还是退款售后? 我会沿着这笔订单继续帮你处理。"
                )
            payment_explanation = _payment_method_explanation(data, user_text)
            if payment_explanation is not None:
                return payment_explanation
            action_explanation = _order_actions_explanation(data, user_text)
            if action_explanation is not None:
                return action_explanation
            receipt_explanation = _confirm_receipt_explanation(data, user_text)
            if receipt_explanation is not None:
                return receipt_explanation
            status = data.get("status")
            normalized_order_question = re.sub(r"\s+", "", user_text).casefold()
            if (
                isinstance(status, Mapping)
                and "售后" in normalized_order_question
                and any(
                    marker in normalized_order_question
                    for marker in ("有没有", "是否有", "进行中", "状态", "处理")
                )
            ):
                after_sale = str(status.get("after_sale") or "none")
                if after_sale in {"none", "closed", "cancelled", "succeeded", "rejected"}:
                    return (
                        "这笔订单当前没有进行中的售后。你仍可从订单卡片查看订单详情和现有可用操作。"
                    )
                return "这笔订单当前有进行中的售后，具体阶段请从订单卡片进入售后详情查看。"
            if (
                isinstance(status, Mapping)
                and status.get("order") == "pending_shipment"
                and any(
                    marker in normalized_order_question
                    for marker in ("没发货", "未发货", "什么时候发", "多久发", "预计发")
                )
            ):
                return (
                    "这笔订单当前已付款、待商家发货。系统暂时没有可核验的具体发货时间，"
                    "所以我不会编造日期，你可以从订单卡片进入详情并联系店铺确认。"
                )
            reference_index = order_reference_index(user_text)
            if _corrects_order_reference(user_text) and reference_index is not None:
                ordinal = ("第一", "第二", "第三", "第四", "第五")[reference_index]
                return f"已按你的纠正切换到{ordinal}笔订单，下方只保留对应卡片。"
            if (
                "上一轮" in normalized_order_question
                and "为什么" in normalized_order_question
                and len(order_reference_indices(user_text)) >= 2
                and reference_index is not None
            ):
                ordinal = ("第一", "第二", "第三", "第四", "第五")[reference_index]
                return (
                    f"上一轮把你作为对照提到的旧序号误识别成了目标；"
                    f"这次已按句子中最先明确的{ordinal}笔重新核对，下方只显示正确卡片。"
                )
            return "已找到这笔订单。点击卡片可查看详情或继续处理。"
        if not isinstance(items, list) or not items:
            empty_state_counts = data.get("requested_state_counts")
            if isinstance(empty_state_counts, Mapping) and empty_state_counts:
                requested_labels = [str(label) for label in empty_state_counts]
                return f"你当前没有{'、'.join(requested_labels)}订单，所以没有对应卡片。"
            return "你的账号下暂未查询到可见订单。"
        if requests_direct_transaction_action(user_text):
            return (
                "为了避免误操作，我不能代你付款、取消订单或确认收货。"
                "请从下方订单卡片进入详情页自行核对并操作。"
            )
        normalized_order_question = re.sub(r"\s+", "", user_text).casefold()
        if _asks_order_spend_summary(user_text):
            spend_summary = _order_spend_summary_text(items)
            if spend_summary is not None:
                return spend_summary
        if _requests_order_state_overview(user_text):
            requested_counts = data.get("requested_state_counts")
            if isinstance(requested_counts, Mapping) and requested_counts:
                overview = "、".join(
                    f"{safe_untrusted_excerpt(label, 16)} {int(count or 0)} 笔"
                    for label, count in requested_counts.items()
                )
                missing = [
                    str(label) for label, count in requested_counts.items() if int(count or 0) == 0
                ]
                answer = (
                    f"已按你指定的状态整理，共展示 {len(items)} 张订单卡片。状态统计：{overview}。"
                )
                if missing:
                    answer += f" 当前没有{'、'.join(missing)}订单，因此这些状态没有卡片。"
                return answer
        if any(marker in normalized_order_question for marker in ("发货", "签收", "收货", "评价")):
            status_labels = {
                "pending_payment": "待付款",
                "pending_shipment": "待发货",
                "shipped": "运输中",
                "completed": "已完成",
                "cancelled": "已取消",
            }
            counts: dict[str, int] = {}
            reviewable = 0
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                status_value = item.get("status")
                status = status_value if isinstance(status_value, Mapping) else {}
                label = status_labels.get(str(status.get("order")), "状态更新中")
                counts[label] = counts.get(label, 0) + 1
                actions = item.get("available_actions")
                if isinstance(actions, list) and "review" in actions:
                    reviewable += 1
            overview = "、".join(f"{label} {count} 笔" for label, count in counts.items())
            answer = f"找到 {len(items)} 笔相关订单, {overview}。具体商品和状态已放在卡片中。"
            if "评价" in normalized_order_question:
                answer += (
                    f"其中 {reviewable} 笔当前可以评价，可从订单卡片进入。"
                    if reviewable
                    else (
                        "这些订单目前都没有可评价入口。签收后还需确认收货, "
                        "进入待评价状态后才能评价。"
                    )
                )
            return answer
        scope = "订单" if _requests_order_state_overview(user_text) else "最近订单"
        answer = f"找到你的 {len(items)} 笔{scope}。点击卡片可查看详情或继续处理。"
        if _requests_compound_advice(user_text):
            pending_count = 0
            shipped_count = 0
            after_sale_count = 0
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                status = item.get("status")
                if not isinstance(status, Mapping):
                    continue
                pending_count += int(status.get("order") == "pending_shipment")
                shipped_count += int(status.get("order") == "shipped")
                after_sale_count += int(status.get("after_sale") not in {None, "none"})
            if pending_count or shipped_count or after_sale_count:
                status_parts = []
                if pending_count:
                    status_parts.append(f"{pending_count} 笔待发货")
                if shipped_count:
                    status_parts.append(f"{shipped_count} 笔运输中")
                if after_sale_count:
                    status_parts.append(f"{after_sale_count} 笔售后中")
                answer = (
                    f"建议先花一分钟看订单，你最近的记录里有{'、'.join(status_parts)}。"
                    "确认发货、物流和售后都没有异常后，再继续逛商品会更安心。"
                    "相关订单已放在下方卡片中。"
                )
        state_counts = data.get("requested_state_counts")
        if isinstance(state_counts, Mapping):
            if _requests_order_state_overview(user_text):
                overview = "、".join(
                    f"{safe_untrusted_excerpt(label, 16)} {int(count or 0)} 笔"
                    for label, count in state_counts.items()
                )
                if overview:
                    answer += f" 状态统计: {overview}。"
            missing = [str(label) for label, count in state_counts.items() if int(count or 0) == 0]
            if missing:
                answer += f" 当前没有{'、'.join(missing)}订单，所以没有对应卡片。"
        return answer
    if plan.intent in {"cart_lookup", "cart_add", "cart_update", "cart_remove", "cart_clear"}:
        if data.get("cart_action_clarification") is True:
            return (
                "我还不能唯一确定你要操作购物车里的哪件商品，因此没有修改。"
                "请说商品名称、款式，或先打开下方购物车卡片核对。"
            )
        if plan.intent == "cart_lookup":
            verification = _cart_add_verification_text(data, user_text)
            if verification is not None:
                return verification
        if plan.intent == "cart_add":
            unavailable_variant = data.get("cart_add_unavailable_variant")
            if isinstance(unavailable_variant, Mapping):
                product_name = safe_untrusted_excerpt(
                    unavailable_variant.get("product_name") or "当前商品", 100
                )
                sku_name = safe_untrusted_excerpt(
                    unavailable_variant.get("sku_name") or "所选款式", 80
                )
                return (
                    f"“{product_name}”的“{sku_name}”当前可售库存为 0，"
                    "所以本次没有加入购物车。其他在售款式已放在下方供你重新选择。"
                )
            return (
                f"已将“{safe_untrusted_excerpt(data.get('added_product_name') or '所选商品', 100)}”"
                f"的“{safe_untrusted_excerpt(data.get('added_sku_name') or '当前款式', 80)}”"
                f"加入购物车，共 {max(1, int(data.get('added_quantity') or 1))} 件。"
                "更新后的购物车已放在下方卡片中。"
            )
        if plan.intent == "cart_update":
            product_name = safe_untrusted_excerpt(
                data.get("updated_product_name") or "所选商品", 100
            )
            sku_name = safe_untrusted_excerpt(data.get("updated_sku_name") or "当前款式", 80)
            return (
                f"已将“{product_name}”的“{sku_name}”"
                f"数量改为 {max(1, int(data.get('updated_quantity') or 1))} 件。"
                "更新后的购物车已放在下方卡片中。"
            )
        if plan.intent == "cart_remove":
            product_name = safe_untrusted_excerpt(
                data.get("removed_product_name") or "所选商品", 100
            )
            sku_name = safe_untrusted_excerpt(data.get("removed_sku_name") or "当前款式", 80)
            return f"已从购物车移除“{product_name}”的“{sku_name}”。更新后的购物车已放在下方卡片中。"
        hypothetical = data.get("cart_hypothetical")
        if isinstance(hypothetical, Mapping):
            return (
                "按当前购物车价格试算: "
                f"{hypothetical.get('item_label', '目标商品')}从 "
                f"{hypothetical.get('from_quantity', 0)} 件改为 "
                f"{hypothetical.get('to_quantity', 0)} 件后，预计已选商品总额从 "
                f"{hypothetical.get('current_total_display', '¥0.00')} 变为 "
                f"{hypothetical.get('projected_total_display', '¥0.00')}。"
                "这里只进行了试算，没有修改购物车。"
            )
        quantity = data.get("cart_total_quantity")
        if not isinstance(quantity, int) or quantity <= 0:
            return "你的购物车还是空的。可以先去逛逛，遇到喜欢的商品再加入购物车。"
        selected = data.get("selected_quantity")
        return (
            f"购物车里共有 {quantity} 件商品"
            + (f"，已选 {selected} 件" if isinstance(selected, int) else "")
            + "。我已整理在卡片里，点击即可检查商品并结算。"
        )
    if plan.intent == "checkout_preview":
        unavailable = data.get("checkout_unavailable")
        if unavailable == "NO_VALID_SELECTED_CART_ITEMS":
            return (
                "购物车中没有已选且可结算的商品，因此没有创建结算预览。请先在购物车选择有效商品。"
            )
        blocking = data.get("blocking_issues")
        if isinstance(blocking, list) and blocking:
            messages = [
                safe_untrusted_excerpt(item.get("message") or "结算条件未满足", 120)
                for item in blocking
                if isinstance(item, Mapping)
            ]
            return "结算预览已生成，但暂时不能提交订单: " + "、".join(messages[:3])
        return (
            "结算预览已生成并放在下方卡片中。当前还没有创建订单，也没有付款。"
            "请核对后再决定是否继续。"
        )
    if plan.intent == "address_lookup":
        profile_value = data.get("profile")
        profile = profile_value if isinstance(profile_value, Mapping) else None
        profile_text = ""
        if profile is not None:
            username = safe_untrusted_excerpt(profile.get("username") or "未设置", 64)
            email = safe_untrusted_excerpt(profile.get("email") or "未设置", 120)
            profile_text = f"你的用户名是“{username}”，当前邮箱是“{email}”。"
        if not isinstance(items, list) or not items:
            return profile_text or "你还没有保存收货地址。可以点击下方入口新增地址。"
        default_count = sum(
            1 for item in items if isinstance(item, Mapping) and item.get("is_default") is True
        )
        answer = profile_text + (
            f"已找到你的 {len(items)} 个收货地址"
            + ("，默认地址已排在最前面" if default_count else "")
            + "。你可以直接查看，或进入地址管理进行修改。"
        )
        if _requests_address_mutation(user_text):
            return "我目前不能在聊天中直接新增、修改或删除收货地址，本次没有改动任何地址。" + answer
        if _asks_address_postcode(user_text):
            has_postcode = any(
                isinstance(item, Mapping) and bool(item.get("postal_code") or item.get("postcode"))
                for item in items
            )
            if not has_postcode:
                return (
                    "当前收货地址记录中没有保存邮编，因此我不能给出一个猜测值。"
                    "地址卡片已放在下方。如确实需要邮编，请向当地邮政渠道核实。"
                )
        return answer
    if plan.intent == "wallet_lookup":
        balance = data.get("balance")
        amount = _money_object_display(balance) if isinstance(balance, Mapping) else "¥0.00"
        transactions = data.get("transactions")
        asks_transactions = any(
            marker in re.sub(r"\s+", "", user_text).casefold()
            for marker in ("余额变动", "余额流水", "资金流水", "交易记录", "最近几笔")
        )
        if asks_transactions:
            count = min(
                max(1, int(data.get("transaction_limit") or 3)),
                len(transactions) if isinstance(transactions, list) else 0,
            )
            return f"你当前的可用余额是 {amount}。" + (
                f"最近 {count} 笔余额变动也已按时间倒序放在卡片中。"
                if count
                else "当前还没有可见的余额变动记录。"
            )
        return f"你当前的可用余额是 {amount}。余额详情已放在下方卡片中。"
    if plan.intent in {"favorites_lookup", "favorite_update"}:
        absent_store = data.get("favorite_store_already_absent")
        if isinstance(absent_store, str):
            return (
                f"你的店铺收藏中当前没有“{safe_untrusted_excerpt(absent_store, 100)}”，"
                "因此本次没有执行删除，其他收藏也没有变化。"
            )
        updated = data.get("favorite_updated")
        if isinstance(updated, Mapping):
            if updated.get("target_type") == "store":
                store_name = safe_untrusted_excerpt(updated.get("store_name") or "所选店铺", 100)
                action = "已收藏" if updated.get("enabled") is True else "已取消收藏"
                return f"{action}店铺“{store_name}”。更新后的收藏已放在下方卡片中。"
            product_name = safe_untrusted_excerpt(updated.get("product_name") or "所选商品", 100)
            action = "已收藏" if updated.get("enabled") is True else "已取消收藏"
            return f"{action}“{product_name}”。更新后的收藏已放在下方卡片中。"
        if data.get("favorite_action_clarification") is True:
            if data.get("favorite_target_type") == "store":
                return (
                    "我还不能唯一确定要修改哪家店铺的收藏，因此没有执行。"
                    "请先展示相关商品或店铺，再说收藏这家店。"
                )
            return (
                "我还不能唯一确定要修改哪件商品的收藏，因此没有执行。请先展示收藏商品，再说第几个。"
            )
        product_count = int(data.get("favorite_product_count") or 0)
        store_count = int(data.get("followed_store_count") or 0)
        if product_count == 0 and store_count == 0:
            return "你当前还没有收藏商品或店铺。"
        if plan.intent == "favorites_lookup" and _requests_favorite_mutation(user_text):
            return (
                f"你当前收藏了 {product_count} 件商品、{store_count} 家店铺。"
                "你要求现在不要执行，因此本次没有修改任何数据。"
            )
        return (
            f"你收藏了 {product_count} 件商品、{store_count} 家店铺。商品和店铺入口已整理在下方。"
        )
    if plan.intent == "review_draft":
        order_values = data.get("items")
        if not isinstance(order_values, list) or not order_values:
            return "当前没有可评价的订单，因此没有生成评价草稿。"
        draft = safe_untrusted_excerpt(data.get("review_draft") or "", 300)
        return (
            f"我已为最近一笔待评价订单整理草稿“{draft}”。"
            "本次没有提交，也没有替你设置星级。请打开卡片核对后再决定是否发布。"
        )
    if plan.intent == "memory_lookup":
        memory = data.get("memory")
        if not isinstance(memory, Mapping) or memory.get("authorized") is not True:
            return "你还没有开启个性化记忆授权，因此我没有读取长期购物偏好。"
        values = data.get("recalled_memories")
        if not isinstance(values, list) or not values:
            return "你已经开启个性化记忆，但目前没有仍然有效、且由你确认保存的购物偏好。"
        answer = f"我记得你确认保存的 {len(values)} 条购物偏好，已放在下方卡片中。"
        if any(marker in user_text for marker in ("删除", "移除", "忘掉", "不再记住")):
            answer += "我不会仅凭聊天文字删除记忆。请点击“管理记忆”进入列表，核对后删除对应偏好。"
        if any(marker in user_text for marker in ("哪里来的", "哪来的", "来源", "怎么记住")):
            answer += (
                "这些内容只来自你在本商城明确授权并确认保存的长期记忆，"
                "不会从订单、地址或其他用户数据中自行推断。"
            )
        return answer
    if plan.intent == "logistics_lookup":
        if not isinstance(items, list) or not items:
            return "该订单当前没有可见物流包裹。"
        if _asks_order_logistics_status_difference(user_text):
            status_value = data.get("order_status_detail")
            status = status_value if isinstance(status_value, Mapping) else {}
            delivered = _has_signed_shipment(data)
            if delivered and status.get("fulfillment") in {"shipped", "partial"}:
                return (
                    "两处状态描述的是不同阶段\uff1a物流“已签收”表示包裹的实体运输节点已经完成\uff1b"
                    "订单卡片“运输中”表示交易履约还没有完成确认收货。当前这笔应以物流卡片"
                    "判断包裹已签收，以订单卡片判断订单尚待确认收货。点击下方卡片可以分别"
                    "查看订单和完整轨迹。"
                )
        if len(items) == 1 and isinstance(items[0], Mapping):
            item = items[0]
            track_value = item.get("last_track")
            track = track_value if isinstance(track_value, Mapping) else {}
            state = _status_label("shipment", item.get("shipment_status"))
            location = safe_untrusted_excerpt(track.get("location_text"), 100)
            description = safe_untrusted_excerpt(track.get("description"), 160)
            location_text = f"，当前位置是 {location}" if location else ""
            detail_text = f": {description}" if description and description != state else ""
            answer = (
                f"包裹最新状态为“{state}”{location_text}{detail_text}。点击卡片可以查看完整物流。"
            )
        else:
            answer = f"已更新 {len(items)} 个物流包裹的最新进度。点击卡片可以查看完整物流。"
        normalized_question = re.sub(r"\s+", "", user_text).casefold()
        if (
            len(items) == 1
            and isinstance(items[0], Mapping)
            and any(
                marker in normalized_question
                for marker in ("为什么", "为何", "原因", "怎么还")
            )
            and items[0].get("shipment_status") in {"picked_up", "in_transit"}
        ):
            answer += (
                " 当前没有查询到更晚的物流节点，所以仍如实显示这一状态；"
                "这本身不能证明包裹已经丢失。"
            )
        delivery_comparison = _delivery_comparison_text(data, user_text)
        if delivery_comparison is not None:
            answer += " " + delivery_comparison
        elif any(
            marker in normalized_question
            for marker in (
                "预计什么时候到",
                "预计什么时候能到",
                "预计多久到",
                "预计送达",
                "什么时候到",
                "什么时候能到",
                "几天到",
                "多久到",
            )
        ):
            estimates = [
                _delivery_estimate_text(item) for item in items if isinstance(item, Mapping)
            ]
            available_estimates = [
                value.removeprefix("; ") for value in estimates if "暂无可靠" not in value
            ]
            answer += (
                " " + "; ".join(available_estimates) + "。"
                if available_estimates
                else " 当前没有承运商或配送模板给出的可靠预计送达时间，我不会编造日期。"
            )
        combined = data.get("combined_refund_precheck")
        if isinstance(combined, Mapping):
            eligibility_value = combined.get("refund_eligibility")
            combined_eligibility = (
                eligibility_value if isinstance(eligibility_value, Mapping) else {}
            )
            shipment_state = "该包裹已签收" if _has_signed_shipment(data) else "该包裹当前尚未签收"
            answer += (
                f" {shipment_state}。售后资格也已检查, 当前可以申请, 本次没有创建或提交申请。"
                if combined_eligibility.get("eligible") is True
                else f" {shipment_state}。当前暂不能申请售后, 原因已放在资格卡片中。"
            )
        elif data.get("conditional_refund_precheck_skipped") is True:
            answer += " 当前物流尚未签收，所以没有执行你设定的后续售后资格检查。"
        payment_detail = data.get("order_payment_detail")
        if isinstance(payment_detail, Mapping):
            payment_answer = _payment_method_explanation(payment_detail, user_text)
            if payment_answer is not None:
                answer += " " + payment_answer
        return answer
    if plan.intent == "refund_precheck":
        eligibility_value = data.get("refund_eligibility")
        eligibility: Mapping[str, Any] = (
            eligibility_value if isinstance(eligibility_value, Mapping) else {}
        )
        refund_eligible = eligibility.get("eligible") is True
        combined_explanation = _refund_amount_and_receipt_explanation(
            data,
            eligibility,
            user_text,
        )
        if combined_explanation is not None:
            return combined_explanation
        lines = [
            (
                "资格检查完成: 当前可以申请售后。金额、类型和下一步入口已整理在卡片中。"
                if refund_eligible
                else "资格检查完成: 当前暂不能申请售后，具体原因已整理在卡片中。"
            )
        ]
        lines.append("本次只完成资格检查，没有创建退款草稿或售后单。")
        return "\n".join(lines)
    if plan.intent == "refund_progress":
        if "refund_id" in data:
            return "已找到这笔售后申请。点击卡片可以查看当前节点和处理记录。"
        if not isinstance(items, list) or not items:
            requested_amounts = data.get("requested_refund_amounts")
            if isinstance(requested_amounts, list) and requested_amounts:
                labels = "、".join(f"¥{int(value) / 100:.2f}" for value in requested_amounts)
                return (
                    f"没有找到申请金额为 {labels} 的售后记录。"
                    "我没有把其他金额的售后单当成这笔。你可以先查看对应订单，或让我检查该订单的售后资格。"
                )
            return "你的账号下暂未查询到售后申请。"
        if len(items) == 1:
            item = items[0]
            if isinstance(item, Mapping):
                status_label = _status_label("refund", item.get("refund_status"))
                normalized_refund_question = re.sub(r"\s+", "", user_text).casefold()
                if any(
                    marker in normalized_refund_question
                    for marker in ("到哪一步", "商家", "处理结果", "审核结果")
                ):
                    terminal = str(item.get("refund_status") or "") in {
                        "succeeded",
                        "rejected",
                        "cancelled",
                        "closed",
                    }
                    return f"这笔售后当前处于“{status_label}”。" + (
                        "已经形成处理结果，点击下方售后卡片可查看完整记录。"
                        if terminal
                        else (
                            "目前还没有最终处理结果，请等待商家处理。"
                            "点击下方售后卡片可查看最新节点。"
                        )
                    )
                if any(
                    marker in normalized_refund_question
                    for marker in ("为什么", "怎么还", "还没有", "没到账", "未到账")
                ):
                    return (
                        f"这笔售后当前处于“{status_label}”，还没有进入退款到账阶段。"
                        "你现在无需重复申请，可以点击下方售后卡片查看处理记录并等待当前节点完成。"
                    )
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
        policy_query = safe_untrusted_excerpt(data.get("policy_query") or user_text, 500)
        relevance_query = safe_untrusted_excerpt(
            data.get("policy_retrieval_query") or policy_query, 500
        )
        answer = concise_policy_answer(
            relevance_query,
            sources,
            intro="根据当前已发布平台规则",
        )
        if "承诺" in user_text and "不承诺" in answer:
            return "不承诺。" + answer
        return answer
    return "已完成查询。"


def _payment_method_explanation(data: Mapping[str, Any], user_text: str) -> str | None:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    if not (
        any(marker in normalized for marker in ("支付", "付款"))
        and any(
            marker in normalized
            for marker in ("方式", "渠道", "怎么付", "如何付", "用余额", "实际扣")
        )
    ):
        return None
    payment_value = data.get("payment")
    if not isinstance(payment_value, Mapping):
        return "这笔订单暂时没有可核验的支付记录，我不会根据订单金额猜测支付方式。"
    method = str(payment_value.get("method") or "")
    method_label = {
        "wallet_balance": "商城余额",
        "fake_balance": "商城模拟支付",
    }.get(method, "其他支付方式")
    amount = payment_value.get("paid_amount")
    amount_text = _money_object_display(amount) if isinstance(amount, Mapping) else "金额待核验"
    return f"最近这笔订单使用“{method_label}”支付，实际支付 {amount_text}。订单卡片已放在下方。"


def _order_actions_explanation(data: Mapping[str, Any], user_text: str) -> str | None:
    """Answer multi-action eligibility questions without dropping requested actions."""

    normalized = re.sub(r"\s+", "", user_text).casefold()
    requested = [
        ("取消订单", "cancel_order", ("取消",)),
        ("确认收货", "confirm_receipt", ("确认收货",)),
        ("评价", "review", ("评价", "评论")),
        ("申请售后", "apply_after_sale", ("申请售后", "退款", "退货")),
    ]
    selected = [
        (label, action)
        for label, action, markers in requested
        if any(marker in normalized for marker in markers)
    ]
    if len(selected) < 2:
        return None
    values = data.get("available_actions")
    available = {str(value) for value in values} if isinstance(values, list) else set()
    status_value = data.get("status")
    status = status_value if isinstance(status_value, Mapping) else {}
    order_label = _status_label("order", status.get("order"))
    conclusions = "、".join(
        f"{label}{'可以' if action in available else '暂不可以'}" for label, action in selected
    )
    return (
        f"这笔订单当前为“{order_label}”。{conclusions}。"
        "可用入口以订单卡片进入详情后的实时按钮为准。本次只检查，没有执行任何操作。"
    )


def _confirm_receipt_explanation(data: Mapping[str, Any], user_text: str) -> str | None:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    if "确认收货" not in normalized:
        return None
    actions = data.get("available_actions")
    available = actions if isinstance(actions, list) else []
    status_value = data.get("status")
    status = status_value if isinstance(status_value, Mapping) else {}
    order_label = _status_label("order", status.get("order"))
    fulfillment_label = _status_label("fulfillment", status.get("fulfillment"))
    if "confirm_receipt" in available:
        return (
            f"可以。这笔订单目前为“{order_label}”，履约状态为“{fulfillment_label}”，"
            "订单详情页已经提供“确认收货”按钮。请先核对商品确实收到且无异常后再自行确认; "
            "本次只做状态说明，没有替你操作。"
        )
    if status.get("order") == "completed" or status.get("fulfillment") == "received":
        return (
            f"不能再次确认。这笔订单已经是“{order_label}”，履约状态为“{fulfillment_label}”，"
            "说明收货环节已经完成，所以页面不会再提供确认收货操作。本次没有修改订单。"
        )
    return (
        f"现在还不能确认。这笔订单目前为“{order_label}”，履约状态为“{fulfillment_label}”，"
        "只有订单进入可确认收货阶段时，详情页才会显示对应按钮。本次只做状态说明，没有操作。"
    )


def _refund_amount_and_receipt_explanation(
    data: Mapping[str, Any],
    eligibility: Mapping[str, Any],
    user_text: str,
) -> str | None:
    """Answer amount-basis and receipt-prerequisite follow-ups from live data."""

    normalized = re.sub(r"\s+", "", user_text).casefold()
    if not (
        any(
            marker in normalized
            for marker in (
                "为什么最多",
                "最多是",
                "最多只能退",
                "最多能退",
                "申请上限",
                "可退上限",
            )
        )
        and any(marker in normalized for marker in ("确认收货", "运输中", "创建申请", "提交申请"))
    ):
        return None
    suggested = eligibility.get("suggested_refund_amount")
    amount = _money_object_display(suggested) if isinstance(suggested, Mapping) else None
    status_value = data.get("status")
    status = status_value if isinstance(status_value, Mapping) else {}
    fulfillment_label = _status_label("fulfillment", status.get("fulfillment"))
    eligible = eligibility.get("eligible") is True
    amount_sentence = (
        f"建议申请金额上限是 {amount}，因为它按这笔订单当前仍可申请售后的商品实付金额计算; "
        if amount is not None
        else "申请金额上限按这笔订单当前仍可申请售后的商品实付金额计算; "
    )
    if eligible:
        receipt_sentence = (
            f"当前履约状态为“{fulfillment_label}”，资格预检已经显示可申请，"
            "不需要为了申请售后而先确认收货。只有实际收到商品且核对无异常时，才应自行确认收货。"
        )
    else:
        receipt_sentence = (
            f"当前履约状态为“{fulfillment_label}”，资格预检暂未通过，"
            "是否确认收货应以是否真实收到商品为准，不能把确认收货当作绕过售后限制的步骤。"
        )
    return (
        amount_sentence
        + receipt_sentence
        + "最终金额以售后提交页核对结果为准。本次只做解释和资格检查，没有创建或提交申请。"
    )


def _asks_order_logistics_status_difference(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    compares = any(marker in compact for marker in ("为什么", "不一致", "矛盾", "以哪个为准"))
    return (
        compares
        and "订单" in compact
        and any(marker in compact for marker in ("物流", "签收", "运输中"))
    )


def _comparison_purchase_conclusion(user_text: str, items: list[object]) -> str | None:
    """Give a bounded recommendation only when public evidence separates options."""

    normalized = re.sub(r"\s+", "", user_text).casefold()
    weight_match = re.search(r"(?P<weight>\d{2,3})斤", normalized)
    if weight_match is not None:
        requested_weight = int(weight_match.group("weight"))
        coverage: list[tuple[str, int | None]] = []
        for item in items[:3]:
            if not isinstance(item, Mapping):
                continue
            maximum: int | None = None
            sku_values = item.get("skus")
            for sku in sku_values if isinstance(sku_values, list) else []:
                if not isinstance(sku, Mapping):
                    continue
                limits = [
                    int(value)
                    for value in re.findall(
                        r"(\d{2,3})斤(?:以下|以内)?", str(sku.get("sku_name") or "")
                    )
                ]
                if limits:
                    maximum = max(maximum or 0, *limits)
            coverage.append((safe_untrusted_excerpt(item.get("name") or "商品", 100), maximum))
        eligible = [name for name, maximum in coverage if maximum and maximum >= requested_weight]
        if eligible:
            return (
                f"按商家公开款式标注，{'、'.join(eligible)}包含覆盖 {requested_weight} 斤的款式。"
                "请再结合商品详情中的身高、围度或版型信息核对。仅凭体重不能保证合身。"
            )
        known_limits = [maximum for _name, maximum in coverage if maximum is not None]
        if known_limits:
            return (
                f"按商家公开款式标注，这两件最高只覆盖到 {max(known_limits)} 斤以下，"
                f"没有可核验的款式覆盖 {requested_weight} 斤，因此我不建议直接下单。"
                "可以放宽商品范围后，我再帮你找合适款式。"
            )
        return (
            f"这两件商品没有提供可核验的体重尺码表，我无法判断是否适合 {requested_weight} 斤。"
            "建议打开商品卡查看详细尺码，或让店铺客服按围度进一步确认。"
        )
    price_conclusion: str | None = None
    if any(term in normalized for term in ("便宜", "价格", "贵", "划算")):
        priced: list[tuple[int, str]] = []
        for item in items[:3]:
            if not isinstance(item, Mapping):
                continue
            price = item.get("price")
            if not isinstance(price, Mapping):
                continue
            try:
                minor_units = max(0, int(price.get("min_amount") or 0))
            except (TypeError, ValueError):
                continue
            priced.append((minor_units, safe_untrusted_excerpt(item.get("name") or "商品", 100)))
        if len(priced) >= 2:
            cheapest = min(priced, key=lambda value: value[0])
            next_price = sorted(value[0] for value in priced)[1]
            if cheapest[0] < next_price:
                difference = _money_object_display(
                    {"minor_units": str(next_price - cheapest[0]), "currency": "CNY"}
                )
                price_conclusion = (
                    f"按当前最低在售价，“{cheapest[1]}”更便宜，比另一件低 {difference}。"
                    "各自款式与实时库存已放在下方对比卡片中。"
                )
            else:
                price_conclusion = (
                    "两件商品当前最低在售价相同，款式与实时库存已放在下方对比卡片中。"
                )
    if "考试" not in normalized:
        return price_conclusion
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
        return price_conclusion
    winner = max(ranked, key=lambda value: value[0])[1]
    recommendation = (
        f"按商家公开信息，“{winner}”更贴近考试使用场景。"
        "价格、款式和实时库存已放在下方对比卡片中，能否携带仍以具体考试规定为准。"
    )
    return (price_conclusion + recommendation) if price_conclusion else recommendation


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
    *,
    query_override: str | None = None,
) -> None:
    if intent != "policy_qa":
        return
    context.run.current_phase = "retrieving"
    context.run.version += 1
    policy_query = _strip_negated_intent_phrases(
        query_override if query_override is not None else context.trigger.text_content or "平台规则"
    )
    retrieval_query = policy_query
    compact_policy_query = re.sub(r"\s+", "", policy_query).casefold()
    if "提现" in compact_policy_query or (
        "退款" in compact_policy_query
        and any(
            marker in compact_policy_query
            for marker in ("原支付渠道", "原路", "退到余额", "退到哪里")
        )
    ):
        retrieval_query = (
            f"{policy_query} 模拟余额不支持提现或转账 余额支付订单退款成功后原路退回用户余额"
        )
    elif any(
        marker in compact_policy_query
        for marker in (
            "每5秒",
            "每五秒",
            "每隔5秒",
            "每隔五秒",
            "自动推进",
            "自动更新",
        )
    ) and any(marker in compact_policy_query for marker in ("物流", "快递", "包裹")):
        # Retrieval query expansion: preserve the user's question for display,
        # but add the exact concepts used by the published logistics policy so
        # hybrid search retrieves the rule instead of a generic logistics chunk.
        retrieval_query = (
            f"{policy_query} 模拟物流 不会根据经过时间自动推进 "
            "不承诺固定几秒内更新 店铺人员 平台管理员 经确认的 Agent 显式记录物流节点"
        )
    elif any(
        marker in compact_policy_query
        for marker in (
            "长时间未收到",
            "长时间没收到",
            "一直没收到",
            "没有新轨迹",
            "没新轨迹",
            "物流没更新",
            "快递没更新",
            "丢件",
            "包裹异常",
        )
    ):
        retrieval_query = (
            f"{policy_query} 包裹长时间停留 已揽收 运输中 没有更晚节点不等于丢件 "
            "核对承运商 运单号 最后更新时间 联系店铺 平台人工客服 售后资格"
        )
    elif "自动确认收货" in compact_policy_query:
        retrieval_query = f"{policy_query} 自动确认收货 物流签收后第七天 用户确认收货 订单完成"
    elif any(marker in compact_policy_query for marker in ("暂停营业", "店铺暂停")):
        retrieval_query = (
            f"{policy_query} 店铺暂停营业 购物车记录保留 标记不可购买 "
            "暂停期间不能创建结算或新订单 恢复营业后重新校验"
        )
    try:
        result = await KnowledgeService(mysql, checkpoint_store.session).search_for_agent(
            query=retrieval_query,
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
    raw_sources = [
        {
            "document_id": item.document_id,
            "title": item.title,
            "version": item.content_version,
            "excerpt": safe_untrusted_excerpt(item.excerpt, 1_200),
            "score": round(item.score, 6),
        }
        for item in result.items
    ]
    compacted_sources = _compact_policy_sources(
        raw_sources,
        retrieval_query,
        limit=4,
    )
    data["knowledge_sources"] = compacted_sources
    data["policy_query"] = policy_query
    data["policy_retrieval_query"] = retrieval_query
    data["rag"] = {
        "scope": "platform:platform",
        "returned_count": len(result.items),
        "used_count": len(compacted_sources),
        "degraded": result.degraded,
        "retrieval_mode": "keyword_only" if result.degraded else "hybrid",
    }


def _compact_policy_sources(
    sources: list[dict[str, object]],
    query: str,
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Carry only the best grounded evidence across the specialist boundary."""

    normalized = re.sub(r"\s+", "", query).casefold()
    domain_terms: tuple[str, ...] = ()
    if any(
        marker in normalized
        for marker in (
            "物流",
            "快递",
            "包裹",
            "发货",
            "签收",
            "未收到",
            "没收到",
            "新轨迹",
            "丢件",
        )
    ):
        domain_terms = ("物流", "快递", "包裹", "运单", "签收", "轨迹", "丢件")
    elif any(marker in normalized for marker in ("退款", "退货", "售后")):
        domain_terms = ("退款", "退货", "售后")
    elif any(marker in normalized for marker in ("充值", "支付", "余额", "提现")):
        domain_terms = ("充值", "支付", "余额", "提现")

    query_terms = {
        term
        for term in re.findall(r"[\u4e00-\u9fff]{2,8}", query)
        if term not in {"如何处理", "平台规则", "只查询"}
    }
    eligible_sources = sources
    if domain_terms:
        title_matches = [
            source
            for source in sources
            if any(
                term in safe_untrusted_excerpt(source.get("title"), 120)
                for term in domain_terms
            )
        ]
        if title_matches:
            eligible_sources = title_matches

    ranked: list[tuple[int, float, int, dict[str, object]]] = []
    for position, source in enumerate(eligible_sources):
        title = safe_untrusted_excerpt(source.get("title"), 120)
        excerpt = safe_untrusted_excerpt(source.get("excerpt"), 1_200)
        searchable = f"{title} {excerpt}"
        domain_score = sum(12 for term in domain_terms if term in title)
        domain_score += sum(3 for term in domain_terms if term in excerpt)
        overlap_score = sum(1 for term in query_terms if term in searchable)
        retrieval_score = float(str(source.get("score") or 0.0))
        ranked.append((domain_score + overlap_score, retrieval_score, -position, source))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1], item[2]))

    selected: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for _, _, _, source in ranked:
        identity = (
            str(source.get("document_id") or ""),
            safe_untrusted_excerpt(source.get("excerpt"), 160),
        )
        if identity in seen:
            continue
        seen.add(identity)
        selected.append(source)
        if len(selected) >= limit:
            break
    return selected


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
    if "recalled_memories" in data or "memory" in data:
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


def _explicit_self_scope_fallback(user_text: str) -> str | None:
    """Extract a clearly stated current-account fallback from a mixed request."""

    compact = re.sub(r"\s+", "", user_text)
    anchors = (
        "只告诉我自己",
        "只查询我自己",
        "只看我自己",
        "只显示我自己",
        "只告诉我本人",
        "只查询本人",
        "告诉我自己的",
        "查询我自己的",
        "查看我自己的",
    )
    positions = [compact.find(anchor) for anchor in anchors if compact.find(anchor) >= 0]
    if not positions:
        return None
    safe = compact[min(positions) :]
    return safe[:500] if _compound_intent_coverage(safe) else None


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
    return (
        f"; 预计送达 {_localized_agent_time(minimum)} 至 "
        f"{_localized_agent_time(maximum)} (来源: {source_label}, 仅供参考)"
    )


def _delivery_comparison_text(data: Mapping[str, Any], user_text: str) -> str | None:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    if not any(marker in normalized for marker in ("最早", "最晚", "比较", "对比")):
        return None
    values = data.get("items")
    if not isinstance(values, list):
        return None
    order_values = data.get("matched_order_ids") or data.get("order_ids")
    order_numbers = (
        [str(value) for value in order_values if isinstance(value, str)]
        if isinstance(order_values, list)
        else []
    )
    ranked: list[tuple[datetime, datetime, str]] = []
    delivered_positions: list[int] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        order_no = item.get("order_id")
        if not isinstance(order_no, str):
            continue
        position = order_numbers.index(order_no) + 1 if order_no in order_numbers else 1
        if str(item.get("shipment_status") or "").casefold() in {
            "delivered",
            "signed",
            "received",
            "completed",
        }:
            delivered_positions.append(position)
            continue
        estimate = item.get("delivery_estimate")
        if not isinstance(estimate, Mapping) or estimate.get("status") != "available":
            continue
        minimum = estimate.get("min_at")
        maximum = estimate.get("max_at")
        if not isinstance(minimum, str) or not isinstance(maximum, str):
            continue
        try:
            minimum_at = datetime.fromisoformat(minimum.replace("Z", "+00:00"))
            maximum_at = datetime.fromisoformat(maximum.replace("Z", "+00:00"))
        except ValueError:
            continue
        ranked.append((minimum_at, maximum_at, order_no))
    if delivered_positions:
        delivered_text = "、".join(f"第 {position} 笔" for position in delivered_positions)
        if not ranked:
            return f"{delivered_text}订单的包裹均已签收，已经送达，无需再比较预计时间。"
        selected_remaining = min(ranked, key=lambda value: value[0])
        remaining_position = (
            order_numbers.index(selected_remaining[2]) + 1
            if selected_remaining[2] in order_numbers
            else 1
        )
        return (
            f"{delivered_text}订单的包裹已经签收，是这些订单中已经最先到达的。"
            f"第 {remaining_position} 笔尚未签收，当前预计送达范围为 "
            f"{_localized_agent_time(selected_remaining[0].isoformat())} 至 "
            f"{_localized_agent_time(selected_remaining[1].isoformat())}。"
            "订单仍显示运输中可能是尚未确认收货，物流实况请以下方轨迹卡片为准。"
        )
    if not ranked:
        return "当前这些包裹都没有可靠预计送达时间，因此无法判断先后。"
    wants_latest = "最晚" in normalized
    selected = (
        max(ranked, key=lambda value: value[1])
        if wants_latest
        else min(ranked, key=lambda value: value[0])
    )
    position = order_numbers.index(selected[2]) + 1 if selected[2] in order_numbers else 1
    label = "最晚" if wants_latest else "最早"
    return (
        f"按当前配送预估，下方第 {position} 笔订单预计{label}送达，时间范围为 "
        f"{_localized_agent_time(selected[0].isoformat())} 至 "
        f"{_localized_agent_time(selected[1].isoformat())}。"
        "这只是当前估算，请以承运商后续轨迹为准。"
    )


def _localized_agent_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return safe_untrusted_excerpt(value, 40)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
    return f"{local.month}月{local.day}日 {local.hour:02d}:{local.minute:02d}"


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
