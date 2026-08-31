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
)
from app.modules.agent_runtime.exclusive_tools import ExclusiveToolGateway
from app.modules.agent_runtime.memory_runtime import AgentMemoryRuntime, explicit_memory_request
from app.modules.agent_runtime.model_gateway import ModelGatewayError
from app.modules.agent_runtime.models import AgentRun, AgentToolApproval
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
    fast_plan = await DeterministicExclusiveModelGateway().plan(trigger_text)
    if fast_plan.intent != "general_chat" or not isinstance(gateway, ProviderExclusiveModelGateway):
        plan = fast_plan
    else:
        planning_input = context_window.planning_input(trigger_text)
        try:
            plan = await gateway.plan(planning_input)
        except (ModelGatewayError, TimeoutError) as exc:
            plan = fast_plan
            run.degraded_reason = model_failure_code(exc, "planning")
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
            result = await tools.search_products(
                context,
                plan.search_text,
                fallback_query=trigger_text,
            )
        elif plan.intent == "order_lookup":
            explicit_order_no = _resource_no(trigger_text, "ord")
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
            ref = (
                None
                if explicit_order_no is not None
                else await builder.require_active_context(context, "order")
            )
            order_no = explicit_order_no or (ref.resource_no if ref is not None else "")
            approval_service = AgentApprovalService(session, settings, security)

            async def build_draft() -> dict[str, object]:
                return await approval_service.build_refund_draft(
                    context,
                    order_no,
                    context.trigger.text_content or "申请退款",
                )

            result = await tools.execute(
                context,
                "after_sale.build_refund_draft",
                {"order_id": order_no},
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
        _attach_conversation_window(context_window, result.data)
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
        await _complete(
            session,
            context,
            answer,
            data=result.data,
            execution_trace=trace,
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
    )
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
        "personalized_recommendation": "catalog.search_products",
        "order_lookup": "order.list_user_orders",
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
    explicit_order_no = _resource_no(trigger_text, "ord")
    if explicit_order_no is not None:
        return explicit_order_no
    if _requests_latest_order(trigger_text) or context.context_refs.get("order") is None:
        return await tools.latest_order_no(context)
    return (await builder.require_active_context(context, "order")).resource_no


def _requests_latest_order(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return any(
        marker in normalized
        for marker in ("最近订单", "最近一笔", "最新订单", "上一笔订单", "刚买", "刚下单")
    )


def _render(plan: ExclusiveAgentPlan, data: Mapping[str, Any], user_text: str = "") -> str:
    if plan.intent == "general_chat":
        return "你好，我是你的专属客服。你可以问我平台规则、商品推荐、本人订单、物流或售后问题。"
    items = data.get("items")
    if plan.intent in {"product_search", "personalized_recommendation"}:
        if not isinstance(items, list) or not items:
            return "暂未找到符合当前条件的公开在售商品。你可以补充品类、用途或预算。"
        lines: list[str] = []
        memories = data.get("recalled_memories")
        if isinstance(memories, list) and memories:
            remembered = [
                safe_untrusted_excerpt(item.get("value"), 160)
                for item in memories[:3]
                if isinstance(item, dict)
            ]
            if remembered:
                lines.append("根据你之前允许我记住的偏好: " + "、".join(remembered) + "。")
        lines.append("找到这些公开在售商品候选:")
        for item in items[:8]:
            if isinstance(item, dict):
                price_value = item.get("price")
                price: Mapping[str, Any] = price_value if isinstance(price_value, dict) else {}
                lines.append(
                    f"- {safe_untrusted_excerpt(item.get('name'), 120)} "
                    f"({safe_untrusted_excerpt(item.get('store_name'), 120)}): "
                    f"{_price_display(price)} 起，"
                    f"可售库存 {max(0, int(item.get('available_stock', 0)))}"
                )
                skus = item.get("skus")
                for sku in skus[:12] if isinstance(skus, list) else []:
                    if not isinstance(sku, dict):
                        continue
                    sku_price = sku.get("price")
                    display = (
                        sku_price.get("display") if isinstance(sku_price, dict) else "价格待核对"
                    )
                    lines.append(
                        f"  - {safe_untrusted_excerpt(sku.get('sku_name'), 120)}: "
                        f"{display}，实时可售 {max(0, int(sku.get('available_stock', 0)))} 件，"
                        f"{safe_untrusted_excerpt(sku.get('availability_label'), 40)}"
                    )
        lines.append("推荐依据: 按当前公开销量排序; 价格与库存以商品详情和结算页实时结果为准。")
        return "\n".join(lines)
    if plan.intent == "order_lookup":
        if "order_id" in data:
            status_value = data.get("status")
            status: Mapping[str, Any] = status_value if isinstance(status_value, dict) else {}
            paid = _nested_value(data, "amounts", "paid")
            paid_display = paid.get("display") if isinstance(paid, dict) else None
            store_name = safe_untrusted_excerpt(data.get("store_name"), 120)
            return (
                f"订单 {data.get('order_id')} ({store_name})，"
                f"实付 {paid_display or '¥0.00'}。当前状态: "
                f"订单{_status_label('order', status.get('order'))}，"
                f"支付{_status_label('payment', status.get('payment'))}，"
                f"履约{_status_label('fulfillment', status.get('fulfillment'))}，"
                f"售后{_status_label('after_sale', status.get('after_sale'))}。"
                "可用操作以订单详情页为准。"
            )
        if not isinstance(items, list) or not items:
            return "你的账号下暂未查询到可见订单。"
        return "最近订单:\n" + "\n".join(
            f"- {item.get('order_id')} ({safe_untrusted_excerpt(item.get('store_name'), 120)}): "
            f"实付 {_money_display(item, 'paid')}，"
            f"{_status_label('order', _nested_value(item, 'status', 'order'))}"
            for item in items
            if isinstance(item, dict)
        )
    if plan.intent == "logistics_lookup":
        if not isinstance(items, list) or not items:
            return "该订单当前没有可见物流包裹。"
        return "订单物流包裹:\n" + "\n".join(
            f"- {safe_untrusted_excerpt(item.get('carrier_name'), 80)}，"
            f"物流单号 {item.get('tracking_no_masked')}: "
            f"{_status_label('shipment', item.get('shipment_status'))}"
            f"{_last_track_text(item)}{_delivery_estimate_text(item)}"
            for item in items
            if isinstance(item, dict)
        )
    if plan.intent == "refund_precheck":
        eligibility_value = data.get("refund_eligibility")
        eligibility: Mapping[str, Any] = (
            eligibility_value if isinstance(eligibility_value, Mapping) else {}
        )
        status_value = data.get("status")
        order_status: Mapping[str, Any] = status_value if isinstance(status_value, Mapping) else {}
        eligible = eligibility.get("eligible") is True
        lines = [
            f"订单 {data.get('order_id')} 当前订单状态为"
            f"{_status_label('order', order_status.get('order'))}，支付"
            f"{_status_label('payment', order_status.get('payment'))}，履约"
            f"{_status_label('fulfillment', order_status.get('fulfillment'))}。",
            "售后资格预检结果: " + ("当前具备申请资格。" if eligible else "当前不具备申请资格。"),
        ]
        suggested = eligibility.get("suggested_refund_amount")
        if eligible and isinstance(suggested, Mapping):
            lines.append(f"当前建议可申请金额为 {_money_object_display(suggested)}。")
        allowed = eligibility.get("allowed_types")
        if eligible and isinstance(allowed, list):
            labels = {"refund_only": "仅退款", "return_and_refund": "退货退款"}
            lines.append(
                "可选类型: "
                + "、".join(labels.get(str(value), str(value)) for value in allowed)
                + "。"
            )
        blocking = eligibility.get("blocking_reasons")
        if not eligible and isinstance(blocking, list) and blocking:
            lines.append("阻断原因: " + "、".join(str(value) for value in blocking) + "。")
        shipment_values = data.get("shipments")
        shipments = shipment_values if isinstance(shipment_values, list) else []
        if shipments and isinstance(shipments[0], Mapping):
            latest = shipments[0]
            lines.append(
                "当前物流: "
                f"{_status_label('shipment', latest.get('shipment_status'))}"
                f"{_last_track_text(latest)}。"
            )
        lines.append(
            "本次只完成资格检查，没有创建退款草稿或售后单。"
            "如果你要继续申请，请明确告诉我申请类型、原因和数量。提交前仍需授权并再次确认。"
        )
        return "\n".join(lines)
    if plan.intent == "refund_progress":
        if "refund_id" in data:
            return f"售后单 {data.get('refund_id')} 当前状态: {data.get('refund_status')}。"
        if not isinstance(items, list) or not items:
            return "你的账号下暂未查询到售后申请。"
        return "最近售后:\n" + "\n".join(
            f"- {item.get('refund_id')}: {item.get('refund_status')}"
            for item in items
            if isinstance(item, dict)
        )
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


def _attach_conversation_window(window: ContextWindow, data: dict[str, object]) -> None:
    if window.recent_turns:
        data["conversation_window"] = window.evidence_projection()


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
