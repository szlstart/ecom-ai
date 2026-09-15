from __future__ import annotations

import re
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.modules.agent_runtime.answer_formatting import concise_policy_answer
from app.modules.agent_runtime.checkpoints import AgentCheckpointStore
from app.modules.agent_runtime.context_window import ContextWindow, ContextWindowBuilder
from app.modules.agent_runtime.conversation_state import ConversationStateRuntime
from app.modules.agent_runtime.conversation_summary import attach_rolling_summary
from app.modules.agent_runtime.deadline import AgentStreamGate, hard_deadline
from app.modules.agent_runtime.model_gateway import (
    STORE_CAPABILITIES,
    DeterministicStoreModelGateway,
    ModelGatewayError,
    StoreAgentPlan,
    StoreIntent,
    StoreModelGateway,
    StoreSupervisorPlan,
    StoreSupervisorSubtask,
    complete_store_plan,
    is_product_fulfillment_question,
    refine_store_plan_for_context,
    requests_cross_store_search,
    requests_other_user_data,
)
from app.modules.agent_runtime.models import AgentRun
from app.modules.agent_runtime.order_cards import (
    _requested_amount_minor_units,
    build_order_cards,
    order_nos_from_result,
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
from app.modules.messaging.sequence import lock_conversation_for_append
from app.modules.stores.models import Store
from app.modules.system.models import OutboxEvent

# The provider transport keeps a larger timeout for transient network recovery, but an
# interactive Agent turn must not spend that entire budget in one stage.  Business tools
# and structured cards remain the source of truth when either model stage times out.
MODEL_PLANNING_BUDGET_SECONDS = 12.0
MODEL_ANSWER_BUDGET_SECONDS = 20.0


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
            "检测到可能要求绕过系统规则或泄露敏感信息的指令，我无法执行该请求，本次不会调用业务工具。你可以重新描述正常的商品、订单或政策问题。",
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
    if requests_cross_store_search(trigger_text) or await _requests_named_other_store(
        session, context.store.id, trigger_text
    ):
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
    if _asks_store_human_service_capabilities(trigger_text):
        await _complete_message(
            session,
            context,
            (
                "本店人工客服适合处理需要店铺人员核实的事情，例如商品资料未写清的细节、"
                "发货异常、订单协调、售后凭证沟通和投诉建议。人工客服仍只能处理本店业务，"
                "不能查看其他店铺或其他顾客的信息，也不能绕过平台交易和售后规则。"
                "你刚才说先不转人工，所以本次不会创建人工服务请求。"
            ),
            execution_trace={"intent": "human_service_capabilities", "answer_mode": "direct"},
        )
        await _finish_checkpoint(checkpoint_store, context, "human_service_capabilities")
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
    context_window = context_window.with_conversation_state(
        await ConversationStateRuntime(session).load(
            context.conversation,
            before_sequence=context.trigger.sequence_no,
        )
    )
    deterministic_gateway = DeterministicStoreModelGateway()
    fast_plan = await deterministic_gateway.plan(trigger_text)
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
    supervisor_plan = await deterministic_gateway.plan_tasks(trigger_text)
    supervisor_plan_source = "deterministic_supervisor"
    planning_model_trace: dict[str, object] = {
        "status": "not_invoked",
        "provider_request_sent": False,
        "stage": "supervisor_planning",
        "reason": "当前运行使用受控本地规划器。",
    }
    provider_single_plan: StoreAgentPlan | None = None
    if isinstance(gateway, ProviderStoreModelGateway):
        planning_input = context_window.planning_input(trigger_text)
        planning_started_at = time.monotonic()
        try:
            provider_supervisor = await hard_deadline(
                gateway.plan_tasks(planning_input),
                budget_seconds=MODEL_PLANNING_BUDGET_SECONDS,
            )
            supervisor_plan = _merge_store_supervisor_plans(
                provider_supervisor,
                supervisor_plan,
                force_intent=(
                    fast_plan.intent
                    if fast_plan.intent == "human_handoff" and len(supervisor_plan.tasks) == 1
                    else None
                ),
            )
            supervisor_plan = _normalize_store_supervisor_plan_for_request(
                supervisor_plan, trigger_text
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
                provider_single_plan = complete_store_plan(
                    StoreAgentPlan(
                        task.intent,
                        search_text=(
                            # The model chooses the business goal and specialist, while
                            # the Tool Gateway rebuilds enforceable filters from the
                            # shopper's exact request.  A prose task objective often
                            # contains presentation instructions (for example “以可点击
                            # 卡片展示”), which must not become catalogue keywords.
                            trigger_text if task.intent == "product_recommend" else None
                        ),
                        confidence=supervisor_plan.confidence,
                        continuation_of_previous_turn=(
                            context_window.conversation_state is not None
                        ),
                    )
                )
        except (ModelGatewayError, TimeoutError) as exc:
            run.degraded_reason = model_failure_code(exc, "planning")
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
        if await _execute_store_supervisor_plan(
            session,
            builder,
            context,
            supervisor_plan,
            context_window=context_window,
            checkpoint_store=checkpoint_store,
            plan_source=supervisor_plan_source,
            planning_model_trace=planning_model_trace,
        ):
            await _finish_checkpoint(checkpoint_store, context, "compound_request")
            return
    plan = refine_store_plan_for_context(
        provider_single_plan or fast_plan,
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
        outcome.data["_planning_source"] = supervisor_plan_source
        outcome.data["_planning_model_trace"] = planning_model_trace
        outcome.data["_supervisor_goal_ledger"] = [
            {
                "goal_key": goal.goal_key,
                "description": goal.description,
                "assigned_task_key": goal.assigned_task_key,
            }
            for goal in supervisor_plan.goal_ledger
        ]
        outcome.data["_supervisor_coverage_complete"] = supervisor_plan.coverage_complete
        _focus_largest_package_variant(outcome.data, trigger_text)
        _focus_explicit_named_variant(outcome.data, trigger_text)
        _focus_body_weight_variant(outcome.data, trigger_text)
        _attach_variant_quantity_projection(outcome.data, trigger_text)
        _attach_product_fulfillment_facts(outcome.data, trigger_text)
        _attach_conversation_window(context_window, context.context_refs, outcome.data)
        if outcome.data.get("focused_variant") is not None:
            outcome.data["presentation"] = "detail_cards"
        elif outcome.data.get("product_fulfillment_facts") is not None:
            outcome.data["presentation"] = "detail_cards"
        elif plan.intent in {"inventory_lookup", "sku_compare", "policy_qa"}:
            outcome.data["presentation"] = "detail_cards"
        elif plan.intent == "product_qa" and _is_targeted_size_or_fit_question(trigger_text):
            outcome.data["presentation"] = "detail_cards"
        elif plan.intent == "product_qa" and _is_product_review_question(trigger_text):
            outcome.data["presentation"] = "detail_cards"
        elif plan.intent == "cart_add":
            outcome.data["presentation"] = (
                "product_cards"
                if outcome.data.get("cart_add_clarification") is True
                else "cart_card"
            )
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
        if tools.execution_records:
            outcome.data["_audit_tool_calls"] = list(tools.execution_records)
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
                sku_nos_by_product=(
                    {str(outcome.data["product_id"]): str(outcome.data["focused_sku_id"])}
                    if isinstance(outcome.data.get("product_id"), str)
                    and isinstance(outcome.data.get("focused_sku_id"), str)
                    else None
                ),
            )
            if plan.intent
            in {
                "product_qa",
                "product_compare",
                "sku_compare",
                "inventory_lookup",
                "product_recommend",
                "cart_add",
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
        if plan.intent == "cart_add" and outcome.data.get("cart_add_clarification") is not True:
            rich_content["cart_card"] = _store_cart_card(outcome.data)
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
        data={"_audit_tool_calls": list(tools.execution_records)},
        error_code=outcome.error_code,
        degraded_reason="tool_denied",
    )
    await _finish_checkpoint(checkpoint_store, context, plan.intent)


def _merge_store_supervisor_plans(
    provider: StoreSupervisorPlan,
    deterministic: StoreSupervisorPlan,
    *,
    force_intent: StoreIntent | None = None,
) -> StoreSupervisorPlan:
    """Use the model Supervisor plan when it passed the closed goal-ledger contract.

    The deterministic planner is an all-or-nothing availability fallback.  It must
    not append keyword-derived business tasks to a valid model plan, otherwise the
    runtime becomes a fixed workflow with an LLM-shaped front door.
    """

    if force_intent is not None:
        forced = next(
            (task for task in deterministic.tasks if task.intent == force_intent),
            StoreSupervisorSubtask("task_1", force_intent, "处理当前明确请求"),
        )
        return StoreSupervisorPlan((forced,), confidence=1.0)
    if not provider.tasks or not provider.coverage_complete:
        return deterministic
    return provider


def _normalize_store_supervisor_plan_for_request(
    plan: StoreSupervisorPlan, user_text: str
) -> StoreSupervisorPlan:
    # A plan that passed the provider's closed goal-ledger contract stays
    # authoritative.  Tool adapters resolve card references and SKU constraints;
    # this layer must not rewrite semantic work from local keyword patterns.
    del user_text
    return plan


def _store_specialist(intent: str) -> str:
    return {
        "product_qa": "storefront_catalog_advisor_agent",
        "product_compare": "storefront_catalog_advisor_agent",
        "product_recommend": "storefront_catalog_advisor_agent",
        "sku_compare": "storefront_variant_inventory_agent",
        "inventory_lookup": "storefront_variant_inventory_agent",
        "order_explain": "storefront_order_fulfillment_agent",
        "after_sale_progress": "storefront_after_sale_guidance_agent",
        "policy_qa": "storefront_service_knowledge_agent",
        "human_handoff": "store_human_service_coordinator",
    }.get(intent, "store_customer_support_supervisor")


def _store_specialist_public_name(code: str) -> str:
    return {
        "storefront_catalog_advisor_agent": "店内商品顾问 Agent",
        "storefront_variant_inventory_agent": "商品规格与库存 Agent",
        "storefront_order_fulfillment_agent": "本店订单履约 Agent",
        "storefront_service_knowledge_agent": "店铺服务知识 Agent",
        "storefront_after_sale_guidance_agent": "本店售后引导 Agent",
        "store_human_service_coordinator": "本店人工服务协调器",
        "store_customer_support_supervisor": "店铺 AI 客服 Supervisor Agent",
    }.get(code, "受限领域 Agent")


def _conditional_recommendation_result(
    task: StoreSupervisorSubtask,
    prior_results: list[tuple[StoreSupervisorSubtask, StoreAgentPlan, StoreToolResult]],
) -> StoreToolResult | None:
    """Stop a conditional fallback recommendation when the requested SKU is available.

    This is dynamic replanning rather than a fixed workflow: the Supervisor first
    observes live inventory, then decides whether the contingent recommendation is
    still necessary.  It never treats product-page text as current stock.
    """

    if task.intent != "product_recommend":
        return None
    normalized = re.sub(r"\s+", "", task.objective).casefold()
    if not re.search(r"如果.{0,40}(?:缺货|没货|无货)", normalized):
        return None
    requested_units = set(re.findall(r"\d+(?:支|件|个|本|片|包)", normalized))
    if not requested_units:
        return None
    for prior_task, _prior_plan, prior_outcome in reversed(prior_results):
        if prior_task.intent != "inventory_lookup" or prior_outcome.status != "succeeded":
            continue
        items = prior_outcome.data.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            sku_name = re.sub(
                r"\s+", "", str(item.get("sku_name") or item.get("name") or "")
            ).casefold()
            if not any(unit in sku_name for unit in requested_units):
                continue
            available = item.get("available_quantity")
            if isinstance(available, int) and available > 0:
                return StoreToolResult(
                    "succeeded",
                    {
                        "conditional_skipped": True,
                        "reason": "requested_variant_available",
                        "sku_name": item.get("sku_name") or item.get("name"),
                        "available_quantity": available,
                        "data_scope": prior_outcome.data.get("data_scope"),
                    },
                )
    return None


async def _execute_store_supervisor_plan(
    session: AsyncSession,
    builder: StoreContextBuilder,
    context: TrustedStoreAgentContext,
    supervisor_plan: StoreSupervisorPlan,
    *,
    context_window: ContextWindow,
    checkpoint_store: AgentCheckpointStore | None,
    plan_source: str,
    planning_model_trace: Mapping[str, object],
) -> bool:
    results: list[tuple[StoreSupervisorSubtask, StoreAgentPlan, StoreToolResult]] = []
    audit_records: list[dict[str, object]] = []
    previous_task_product_no: str | None = None
    for task in supervisor_plan.tasks[:4]:
        plan = complete_store_plan(
            StoreAgentPlan(
                task.intent,
                search_text=task.objective if task.intent == "product_recommend" else None,
                confidence=supervisor_plan.confidence,
                continuation_of_previous_turn=True,
            )
        )
        tools = StoreToolGateway(session)
        conditional = _conditional_recommendation_result(task, results)
        if conditional is not None:
            results.append((task, plan, conditional))
            continue
        try:
            outcome = await _execute_plan(
                builder,
                tools,
                context,
                plan,
                task.objective,
                preferred_product_no=(
                    previous_task_product_no
                    if previous_task_product_no
                    and _references_previous_task_product(task.objective)
                    else None
                ),
            )
        except ApplicationError as exc:
            outcome = StoreToolResult("failed", {}, exc.code)
        if outcome.status == "succeeded":
            outcome_product_no = outcome.data.get("product_id")
            if isinstance(outcome_product_no, str):
                previous_task_product_no = outcome_product_no
            _focus_largest_package_variant(outcome.data, task.objective)
            _focus_explicit_named_variant(outcome.data, task.objective)
            _focus_body_weight_variant(outcome.data, task.objective)
            _attach_variant_quantity_projection(outcome.data, task.objective)
            if plan.intent == "product_qa":
                _attach_product_fulfillment_facts(outcome.data, task.objective)
            _attach_conversation_window(context_window, context.context_refs, outcome.data)
            if context.trigger.message_type not in {"product_card", "order_card"}:
                await _attach_store_knowledge(
                    session,
                    checkpoint_store,
                    context,
                    plan.intent,
                    outcome.data,
                )
        audit_records.extend(dict(item) for item in tools.execution_records)
        results.append((task, plan, outcome))

    succeeded = [item for item in results if item[2].status == "succeeded"]
    if not succeeded:
        return False

    product_nos: list[str] = []
    order_nos: list[str] = []
    detail_cards: list[dict[str, object]] = []
    answer_parts: list[str] = []
    shared_knowledge_sources: list[object] = []
    for _task, _plan, shared_outcome in results:
        values = shared_outcome.data.get("knowledge_sources")
        if isinstance(values, list):
            shared_knowledge_sources.extend(values)
    for task, plan, outcome in results:
        if outcome.status != "succeeded":
            answer_parts.append(f"{_store_task_label(task.intent)}暂时未完成")
            continue
        product_nos.extend(product_nos_from_result(outcome.data))
        order_nos.extend(order_nos_from_result(outcome.data))
        detail_cards.extend(_store_detail_cards(plan, outcome.data, task.objective))
        render_data = outcome.data
        render_text = task.objective
        if plan.intent == "policy_qa" and shared_knowledge_sources:
            render_data = {**outcome.data, "knowledge_sources": shared_knowledge_sources}
            render_text = f"{task.objective}\n{context.trigger.text_content or ''}"
        rendered = _render(plan, render_data, render_text)
        if rendered:
            answer_parts.append(
                f"{_store_task_label(task.intent)}: {safe_untrusted_excerpt(rendered, 520)}"
            )

    rich_content: dict[str, object] = {}
    unique_product_nos = list(dict.fromkeys(product_nos))[:8]
    if unique_product_nos:
        rich_content["product_cards"] = await build_product_cards(
            session,
            context.conversation,
            unique_product_nos,
        )
    unique_order_nos = list(dict.fromkeys(order_nos))[:8]
    if unique_order_nos:
        rich_content["order_cards"] = await build_order_cards(
            session,
            context.user,
            context.conversation,
            unique_order_nos,
        )
    if detail_cards:
        rich_content["detail_cards"] = _dedupe_store_detail_cards(detail_cards)[:12]
    cart_result = next(
        (
            outcome.data
            for task, _plan, outcome in reversed(results)
            if task.intent == "cart_add"
            and outcome.status == "succeeded"
            and outcome.data.get("cart_add_clarification") is not True
        ),
        None,
    )
    if isinstance(cart_result, Mapping):
        rich_content["cart_card"] = _store_cart_card(cart_result)

    steps: list[dict[str, object]] = []
    subtasks: list[dict[str, object]] = []
    for task, _plan, outcome in results:
        specialist = _store_specialist(task.intent)
        status = "succeeded" if outcome.status == "succeeded" else outcome.status
        matching_calls = [
            record
            for record in audit_records
            if record.get("tool_code") in STORE_CAPABILITIES.get(task.intent, ())
        ]
        specialist_name = _store_specialist_public_name(specialist)
        step: dict[str, object] = {
            "kind": "delegation",
            "label": f"委派给 {specialist_name}",
            "status": status,
            "specialist": specialist,
            "objective": task.objective,
            "allowed_tools": list(STORE_CAPABILITIES.get(task.intent, ())),
            "tool_calls": len(matching_calls),
        }
        if matching_calls:
            step["tool_call"] = matching_calls[0]
            step["tool_code"] = matching_calls[0].get("tool_code")
        steps.append(step)
        subtasks.append(
            {
                "subtask_key": task.subtask_key,
                "specialist": specialist_name,
                "specialist_code": specialist,
                "intent": task.intent,
                "objective": task.objective,
                "status": status,
                "allowed_tools": list(STORE_CAPABILITIES.get(task.intent, ())),
                "depth": 1,
            }
        )
    data: dict[str, object] = {
        "compound_results": {
            task.subtask_key: {
                "intent": task.intent,
                "status": outcome.status,
                "data": outcome.data,
            }
            for task, _plan, outcome in results
        },
        "_audit_tool_calls": audit_records,
        "conversation_window": context_window.model_projection(context.context_refs),
    }
    knowledge_sources: list[object] = []
    for _task, _plan, outcome in results:
        sources = outcome.data.get("knowledge_sources")
        if isinstance(sources, list):
            knowledge_sources.extend(sources)
    if knowledge_sources:
        data["knowledge_sources"] = knowledge_sources
    source_ids = tuple(
        dict.fromkeys(
            f"tool:{record['tool_code']}"
            for record in audit_records
            if isinstance(record.get("tool_code"), str)
        )
    )
    trace = public_trace(
        run_id=context.run.run_no,
        agent="店铺 AI 客服 Supervisor Agent",
        model=context.agent_version.model_profile,
        question=agent_trace_question(context.trigger),
        intent="compound_request",
        data=data,
        steps=steps,
        source_ids=source_ids,
        tool_code="multi_agent",
        extra={
            "orchestration_mode": "multi_agent",
            "execution_strategy": "serial_shared_transaction",
            "planning_source": plan_source,
            "goal_ledger": [
                {
                    "goal_key": goal.goal_key,
                    "description": goal.description,
                    "assigned_task_key": goal.assigned_task_key,
                }
                for goal in supervisor_plan.goal_ledger
            ],
            "coverage_complete": supervisor_plan.coverage_complete,
            "subtasks": subtasks,
            "confidence": supervisor_plan.confidence,
            "model_invocation": dict(planning_model_trace),
        },
    )
    completed_count = len(succeeded)
    failed_labels = [
        _store_task_label(task.intent)
        for task, _plan, outcome in results
        if outcome.status != "succeeded"
    ]
    if completed_count == len(results):
        answer = "已完成逐项核对。"
    else:
        answer = (
            f"已完成 {completed_count}/{len(results)} 项核对。"
            + (f"暂未完成: {'、'.join(failed_labels)}。" if failed_labels else "")
            + "已取得的结果仍保留在下方卡片中。"
        )
    if answer_parts:
        unique_answer_parts = list(dict.fromkeys(answer_parts))
        answer += "\n\n" + "\n\n".join(unique_answer_parts)
    await _complete_message(
        session,
        context,
        answer,
        data=data,
        execution_trace=trace,
        extra_content=rich_content or None,
    )
    return True


def _store_task_label(intent: str) -> str:
    return {
        "product_qa": "商品信息",
        "product_compare": "商品对比",
        "product_recommend": "店内推荐",
        "sku_compare": "款式对比",
        "inventory_lookup": "实时库存",
        "policy_qa": "店铺政策",
        "order_explain": "本店订单与物流",
        "cart_add": "加入购物车",
        "human_handoff": "人工服务",
    }.get(intent, "当前问题")


def _dedupe_store_detail_cards(
    cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for card in cards:
        action = card.get("action")
        action_path = str(action.get("path") or "") if isinstance(action, Mapping) else ""
        key = (
            str(card.get("kind") or ""),
            str(card.get("title") or ""),
            str(card.get("badge") or ""),
            repr(card.get("rows") or []),
            action_path,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(card)
    return unique


async def _execute_plan(
    builder: StoreContextBuilder,
    tools: StoreToolGateway,
    context: TrustedStoreAgentContext,
    plan: StoreAgentPlan,
    trigger_text: str,
    *,
    preferred_product_no: str | None = None,
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
        all_product_nos = (
            [
                str(item["product_id"])
                for item in items[:4]
                if isinstance(item, dict) and isinstance(item.get("product_id"), str)
            ]
            if isinstance(items, list)
            else []
        )
        reference_indices = product_card_reference_indices(trigger_text)
        product_nos = (
            [
                all_product_nos[index]
                for index in reference_indices[:4]
                if index < len(all_product_nos)
            ]
            if len(reference_indices) >= 2
            else all_product_nos
        )
        if len(product_nos) >= 2:
            comparison = await tools.compare_products(context, product_nos)
            if comparison.status == "succeeded":
                recommendations.data["comparison"] = comparison.data.get("items", [])
                recommendations.data["comparison_requested"] = len(reference_indices) >= 2
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
        else:
            recent_order_nos = await recent_agent_order_nos(
                tools.session,
                context.conversation,
                before_sequence=context.trigger.sequence_no,
            )
            order_no = referenced_order_no(trigger_text, recent_order_nos)
            if order_no is None:
                order_no = await referenced_recent_order_no(
                    tools.session,
                    context.conversation,
                    before_sequence=context.trigger.sequence_no,
                    user_text=trigger_text,
                )
            if order_no is None and "order" in context.context_refs:
                order_no = (await builder.require_active_context(context, "order")).resource_no
        if order_no is None:
            order_list = await tools.list_user_orders(context)
            order_no, reference_status = _match_store_order_reference(
                trigger_text, order_list.data
            )
            if order_no is None:
                if reference_status is not None:
                    order_list.data["requested_reference_status"] = reference_status
                    order_list.data["requested_amounts"] = sorted(
                        _requested_amount_minor_units(
                            re.sub(r"\s+", "", trigger_text).casefold()
                        )
                    )
                return order_list
        summary = await tools.order_summary(context, order_no)
        if summary.status != "succeeded":
            return summary
        shipment_result = await tools.shipments(context, order_no)
        if shipment_result.status != "succeeded":
            return shipment_result
        summary.data["shipments"] = shipment_result.data.get("items", [])
        summary.data["presentation"] = "order_card"
        return summary
    if plan.intent == "after_sale_progress":
        return await tools.list_user_refunds(context)
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
        if plan.intent == "cart_add":
            return await _add_store_product_to_cart(
                tools,
                context,
                product_no,
                trigger_text,
                preferred_sku_no=_trigger_resource_no(
                    context.trigger, "product_card", "sku_id"
                ),
            )
        return await tools.product(context, product_no)
    if preferred_product_no is not None:
        if plan.intent == "sku_compare":
            comparison = await tools.compare_skus(context, preferred_product_no)
            _limit_sku_comparison_to_named_variants(comparison.data, trigger_text)
            return comparison
        if plan.intent == "inventory_lookup":
            return await tools.inventory(context, preferred_product_no)
        if plan.intent == "cart_add":
            return await _add_store_product_to_cart(
                tools, context, preferred_product_no, trigger_text
            )
        return await tools.product(context, preferred_product_no)
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
    if plan.intent == "cart_add":
        preferred_sku_no = (
            recent_reference.get("sku_id") if isinstance(recent_reference, Mapping) else None
        )
        return await _add_store_product_to_cart(
            tools,
            context,
            product_no,
            trigger_text,
            preferred_sku_no=(preferred_sku_no if isinstance(preferred_sku_no, str) else None),
        )
    return await tools.product(context, product_no)


async def _add_store_product_to_cart(
    tools: StoreToolGateway,
    context: TrustedStoreAgentContext,
    product_no: str,
    user_text: str,
    *,
    preferred_sku_no: str | None = None,
) -> StoreToolResult:
    product = await tools.product(context, product_no)
    if product.status != "succeeded":
        return product
    skus_value = product.data.get("skus")
    skus = (
        [item for item in skus_value if isinstance(item, Mapping)]
        if isinstance(skus_value, list)
        else []
    )
    compact = re.sub(r"\s+", "", user_text).casefold()
    explicit_matches = [
        item
        for item in skus
        if isinstance(item.get("sku_name"), str)
        and re.sub(r"\s+", "", str(item["sku_name"])).casefold() in compact
    ]
    selected: Mapping[str, Any] | None = None
    if len(explicit_matches) == 1:
        selected = explicit_matches[0]
    elif preferred_sku_no is not None:
        selected = next(
            (item for item in skus if item.get("sku_id") == preferred_sku_no),
            None,
        )
    elif len(skus) == 1:
        selected = skus[0]
    if selected is None:
        product.data["cart_add_clarification"] = True
        product.data["presentation"] = "product_cards"
        return product
    available = selected.get("available_quantity")
    if not isinstance(available, int) or available <= 0:
        product.data["cart_add_unavailable_variant"] = {
            "sku_name": selected.get("sku_name"),
            "available_quantity": available,
        }
        product.data["presentation"] = "detail_cards"
        return product
    sku_no = selected.get("sku_id")
    if not isinstance(sku_no, str):
        return StoreToolResult("failed", {}, "TOOL_RESULT_INVALID")
    quantity = _requested_store_cart_quantity(user_text)
    result = await tools.add_cart_item(
        context,
        product_no,
        sku_no,
        quantity,
    )
    if result.status == "succeeded":
        result.data["added_product_name"] = product.data.get("name")
        result.data["added_sku_name"] = selected.get("sku_name")
        result.data["added_quantity"] = quantity
        result.data["presentation"] = "cart_card"
    return result


def _requested_store_cart_quantity(value: str) -> int:
    compact = re.sub(r"\s+", "", value).casefold()
    for pattern in (
        r"数量(?:改成|改为|是|为)?(\d{1,2})",
        r"(?:加入|加到|放进|放到|加进)(?:购物车)?(?:里|中)?[，,]?(\d{1,2})件",
        r"(?:加入|加到|放进|放到|加进)(\d{1,2})件",
    ):
        match = re.search(pattern, compact)
        if match is not None:
            return min(99, max(1, int(match.group(1))))
    return 1


def _references_previous_task_product(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return any(
        marker in normalized
        for marker in ("该商品", "这个商品", "上述商品", "这件商品", "该款", "它的")
    )


def _match_store_order_reference(
    user_text: str, data: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """Resolve an amount reference against freshly read, store-scoped orders."""

    requested = _requested_amount_minor_units(re.sub(r"\s+", "", user_text).casefold())
    if not requested:
        return None, None
    values = data.get("items")
    matches: list[str] = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, Mapping):
            continue
        amounts = item.get("amounts")
        paid = amounts.get("paid") if isinstance(amounts, Mapping) else None
        minor_units = paid.get("minor_units") if isinstance(paid, Mapping) else None
        order_no = item.get("order_id")
        if isinstance(order_no, str) and str(minor_units) in requested:
            matches.append(order_no)
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0], "matched"
    return None, "ambiguous" if unique else "not_found"


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


def _focus_largest_package_variant(data: dict[str, Any], user_text: str) -> None:
    """Select an explicitly requested largest pack without guessing about sizes."""

    normalized = re.sub(r"\s+", "", user_text).casefold()
    if "最大" not in normalized and "最多" not in normalized:
        return
    if not (
        "包装" in normalized
        or "规格" in normalized
        or "款式" in normalized
        or re.search(r"最多(?:有|是|为|的)?几(?:支|件|个|本|片|包)", normalized)
        or re.search(r"最大(?:有|是|为|的)?多少(?:支|件|个|本|片|包)", normalized)
    ):
        return
    source_key = "items" if isinstance(data.get("items"), list) else "skus"
    values = data.get(source_key)
    if not isinstance(values, list):
        return
    ranked: list[tuple[int, Mapping[str, Any]]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        name = value.get("sku_name") or value.get("name")
        if not isinstance(name, str):
            continue
        quantities = [int(item) for item in re.findall(r"(?<!\d)(\d{1,5})(?!\d)", name)]
        if quantities:
            ranked.append((max(quantities), value))
    if not ranked:
        return
    _, selected = max(ranked, key=lambda item: item[0])
    focused = dict(selected)
    data["focused_variant"] = focused
    data["focused_variant_reason"] = "largest_package"
    if isinstance(focused.get("sku_id"), str):
        data["focused_sku_id"] = focused["sku_id"]
    data[source_key] = [focused]


def _focus_explicit_named_variant(data: dict[str, Any], user_text: str) -> None:
    """Focus a unique SKU named by color, size or package count in the current turn."""

    if data.get("focused_variant") is not None:
        return
    values = data.get("items") if isinstance(data.get("items"), list) else data.get("skus")
    if not isinstance(values, list):
        return
    normalized = re.sub(r"\s+", "", user_text).casefold()
    colors = (
        "黑色",
        "白色",
        "红色",
        "蓝色",
        "绿色",
        "灰色",
        "黄色",
        "粉色",
        "紫色",
        "棕色",
        "米色",
    )
    requested_colors = {item for item in colors if item in normalized}
    requested_sizes = set(re.findall(r"(?<![a-z])(?:xxxxl|xxxl|xxl|xl|l|m|s)(?![a-z])", normalized))
    requested_units = set(re.findall(r"\d+(?:支|件|个|本|片|包)", normalized))
    if not requested_colors and not requested_sizes and not requested_units:
        return
    ranked: list[tuple[int, Mapping[str, Any]]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        name = str(value.get("sku_name") or value.get("name") or "")
        candidate = re.sub(r"\s+", "", name).casefold()
        candidate_sizes = set(
            re.findall(r"(?<![a-z])(?:xxxxl|xxxl|xxl|xl|l|m|s)(?![a-z])", candidate)
        )
        score = 2 * len(requested_colors.intersection({c for c in colors if c in candidate}))
        score += 2 * len(requested_sizes.intersection(candidate_sizes))
        candidate_units = set(re.findall(r"\d+(?:支|件|个|本|片|包)", candidate))
        score += 2 * len(requested_units.intersection(candidate_units))
        if score:
            ranked.append((score, value))
    if not ranked:
        return
    best_score = max(score for score, _value in ranked)
    winners = [value for score, value in ranked if score == best_score]
    if len(winners) != 1:
        return
    focused = dict(winners[0])
    data["focused_variant"] = focused
    data["focused_variant_reason"] = "explicit_name"
    if isinstance(focused.get("sku_id"), str):
        data["focused_sku_id"] = focused["sku_id"]
    if isinstance(data.get("items"), list):
        data["items"] = [focused]
    elif isinstance(data.get("skus"), list):
        data["skus"] = [focused]


def _focus_body_weight_variant(data: dict[str, Any], user_text: str) -> None:
    """Focus the narrowest matching weight-labelled SKU without promising fit."""

    if data.get("focused_variant") is not None:
        return
    normalized = re.sub(r"\s+", "", user_text).casefold()
    weight_match = re.search(r"(?<!\d)(\d{2,3})(?:\.\d+)?斤", normalized)
    if weight_match is None:
        return
    weight = int(weight_match.group(1))
    values = data.get("items") if isinstance(data.get("items"), list) else data.get("skus")
    if not isinstance(values, list):
        return
    colors = (
        "黑色",
        "白色",
        "红色",
        "蓝色",
        "绿色",
        "灰色",
        "黄色",
        "粉色",
        "紫色",
        "棕色",
        "米色",
    )
    requested_colors = {color for color in colors if color in normalized}
    ranked: list[tuple[int, Mapping[str, Any]]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        name = re.sub(r"\s+", "", str(value.get("sku_name") or value.get("name") or ""))
        if requested_colors and not any(color in name for color in requested_colors):
            continue
        limit_match = re.search(r"(?<!\d)(\d{2,3})(?:\.\d+)?斤以下", name)
        if limit_match is None:
            continue
        upper_limit = int(limit_match.group(1))
        if upper_limit >= weight:
            ranked.append((upper_limit, value))
    if not ranked:
        return
    smallest_limit = min(limit for limit, _value in ranked)
    winners = [value for limit, value in ranked if limit == smallest_limit]
    if len(winners) != 1:
        return
    focused = dict(winners[0])
    data["focused_variant"] = focused
    data["focused_variant_reason"] = "body_weight_reference"
    data["requested_body_weight_jin"] = weight
    if isinstance(focused.get("sku_id"), str):
        data["focused_sku_id"] = focused["sku_id"]
    if isinstance(data.get("items"), list):
        data["items"] = [focused]
    elif isinstance(data.get("skus"), list):
        data["skus"] = [focused]


def _attach_variant_quantity_projection(data: dict[str, Any], user_text: str) -> None:
    """Calculate a requested pack quantity without creating or changing an order."""

    focused = data.get("focused_variant")
    if not isinstance(focused, Mapping):
        return
    normalized = re.sub(r"\s+", "", user_text).casefold()
    count_match = re.search(
        r"(?:买|要|来|算)?(?P<count>\d+|[一二两三四五六七八九十])(?:盒|包|件|份|套|组)",
        normalized,
    )
    if count_match is None:
        return
    purchase_count = _natural_quantity(count_match.group("count"))
    if purchase_count is None or purchase_count < 1 or purchase_count > 99:
        return
    name = str(focused.get("sku_name") or focused.get("name") or "")
    unit_match = re.search(r"(?P<count>\d+)(?:支|个|本|片|枚)", name)
    price = focused.get("price")
    if not isinstance(price, Mapping):
        return
    try:
        unit_minor = int(price.get("minor_units") or 0)
    except (TypeError, ValueError):
        return
    if unit_minor < 0:
        return
    contained_units = int(unit_match.group("count")) if unit_match else None
    data["variant_quantity_projection"] = {
        "sku_name": safe_untrusted_excerpt(name or "当前款式", 80),
        "purchase_count": purchase_count,
        "contained_units": contained_units,
        "total_units": contained_units * purchase_count if contained_units is not None else None,
        "unit_price": _money_value(price),
        "total_price": _money_value(
            {
                "minor_units": unit_minor * purchase_count,
                "currency": price.get("currency") or "CNY",
            }
        ),
    }


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


def _is_targeted_size_or_fit_question(user_text: str) -> bool:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    return any(
        marker in normalized
        for marker in ("尺码", "多少码", "多大码", "最大码", "能穿", "合身", "适合我")
    ) or bool(re.search(r"\d{2,3}斤", normalized))


def _requests_previous_result_reselection(user_text: str) -> bool:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    return any(
        marker in normalized
        for marker in ("再看看第一个", "再看第一个", "回到第一个", "换回第一个")
    )


async def _requests_named_other_store(
    session: AsyncSession, current_store_id: int, user_text: str
) -> bool:
    names = list(
        await session.scalars(
            select(Store.store_name).where(
                Store.id != current_store_id,
                Store.store_status.in_(("active", "suspended")),
            )
        )
    )
    return _named_other_store_in_text(user_text, names)


def _named_other_store_in_text(user_text: str, store_names: list[str]) -> bool:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    business_query = any(
        marker in normalized
        for marker in (
            "商品",
            "衣服",
            "文具",
            "裤子",
            "同款",
            "库存",
            "价格",
            "订单",
            "物流",
            "政策",
            "查",
            "找",
            "推荐",
            "比较",
            "对比",
            "最便宜",
        )
    )
    for store_name in store_names:
        name = re.sub(r"\s+", "", store_name).casefold()
        if name and name in normalized:
            return True
        alias = re.sub(r"(?:旗舰店|专卖店|店铺|商店|店)$", "", name)
        if business_query and len(alias) >= 4 and alias in normalized:
            return True
    return False


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
    conversation = await lock_conversation_for_append(session, context.conversation.id)
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
            "planning_source": data.get("_planning_source"),
            "planning_confidence": plan.confidence,
            "required_capabilities": list(plan.required_capabilities),
            "missing_slots": list(plan.missing_slots),
            "continuation_of_previous_turn": plan.continuation_of_previous_turn,
            "response_strategy": plan.response_strategy,
            "model_invocation": data.get("_planning_model_trace"),
            "goal_ledger": data.get("_supervisor_goal_ledger", []),
            "coverage_complete": data.get("_supervisor_coverage_complete", True),
        },
    )
    if data.get("presentation") in {
        "order_card",
        "order_cards",
        "product_cards",
        "product_comparison",
        "detail_cards",
        "cart_card",
        "after_sale_cards",
    }:
        trace["answer_mode"] = "structured_ui"
        return fallback, trace
    if not isinstance(gateway, ProviderStoreModelGateway):
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
        "after_sale_progress": "after_sale.list_user_store_refunds",
        "sku_compare": "catalog.compare_skus",
        "inventory_lookup": "catalog.get_inventory_availability",
        "product_qa": "catalog.get_product",
        "cart_add": "cart.add_item",
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
    if plan.intent == "cart_add":
        if data.get("cart_add_clarification") is True:
            return (
                "这件商品有多个款式。请在商品卡片里选择款式，"
                "或直接告诉我款式名称，我再加入购物车。"
            )
        unavailable = data.get("cart_add_unavailable_variant")
        if isinstance(unavailable, Mapping):
            sku_name = safe_untrusted_excerpt(
                unavailable.get("sku_name") or "这个款式", 80
            )
            return f"“{sku_name}”当前无货，没有加入购物车。你可以换一个有货款式。"
        return (
            f"已将“{safe_untrusted_excerpt(data.get('added_product_name') or '这件商品', 100)}”"
            f"的“{safe_untrusted_excerpt(data.get('added_sku_name') or '所选款式', 80)}”"
            f"加入购物车，共 {int(data.get('added_quantity') or 1)} 件。"
        )
    projection = data.get("variant_quantity_projection")
    if isinstance(projection, Mapping):
        total_units = projection.get("total_units")
        unit_summary = f"，共 {total_units} 支" if isinstance(total_units, int) else ""
        return (
            f"“{projection.get('sku_name', '当前款式')}”每盒 "
            f"{projection.get('unit_price', '¥0.00')}，"
            f"{projection.get('purchase_count', 0)} 盒{unit_summary}，商品金额 "
            f"{projection.get('total_price', '¥0.00')}。这里只做试算，没有下单或修改购物车。"
        )
    focused_variant = data.get("focused_variant")
    if isinstance(focused_variant, Mapping):
        name = safe_untrusted_excerpt(
            focused_variant.get("sku_name") or focused_variant.get("name") or "最大包装",
            80,
        )
        price = (
            _money_value(focused_variant.get("price"))
            if focused_variant.get("price") is not None
            else _money(
                focused_variant.get("sale_price_amount"),
                focused_variant.get("currency") or "CNY",
            )
        )
        quantity = focused_variant.get("available_quantity")
        availability = _availability_label(focused_variant)
        quantity_text = f"，可售 {max(0, quantity)} 件" if isinstance(quantity, int) else ""
        prefix = (
            "最大包装"
            if data.get("focused_variant_reason") == "largest_package"
            else "按商品体重标注可参考的款式"
            if data.get("focused_variant_reason") == "body_weight_reference"
            else "你问的款式"
        )
        disclaimer = (
            " 体重标注只能作为参考，无法保证一定合身或达到宽松效果，请再结合商品尺寸信息判断。"
            if data.get("focused_variant_reason") == "body_weight_reference"
            else ""
        )
        return f"{prefix}是“{name}”，价格 {price}，当前{availability}{quantity_text}。{disclaimer}"
    fulfillment_answer = _render_product_fulfillment_answer(data)
    if fulfillment_answer is not None:
        return fulfillment_answer
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
        if "数学" in user_text and isinstance(items, list):
            names = [
                safe_untrusted_excerpt(item.get("name") or "", 120)
                for item in items[:2]
                if isinstance(item, Mapping)
            ]
            pencil = next((name for name in names if "铅笔" in name), None)
            ruler = next((name for name in names if "直尺" in name or "尺子" in name), None)
            if pencil and ruler:
                return (
                    f"这两件用途不同\uff1a“{pencil}”适合需要铅笔书写或填涂的部分。"
                    f"“{ruler}”适合几何作图和测量。仅凭“数学考试”不能负责任地只选一个，"
                    "请按试卷是否有作图题及考场规定选择。两件商品的可点击入口都在下方。"
                )
        return f"已按同一口径对比这 {count} 件商品，价格、公开参数和商品入口都在下方卡片中。"
    if plan.intent == "policy_qa":
        items = data.get("items")
        knowledge = data.get("knowledge_sources")
        normalized_question = re.sub(r"\s+", "", user_text).casefold()
        asks_dispatch = any(
            marker in normalized_question
            for marker in ("几天发", "多久发", "发货时效", "什么时候发")
        )
        asks_courier = any(
            marker in normalized_question
            for marker in ("默认快递", "什么快递", "用什么快递", "什么物流", "用什么物流")
        )
        if asks_dispatch or asks_courier:
            raw_sources = [
                str(item.get("content") or "")
                for item in (items if isinstance(items, list) else [])
                if isinstance(item, Mapping)
            ] + [
                str(item.get("excerpt") or "")
                for item in (knowledge if isinstance(knowledge, list) else [])
                if isinstance(item, Mapping)
            ]
            compact_sources = re.sub(r"\s+", "", " ".join(raw_sources))
            answers: list[str] = []
            if asks_dispatch:
                dispatch = re.search(
                    r"(?:付款后|下单后|拍下后)?(?:一般|通常|预计)?"
                    r"(?P<start>\d{1,2})(?:[-—~至到](?P<end>\d{1,2}))?天内(?:发出|发货)",
                    compact_sources,
                )
                if dispatch is None:
                    answers.append(
                        "本店公开政策暂未统一写明付款后几天发货。不同商品可能不同，"
                        "请点选具体商品后我再查它的当前履约资料。"
                    )
                else:
                    end = dispatch.group("end")
                    window = (
                        f"{dispatch.group('start')}-{end} 天内"
                        if end
                        else f"{dispatch.group('start')} 天内"
                    )
                    answers.append(f"本店当前公开资料写明，付款后{window}发货。")
            if asks_courier:
                carrier = next((item for item in _KNOWN_CARRIERS if item in compact_sources), None)
                answers.append(
                    f"本店当前公开资料写明默认使用{carrier}发货。"
                    if carrier
                    else "本店公开政策暂未统一写明默认快递，请点选具体商品后再核对。"
                )
            delivery = data.get("platform_delivery")
            if isinstance(delivery, Mapping):
                answers.append("平台配送方式为邮寄，当前结算规则为包邮。")
            return "\n\n".join(answers)
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
            for item in (knowledge if isinstance(knowledge, list) else [])
            if isinstance(item, dict)
        )
        has_logistics_escalation_source = any(
            "物流" in str(title or "")
            and "平台人工客服" in str(content or "")
            for title, content in sources
        )
        asks_stalled_logistics = any(
            marker in normalized_question
            for marker in (
                "没新轨迹",
                "没有新轨迹",
                "无新轨迹",
                "轨迹停滞",
                "物流不动",
                "长时间未收到",
                "长时间没收到",
            )
        )
        asks_escalation_order = (
            "本店人工" in normalized_question
            and "平台客服" in normalized_question
            and any(marker in normalized_question for marker in ("顺序", "先联系", "何时升级"))
        )
        if (asks_stalled_logistics or asks_escalation_order) and has_logistics_escalation_source:
            return (
                "处理顺序：1. 先在订单物流卡核对承运商、运单号、最后更新时间和位置；"
                "2. 再联系本店人工核查发货与承运情况；"
                "3. 若长时间仍无新轨迹，或明确显示异常、退回、疑似丢件，"
                "再携带订单和物流记录联系平台人工，并按当前订单状态检查售后资格。"
                "本次只说明顺序，没有为你转人工。"
            )
        answer = concise_policy_answer(
            user_text, sources, intro="根据本店当前生效政策"
        )
        if answer == "根据本店当前生效政策" and any(
            marker in normalized_question
            for marker in ("退换", "换货", "质量问题", "商品质量")
        ):
            has_platform_after_sale_source = any(
                isinstance(item, Mapping)
                and item.get("scope") == "platform:platform"
                and any(
                    marker in str(item.get("title") or "")
                    for marker in ("售后", "退款", "客服规则")
                )
                for item in (knowledge if isinstance(knowledge, list) else [])
            )
            if has_platform_after_sale_source:
                return (
                    "本店未发布额外退换政策时，适用平台当前公开售后规则。"
                    "商品存在质量问题可从对应订单发起售后并保留图片等凭证；"
                    "是否支持退货或退款仍需按订单当前状态和可退款数量实时检查。"
                )
        return answer
    if plan.intent == "after_sale_progress":
        items = data.get("items")
        count = len(items) if isinstance(items, list) else 0
        if count == 0:
            return "你在本店暂时没有已提交的售后申请。"
        return f"已找到你在本店的 {count} 笔售后申请，当前进度已整理在卡片中。"
    if plan.intent == "order_explain":
        if data.get("presentation") == "order_cards":
            items = data.get("items")
            count = len(items) if isinstance(items, list) else 0
            reference_status = data.get("requested_reference_status")
            requested_amounts = data.get("requested_amounts")
            if reference_status in {"not_found", "ambiguous"}:
                amount_text = "指定金额"
                if isinstance(requested_amounts, list) and requested_amounts:
                    try:
                        amount_text = f"¥{int(requested_amounts[0]) / 100:.2f}"
                    except (TypeError, ValueError):
                        pass
                return (
                    f"找到多笔实付 {amount_text} 的本店订单，请从下方卡片中选择具体一笔。"
                    if reference_status == "ambiguous"
                    else f"当前没有找到实付 {amount_text} 的本店订单。下方是最近订单，"
                    "你可以点击具体一笔后继续问我。"
                )
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
        if data.get("conditional_skipped") is True:
            sku_name = safe_untrusted_excerpt(data.get("sku_name") or "你指定的款式", 80)
            available = data.get("available_quantity")
            quantity = f"，当前可售 {available} 件" if isinstance(available, int) else ""
            return f"“{sku_name}”目前有货{quantity}，所以没有再用其他商品替代。"
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return "本店当前没有符合条件的在售商品。我不会跨店补充结果，你可以调整一个筛选条件。"
        if len(items) == 1:
            return (
                "按你给出的条件，本店目前只找到 1 件匹配的在售商品，已放在卡片中。"
                "我没有用不相关商品凑数。你可以再补充用途或偏好，我会继续筛选。"
            )
        next_hint = (
            "如需进一步缩小范围，可以补充考试类型、科目或其他偏好。"
            if any(marker in user_text for marker in ("预算", "以内", "不超过", "低于"))
            else "如果你再告诉我预算或具体用途，我还能继续缩小范围。"
        )
        inventory_phrase = (
            "，均有实时可售库存"
            if data.get("inventory_policy") == "available_sku_required"
            else ""
        )
        recommendation_answer = (
            f"为你找到 {min(len(items), 5)} 件本店在售商品{inventory_phrase}。"
            "可以直接点击卡片查看详情。" + next_hint
        )
        comparison = data.get("comparison")
        if data.get("comparison_requested") is True and isinstance(comparison, list):
            comparison_answer = _render(
                StoreAgentPlan("product_compare"),
                {"items": comparison},
                user_text,
            )
            return recommendation_answer + " " + comparison_answer
        return recommendation_answer
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
    review_answer = _render_product_review_answer(data, user_text)
    if review_answer is not None:
        return review_answer
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
    asks_status = any(term in normalized for term in ("状态", "进度", "现在怎样", "怎么样了"))
    asks_logistics = any(
        term in normalized
        for term in ("物流", "快递", "包裹", "轨迹", "到哪里", "到哪")
    )
    asks_after_sale = any(term in normalized for term in ("退款", "退货", "售后"))
    answers: list[str] = []
    if asks_status:
        status = data.get("status")
        status_map = status if isinstance(status, Mapping) else {}
        answers.append(
            "订单状态: "
            f"{_store_status_label('order', status_map.get('order'))}，"
            f"履约{_store_status_label('fulfillment', status_map.get('fulfillment'))}。"
        )
    if asks_logistics:
        shipments = data.get("shipments")
        values = shipments if isinstance(shipments, list) else []
        if not values:
            answers.append("物流: 当前还没有可见包裹，可稍后再查。")
        else:
            first = values[0] if isinstance(values[0], Mapping) else {}
            tracks = first.get("latest_tracks")
            latest = tracks[0] if isinstance(tracks, list) and tracks else {}
            latest_map = latest if isinstance(latest, Mapping) else {}
            shipment_status = _store_status_label("shipment", first.get("shipment_status"))
            location = safe_untrusted_excerpt(latest_map.get("location") or "位置更新中", 80)
            answers.append(f"物流: {shipment_status}，最新位置“{location}”。")
            if any(
                marker in normalized
                for marker in (
                    "为什么",
                    "为何",
                    "没新轨迹",
                    "没有新轨迹",
                    "无新轨迹",
                    "暂无新轨迹",
                    "轨迹停滞",
                    "一直不动",
                )
            ) and first.get("shipment_status") in {"picked_up", "in_transit"}:
                answers.append(
                    "当前没有查到更晚的承运节点，所以页面仍如实显示最后一次轨迹；"
                    "这本身不等于已确认丢件。"
                )
    if asks_after_sale:
        actions = data.get("available_actions")
        can_apply = isinstance(actions, list) and "apply_after_sale" in actions
        answers.append(
            "售后: 当前可以申请，点击订单卡片进入详情后选择“申请售后”。"
            if can_apply
            else "售后: 当前没有可用入口，可点击订单卡片查看具体原因。"
        )
    if not answers:
        return None
    answers.append("点击卡片可查看完整订单，并继续处理可用操作。")
    return "\n".join(answers)


def _is_usage_question(user_text: str) -> bool:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    return any(
        marker in normalized
        for marker in ("适合", "使用场景", "什么场景", "什么用途", "用来做什么", "能干什么")
    )


def _is_product_review_question(user_text: str) -> bool:
    normalized = re.sub(r"\s+", "", user_text).casefold()
    return any(marker in normalized for marker in ("评价", "评分", "口碑", "买家反馈"))


def _render_product_review_answer(
    data: Mapping[str, Any], user_text: str
) -> str | None:
    if not _is_product_review_question(user_text):
        return None
    summary = data.get("review_summary")
    if not isinstance(summary, Mapping):
        return "这件商品暂时没有可核验的公开评价数据。"
    count = int(summary.get("review_count") or 0)
    if count <= 0:
        return "这件商品暂时还没有公开评价。你可以先查看商品参数、款式和实时库存。"
    score = safe_untrusted_excerpt(summary.get("rating_score") or "暂无评分", 20)
    return (
        f"这件商品目前有 {count} 条公开评价，综合评分 {score} 分。"
        "我把有内容的买家反馈整理在下方，点击商品卡片还能查看完整评价。"
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
    material_facts: list[str] = []
    attributes = data.get("attributes")
    if isinstance(attributes, list):
        for item in attributes[:20]:
            if not isinstance(item, Mapping):
                continue
            name = safe_untrusted_excerpt(item.get("name") or item.get("code") or "", 40)
            value = safe_untrusted_excerpt(item.get("value") or "", 120)
            if not name or not value:
                continue
            evidence_values.extend((name, value))
            if any(marker in name for marker in ("面料", "材质", "成分")):
                material_facts.append(f"{name}为{value}")
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
    asks_summer = any(term in normalized_question for term in ("夏天", "夏季"))
    if asks_summer and any(term in evidence for term in ("夏天", "夏季")):
        conclusion = "商家资料明确标注了夏季使用场景，但具体穿着体感仍因人而异。"
    elif asks_summer:
        season_hint = "商品名称标注为秋季款; " if "秋季" in evidence else ""
        conclusion = f"{season_hint}商家资料没有明确说明适合夏天，我不能保证夏季穿着体验。"
    elif requested and all(term in supported for term in requested):
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
    material_note = (
        "公开参数显示: " + "、".join(dict.fromkeys(material_facts)) + "。"
        if material_facts and any(term in normalized_question for term in ("面料", "材质", "成分"))
        else ""
    )
    return (
        f"关于“{product_name}”，{material_note}{conclusion}{known_uses}"
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


_KNOWN_CARRIERS = (
    "京东物流",
    "中国邮政",
    "邮政EMS",
    "顺丰",
    "京东",
    "中通",
    "圆通",
    "申通",
    "韵达",
    "极兔",
    "德邦",
    "菜鸟",
    "丰网",
    "邮政",
)


def _attach_product_fulfillment_facts(data: dict[str, Any], user_text: str) -> None:
    """Extract a focused, trustworthy answer from product detail/OCR text."""

    if not is_product_fulfillment_question(user_text):
        return
    source_text = str(data.get("safe_detail_text") or "")
    compact = re.sub(r"\s+", "", source_text)
    normalized_question = re.sub(r"\s+", "", user_text).casefold()
    asks_dispatch = any(
        marker in normalized_question
        for marker in (
            "几天内发",
            "多久发",
            "多长时间发",
            "什么时候发",
            "何时发",
            "发货时效",
            "付款后几天",
            "付款后多久",
            "下单后几天",
            "下单后多久",
            "拍下后几天",
            "拍下后多久",
        )
    )
    asks_courier = any(
        marker in normalized_question
        for marker in (
            "快递",
            "物流发",
            "用什么物流",
            "走什么物流",
            "默认物流",
            "哪个物流",
            "哪家物流",
        )
    )
    asks_origin = any(
        marker in normalized_question
        for marker in ("从哪里发", "哪里发货", "哪儿发货", "发货地", "从哪发")
    )
    dispatch_origin: str | None = None
    attributes = data.get("attributes")
    if isinstance(attributes, list):
        for attribute in attributes:
            if not isinstance(attribute, Mapping):
                continue
            name = re.sub(r"\s+", "", str(attribute.get("name") or ""))
            value = attribute.get("value")
            if name == "发货地" and isinstance(value, str) and value.strip():
                dispatch_origin = value.strip()
                break
    dispatch_text: str | None = None
    range_match = re.search(
        r"(?:付款后|下单后|拍下后)?(?:一般|通常|预计)?"
        r"(?P<start>\d{1,2})[-—\u2013~\uff5e至到](?P<end>\d{1,2})天内(?:发出|发货)",
        compact,
    )
    if range_match is not None:
        dispatch_text = (
            f"一般 {int(range_match.group('start'))}-{int(range_match.group('end'))} 天内发出"
        )
    else:
        single_match = re.search(
            r"(?:付款后|下单后|拍下后)?(?:一般|通常|预计)?"
            r"(?P<count>\d{1,3})(?P<unit>小时|天)内(?:发出|发货)",
            compact,
        )
        if single_match is not None:
            dispatch_text = f"{int(single_match.group('count'))} {single_match.group('unit')}内发出"
    fulfillment_source = "product_detail_description"
    if dispatch_text is None:
        estimate = data.get("dispatch_estimate")
        if isinstance(estimate, Mapping) and estimate.get("status") == "available":
            as_of = _datetime_value(estimate.get("as_of"))
            min_at = _datetime_value(estimate.get("min_at"))
            max_at = _datetime_value(estimate.get("max_at"))
            if as_of is not None and min_at is not None and max_at is not None:
                min_hours = max(0, round((min_at - as_of).total_seconds() / 3600))
                max_hours = max(min_hours, round((max_at - as_of).total_seconds() / 3600))
                if min_hours % 24 == 0 and max_hours % 24 == 0:
                    min_days = min_hours // 24
                    max_days = max_hours // 24
                    dispatch_text = (
                        f"预计 {min_days}-{max_days} 天内发出"
                        if min_days != max_days
                        else f"预计 {min_days} 天内发出"
                    )
                else:
                    dispatch_text = (
                        f"预计 {min_hours}-{max_hours} 小时内发出"
                        if min_hours != max_hours
                        else f"预计 {min_hours} 小时内发出"
                    )
                fulfillment_source = "store_fulfillment_profile"
    carrier = next(
        (
            item
            for item in _KNOWN_CARRIERS
            if re.search(rf"默认(?:使用|发|走)?{re.escape(item)}(?:快递|物流)?", compact)
        ),
        None,
    )
    data["product_fulfillment_facts"] = {
        "asks_dispatch": asks_dispatch,
        "asks_courier": asks_courier,
        "asks_origin": asks_origin,
        "dispatch_text": dispatch_text,
        "dispatch_caveat": (
            "具体发货时间以拍下页面为准"
            if "具体发货时间以拍下页面为准" in compact or "具体发货时间以拍下页为准" in compact
            else None
        ),
        "carrier": carrier,
        "dispatch_origin": dispatch_origin,
        "alternative_courier_requires_contact": bool(
            carrier and ("其他快递请联系客服" in compact or "其它快递请联系客服" in compact)
        ),
        "source": fulfillment_source,
    }


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _render_product_fulfillment_answer(data: Mapping[str, Any]) -> str | None:
    value = data.get("product_fulfillment_facts")
    if not isinstance(value, Mapping):
        return None
    answers: list[str] = []
    if value.get("asks_dispatch") is True:
        dispatch_text = value.get("dispatch_text")
        if isinstance(dispatch_text, str):
            caveat = "，具体发货时间以拍下页面为准" if value.get("dispatch_caveat") else ""
            source_prefix = (
                "店铺当前履约资料显示，付款后"
                if value.get("source") == "store_fulfillment_profile"
                else "商品详情写明，付款后"
            )
            answers.append(f"{source_prefix}{dispatch_text}{caveat}。")
        else:
            answers.append("商品详情暂未写明具体发货天数，请以下单页面显示为准。")
    if value.get("asks_courier") is True:
        carrier = value.get("carrier")
        if isinstance(carrier, str):
            suffix = (
                "，如需其他快递，请在下单前联系店铺客服确认"
                if value.get("alternative_courier_requires_contact")
                else ""
            )
            answers.append(f"商品详情写明，本店默认使用{carrier}发货{suffix}。")
        else:
            answers.append("商品详情暂未写明默认快递，请在下单前联系店铺客服确认。")
    if value.get("asks_origin") is True:
        origin = value.get("dispatch_origin")
        answers.append(
            f"当前商品标注的发货地是{origin}。"
            if isinstance(origin, str)
            else "当前商品资料暂未标注发货地，请在下单前联系店铺客服确认。"
        )
    return "\n\n".join(answers) if answers else None


def _asks_store_human_service_capabilities(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    has_human = any(term in normalized for term in ("人工客服", "店铺客服", "人工", "真人"))
    asks_information = any(
        term in normalized
        for term in ("能处理什么", "可以处理什么", "能做什么", "可以做什么", "服务范围")
    )
    return has_human and asks_information


def _store_detail_cards(
    plan: StoreAgentPlan,
    data: Mapping[str, Any],
    user_text: str = "",
) -> list[dict[str, Any]]:
    comparison = data.get("comparison")
    if (
        plan.intent == "product_recommend"
        and data.get("comparison_requested") is True
        and isinstance(comparison, list)
    ):
        return _store_detail_cards(
            StoreAgentPlan("product_compare"),
            {"items": comparison},
            user_text,
        )
    product_no = data.get("product_id")
    product_action = (
        {"resource_type": "product", "resource_id": product_no, "label": "打开商品详情"}
        if isinstance(product_no, str)
        else None
    )
    normalized_text = re.sub(r"\s+", "", user_text).casefold()
    if plan.intent == "after_sale_progress":
        refund_values = data.get("items")
        return [
            {
                "kind": "store_after_sale_progress",
                "icon": "售",
                "eyebrow": "本店售后进度",
                "title": safe_untrusted_excerpt(item.get("product_name") or "售后申请", 100),
                "badge": _store_status_label("refund", item.get("refund_status")),
                "summary": "状态来自当前用户在本店提交的真实售后申请。",
                "rows": [
                    {
                        "label": "申请金额",
                        "value": _money_value(item.get("requested_amount")),
                        "meta": safe_untrusted_excerpt(
                            item.get("reason_detail") or item.get("reason_code") or "未填写原因",
                            100,
                        ),
                    },
                    {
                        "label": "提交时间",
                        "value": str(item.get("submitted_at") or "—"),
                        "meta": "售后编号已保存在审计记录中",
                    },
                ],
                "action": {
                    "resource_type": "refund",
                    "resource_id": item.get("refund_id"),
                    "label": "查看售后详情",
                },
            }
            for item in (refund_values if isinstance(refund_values, list) else [])[:5]
            if isinstance(item, Mapping) and isinstance(item.get("refund_id"), str)
        ]
    review_summary = data.get("review_summary")
    if _is_product_review_question(user_text) and isinstance(review_summary, Mapping):
        samples_value = review_summary.get("samples")
        rows = [
            {
                "label": f"{int(sample.get('rating') or 0)} 星评价",
                "value": safe_untrusted_excerpt(sample.get("content") or "未填写文字", 90),
                "meta": "已公开",
            }
            for sample in (samples_value if isinstance(samples_value, list) else [])[:3]
            if isinstance(sample, Mapping)
        ]
        if not rows:
            rows = [
                {
                    "label": "公开评价",
                    "value": "暂无文字评价",
                    "meta": "可稍后再看",
                }
            ]
        return [
            {
                "kind": "review_summary",
                "icon": "评",
                "eyebrow": "买家公开评价",
                "title": safe_untrusted_excerpt(data.get("name") or "当前商品", 120),
                "badge": f"{review_summary.get('rating_score') or '—'} 分",
                "summary": f"共 {int(review_summary.get('review_count') or 0)} 条公开评价。",
                "rows": rows,
                "action": product_action,
            }
        ]
    fulfillment = data.get("product_fulfillment_facts")
    if isinstance(fulfillment, Mapping):
        fulfillment_rows: list[dict[str, object]] = []
        if fulfillment.get("asks_dispatch") is True:
            fulfillment_rows.append(
                {
                    "label": "付款后发货",
                    "value": safe_untrusted_excerpt(
                        fulfillment.get("dispatch_text") or "商品详情未明确", 50
                    ),
                    "meta": safe_untrusted_excerpt(
                        fulfillment.get("dispatch_caveat") or "以下单页面为准", 80
                    ),
                }
            )
        if fulfillment.get("asks_courier") is True:
            fulfillment_rows.append(
                {
                    "label": "默认快递",
                    "value": safe_untrusted_excerpt(
                        fulfillment.get("carrier") or "商品详情未明确", 50
                    ),
                    "meta": (
                        "如需其他快递，请先联系店铺客服"
                        if fulfillment.get("alternative_courier_requires_contact")
                        else "下单前可向店铺客服确认"
                    ),
                }
            )
        if fulfillment.get("asks_origin") is True:
            fulfillment_rows.append(
                {
                    "label": "发货地",
                    "value": safe_untrusted_excerpt(
                        fulfillment.get("dispatch_origin") or "商品资料未明确", 50
                    ),
                    "meta": "以商品页当前公开资料为准",
                }
            )
        return [
            {
                "kind": "product_fulfillment",
                "icon": "寄",
                "eyebrow": "商品发货说明",
                "title": safe_untrusted_excerpt(data.get("name") or "当前商品", 120),
                "badge": "商品详情",
                "summary": "已读取商家填写的商品详情图片说明，实际安排以下单页和店铺确认为准。",
                "rows": fulfillment_rows,
                "action": product_action,
            }
        ]
    projection = data.get("variant_quantity_projection")
    if isinstance(projection, Mapping):
        total_units = projection.get("total_units")
        return [
            {
                "kind": "purchase_projection",
                "icon": "算",
                "eyebrow": "购买试算",
                "title": safe_untrusted_excerpt(projection.get("sku_name") or "当前款式", 80),
                "badge": "未下单",
                "summary": "按当前商品价格试算，实际金额与库存以结算页为准。",
                "rows": [
                    {
                        "label": "购买数量",
                        "value": f"{projection.get('purchase_count', 0)} 盒",
                        "meta": f"共 {total_units} 支" if isinstance(total_units, int) else "",
                    },
                    {
                        "label": "商品金额",
                        "value": str(projection.get("total_price") or "¥0.00"),
                        "meta": f"单盒 {projection.get('unit_price', '¥0.00')}",
                    },
                ],
                "action": product_action,
            }
        ]
    focused_variant = data.get("focused_variant")
    if isinstance(focused_variant, Mapping):
        label = safe_untrusted_excerpt(
            focused_variant.get("sku_name") or focused_variant.get("name") or "最大包装",
            80,
        )
        availability = _availability_label(focused_variant)
        quantity = focused_variant.get("available_quantity")
        meta = availability
        if isinstance(quantity, int):
            meta += f" · 可售 {max(0, quantity)} 件"
        return [
            {
                "kind": "sku_focus",
                "icon": "款",
                "eyebrow": "已核实款式",
                "title": safe_untrusted_excerpt(
                    data.get("product_name") or data.get("name") or "当前商品", 120
                ),
                "badge": (
                    "最大包装"
                    if data.get("focused_variant_reason") == "largest_package"
                    else "体重参考"
                    if data.get("focused_variant_reason") == "body_weight_reference"
                    else "指定款式"
                ),
                "summary": (
                    "按商品已标注的体重范围筛选，只作为选码参考，不保证一定合身。"
                    if data.get("focused_variant_reason") == "body_weight_reference"
                    else "只展示本次问题对应的款式，价格与库存来自实时商品数据。"
                ),
                "rows": [
                    {
                        "label": label,
                        "value": _money_value(focused_variant.get("price")),
                        "meta": meta,
                    }
                ],
                "action": product_action,
            }
        ]
    sku_values = data.get("skus")
    asks_variant_facts = any(
        term in normalized_text
        for term in ("款式", "规格", "型号", "颜色", "尺码", "价格", "库存", "有货")
    )
    if plan.intent == "product_qa" and asks_variant_facts and isinstance(sku_values, list):
        rows = []
        for item in sku_values[:8]:
            if not isinstance(item, Mapping):
                continue
            quantity = item.get("available_quantity")
            stock_text = _availability_label(item)
            if isinstance(quantity, int):
                stock_text += f" · 可售 {max(0, quantity)} 件"
            specifications = _sku_specification_text(item.get("specifications"))
            if specifications:
                stock_text = f"{specifications} · {stock_text}"
            rows.append(
                {
                    "label": safe_untrusted_excerpt(
                        item.get("sku_name") or item.get("name") or "默认款式", 80
                    ),
                    "value": _money_value(item.get("price")),
                    "meta": stock_text,
                }
            )
        if rows:
            return [
                {
                    "kind": "product_variants",
                    "icon": "款",
                    "eyebrow": "款式与实时库存",
                    "title": safe_untrusted_excerpt(data.get("name") or "当前商品", 120),
                    "badge": f"{len(rows)} 个款式",
                    "summary": "价格与可售数量来自当前商品数据，最终以结算页为准。",
                    "rows": rows,
                    "action": product_action,
                }
            ]
    asks_logistics = any(
        term in normalized_text for term in ("物流", "快递", "包裹", "到哪里", "到哪")
    )
    if plan.intent == "order_explain" and isinstance(data.get("order_id"), str) and asks_logistics:
        shipment_values = data.get("shipments")
        shipment_rows: list[dict[str, object]] = []
        for shipment in (shipment_values if isinstance(shipment_values, list) else [])[:3]:
            if not isinstance(shipment, Mapping):
                continue
            tracks = shipment.get("latest_tracks")
            latest = tracks[0] if isinstance(tracks, list) and tracks else {}
            latest_map = latest if isinstance(latest, Mapping) else {}
            shipment_rows.append(
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
        if shipment_rows:
            return [
                {
                    "kind": "store_order_logistics",
                    "icon": "运",
                    "eyebrow": "本店订单物流",
                    "title": "包裹最新进度",
                    "badge": "已核验",
                    "summary": "轨迹来自当前订单的最近一次物流同步。",
                    "rows": shipment_rows,
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
                    f"{_availability_label(item)}"
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
            sku_specifications = item.get("specifications")
            specification_text = _sku_specification_text(sku_specifications)
            available_quantity = item.get("available_quantity")
            availability = _availability_label(item)
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
        knowledge = data.get("knowledge_sources")
        normalized_policy_question = re.sub(r"\s+", "", user_text).casefold()
        has_platform_after_sale_source = isinstance(knowledge, list) and any(
            isinstance(item, Mapping)
            and item.get("scope") == "platform:platform"
            and any(
                marker in str(item.get("title") or "")
                for marker in ("售后", "退款", "客服规则")
            )
            for item in knowledge
        )
        if not rows and has_platform_after_sale_source and any(
            marker in normalized_policy_question
            for marker in ("退换", "换货", "质量问题", "商品质量")
        ):
            rows = [
                {
                    "label": "质量问题处理",
                    "value": "从对应订单发起售后",
                    "meta": "请补充问题说明并保留商品问题图片等凭证",
                },
                {
                    "label": "退货或退款资格",
                    "value": "提交前实时检查",
                    "meta": "以订单状态、可退款数量、历史售后和当前规则为准",
                },
            ]
        if not rows and isinstance(knowledge, list):
            rows = [
                {
                    "label": safe_untrusted_excerpt(
                        item.get("title") or "公开服务规则", 80
                    ),
                    "value": (
                        "平台公开规则"
                        if item.get("scope") == "platform:platform"
                        else "本店公开资料"
                    ),
                    "meta": safe_untrusted_excerpt(item.get("excerpt") or "", 220),
                }
                for item in knowledge
                if isinstance(item, Mapping)
                and _policy_source_matches_question(item, user_text)
                and (
                    item.get("scope") == "platform:platform"
                    or "政策" in str(item.get("title") or "")
                    or "规则" in str(item.get("title") or "")
                )
            ][:3]
        if rows:
            return [
                {
                    "kind": "store_policy",
                    "icon": "规",
                    "eyebrow": "店铺服务政策",
                    "title": "本次回答依据",
                    "badge": "已核验",
                    "summary": (
                        "优先使用本店公开政策；本店未发布额外规则时，使用平台当前公开规则。"
                    ),
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


def _policy_source_matches_question(
    source: Mapping[str, object], question: str
) -> bool:
    normalized_question = re.sub(r"\s+", "", question).casefold()
    source_text = re.sub(
        r"\s+",
        "",
        f"{source.get('title') or ''} {source.get('excerpt') or ''}",
    ).casefold()
    topic_groups = (
        (
            ("退换", "换货", "退款", "售后", "质量"),
            ("退换", "换货", "退款", "售后", "质量"),
        ),
        (
            ("包邮", "运费", "配送", "邮寄", "快递", "发货"),
            ("包邮", "运费", "配送", "邮寄", "快递", "发货", "物流"),
        ),
        (
            ("支付", "余额", "充值"),
            ("支付", "余额", "充值"),
        ),
    )
    matched_group = False
    for question_terms, source_terms in topic_groups:
        if not any(term in normalized_question for term in question_terms):
            continue
        matched_group = True
        if any(term in source_text for term in source_terms):
            return True
    return not matched_group


def _compact_store_tracking_no(value: object) -> str:
    text = safe_untrusted_excerpt(value or "物流单号待更新", 80)
    if text.count("*") >= 6:
        suffix = text.rstrip("*")[-4:] if not text.endswith("*") else ""
        return f"尾号 {suffix}" if suffix else "物流单号已脱敏"
    return text


def _sku_specification_text(value: object) -> str:
    parts: list[str] = []
    if isinstance(value, Mapping):
        for raw_name, raw_detail in list(value.items())[:4]:
            name = safe_untrusted_excerpt(raw_name or "规格", 30)
            detail = safe_untrusted_excerpt(raw_detail or "", 50)
            if detail:
                parts.append(f"{name}: {detail}")
        return " / ".join(parts)
    if not isinstance(value, list):
        return ""
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
    requested_weight_match = re.search(r"(?<!\d)(\d{2,3})(?:\.\d+)?斤", normalized)
    labelled_limits: list[tuple[int, str]] = []
    if requested_weight_match is not None:
        requested_weight = int(requested_weight_match.group(1))
        for item in skus[:20]:
            if not isinstance(item, Mapping):
                continue
            sku_name = safe_untrusted_excerpt(item.get("sku_name"), 160)
            for raw_limit in re.findall(r"(?<!\d)(\d{2,3})(?:\.\d+)?斤以下", sku_name):
                labelled_limits.append((int(raw_limit), sku_name))
        if labelled_limits:
            max_limit = max(limit for limit, _name in labelled_limits)
            if requested_weight > max_limit:
                max_names = list(
                    dict.fromkeys(name for limit, name in labelled_limits if limit == max_limit)
                )
                product_name = safe_untrusted_excerpt(data.get("name") or "当前商品", 120)
                examples = "、".join(max_names[:2])
                return (
                    f"{product_name}当前公开款式最高只标注到 {max_limit} 斤以下"
                    f"({examples})。你说的 {requested_weight} 斤已经超出商品标注范围，"
                    "我无法确认有明确合适的尺码，也不建议仅凭体重直接下单; "
                    "请再核对商品尺寸数据或让人工客服确认。"
                )
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
    query = context.trigger.text_content or "店铺公开信息"
    knowledge_service = KnowledgeService(mysql, checkpoint_store.session)
    try:
        store_result = await knowledge_service.search_for_agent(
            query=query,
            scope_type="store",
            scope_no=context.store.store_no,
            limit=6,
            trace_id=context.run.trace_id,
        )
        # A store may not publish an extra return/after-sale policy.  Policy
        # questions may then use the platform's *public* rules as a fallback,
        # while product questions remain strictly inside the current store.
        platform_result = (
            await knowledge_service.search_for_agent(
                query=query,
                scope_type="platform",
                scope_no="platform",
                limit=8,
                trace_id=context.run.trace_id,
            )
            if intent == "policy_qa"
            else None
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
    sources = [
        {
            "document_id": item.document_id,
            "title": item.title,
            "version": item.content_version,
            "excerpt": item.excerpt,
            "score": round(item.score, 6),
            "scope": f"store:{context.store.store_no}",
        }
        for item in store_result.items
    ]
    if platform_result is not None:
        sources.extend(
            {
                "document_id": item.document_id,
                "title": item.title,
                "version": item.content_version,
                "excerpt": item.excerpt,
                "score": round(item.score, 6),
                "scope": "platform:platform",
            }
            for item in platform_result.items
        )
    data["knowledge_sources"] = sources
    scopes = [
        {
            "scope": f"store:{context.store.store_no}",
            "returned_count": len(store_result.items),
            "degraded": store_result.degraded,
            "retrieval_mode": "keyword_only" if store_result.degraded else "hybrid",
        }
    ]
    if platform_result is not None:
        scopes.append(
            {
                "scope": "platform:platform",
                "returned_count": len(platform_result.items),
                "degraded": platform_result.degraded,
                "retrieval_mode": (
                    "keyword_only" if platform_result.degraded else "hybrid"
                ),
            }
        )
    data["rag"] = {
        "scope": f"store:{context.store.store_no}",
        "scopes": scopes,
        "returned_count": len(sources),
        "degraded": any(bool(item["degraded"]) for item in scopes),
        "retrieval_mode": (
            "keyword_only"
            if any(bool(item["degraded"]) for item in scopes)
            else "hybrid"
        ),
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
    "refund": {
        "draft": "草稿",
        "submitted": "待商家处理",
        "merchant_review": "商家处理中",
        "approved": "已同意",
        "rejected": "已拒绝",
        "return_pending": "待寄回",
        "return_in_transit": "退货运输中",
        "return_received": "商家已收货",
        "refund_processing": "退款处理中",
        "succeeded": "退款成功",
        "cancelled": "已取消",
        "closed": "已关闭",
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


def _store_cart_card(data: Mapping[str, Any]) -> dict[str, object]:
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


def _availability_label(value: Mapping[str, Any]) -> str:
    quantity = value.get("available_quantity")
    fallback = "有货" if isinstance(quantity, int) and quantity > 0 else "缺货"
    return safe_untrusted_excerpt(value.get("availability_label") or fallback, 40)


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
