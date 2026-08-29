from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.modules.agent_runtime.approval_service import AgentApprovalService
from app.modules.agent_runtime.checkpoints import AgentCheckpointStore
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
from app.modules.agent_runtime.model_gateway import ModelGatewayError
from app.modules.agent_runtime.models import AgentRun, AgentToolApproval
from app.modules.agent_runtime.prompt_safety import detects_prompt_injection, safe_untrusted_excerpt
from app.modules.agent_runtime.provider_gateway import ProviderExclusiveModelGateway
from app.modules.agent_runtime.store_agent import _stream_events
from app.modules.agent_runtime.store_tools import StoreToolResult
from app.modules.content.models import PlatformContentEntry, PlatformContentVersion
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
    trigger_text = context.trigger.text_content or ""
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

    try:
        plan = await (model_gateway or DeterministicExclusiveModelGateway()).plan(trigger_text)
    except (ModelGatewayError, TimeoutError):
        await _handoff(session, context, settings, security, "MODEL_UNAVAILABLE")
        await _finish_checkpoint(checkpoint_store, context, "human_handoff")
        return
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
        if plan.intent == "policy_qa":
            result = await _platform_policy(session, context)
        elif plan.intent in {"product_search", "personalized_recommendation"}:
            result = await tools.search_products(context, plan.search_text)
        elif plan.intent == "order_lookup":
            ref = context.context_refs.get("order")
            result = (
                await tools.order_detail(
                    context, (await builder.require_active_context(context, "order")).resource_no
                )
                if ref is not None
                else await tools.list_orders(context)
            )
        elif plan.intent == "logistics_lookup":
            ref = await builder.require_active_context(context, "order")
            result = await tools.shipments(context, ref.resource_no)
        elif plan.intent == "refund_progress":
            ref = context.context_refs.get("refund")
            result = (
                await tools.refund_detail(
                    context, (await builder.require_active_context(context, "refund")).resource_no
                )
                if ref is not None
                else await tools.list_refunds(context)
            )
        else:
            ref = await builder.require_active_context(context, "order")
            approval_service = AgentApprovalService(session, settings, security)

            async def build_draft() -> dict[str, object]:
                return await approval_service.build_refund_draft(
                    context,
                    ref.resource_no,
                    context.trigger.text_content or "申请退款",
                )

            result = await tools.execute(
                context,
                "after_sale.build_refund_draft",
                {"order_id": ref.resource_no},
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
        await _complete(
            session,
            context,
            "当前选择的订单或售后上下文已变化，请重新从对应详情页选择后再试。",
            error_code=exc.code,
            degraded_reason="context_unavailable",
        )
        await _finish_checkpoint(checkpoint_store, context, plan.intent)
        return

    if result.status == "succeeded":
        await _attach_platform_knowledge(
            session,
            checkpoint_store,
            context,
            plan.intent,
            result.data,
        )
        answer, trace = await _grounded_answer(context, model_gateway, plan, result.data)
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
            "已为你转接平台人工客服，请留意排队状态。",
            data=result.data,
            degraded_reason=(
                None if reason_code == "USER_REQUESTED_HUMAN" else reason_code.casefold()
            ),
        )
    else:
        await _complete(
            session,
            context,
            "平台智能客服暂时不可用，请稍后点击“转人工客服”重试。",
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
) -> None:
    now = utc_now()
    conversation = context.conversation
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
            "execution_trace": dict(execution_trace or {}),
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
) -> tuple[str, dict[str, object]]:
    fallback = _render(plan, data)
    tool_code = _tool_for_intent(plan.intent)
    sources = _source_refs(data)
    source_ids = tuple(
        f"{item['type']}:{item['id']}"
        for item in sources
        if isinstance(item.get("type"), str) and isinstance(item.get("id"), str)
    ) or (f"tool:{tool_code}",)
    steps: list[dict[str, object]] = [
        {"kind": "plan", "label": "理解问题", "status": "completed"},
        {
            "kind": "tool",
            "label": "查询用户范围内的可信数据",
            "tool_code": tool_code,
            "status": "completed",
        },
        {"kind": "answer", "label": "生成证据约束回复", "status": "completed"},
    ]
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
    trace: dict[str, object] = {
        "version": "public-agent-trace-v1",
        "run_id": context.run.run_no,
        "agent": "专属客服",
        "model": context.agent_version.model_profile,
        "status": "completed",
        "intent": plan.intent,
        "steps": steps,
        "source_ids": list(source_ids),
        "raw_reasoning_exposed": False,
    }
    if not isinstance(gateway, ProviderExclusiveModelGateway):
        trace["answer_mode"] = "deterministic_fallback"
        return fallback, trace
    context.run.current_phase = "answering"
    context.run.version += 1
    try:
        answer = await gateway.synthesize(
            agent_prompt=context.agent_version.system_prompt,
            user_text=context.trigger.text_content or "",
            intent=plan.intent,
            evidence=data,
            source_ids=source_ids,
        )
    except (ModelGatewayError, TimeoutError):
        trace["answer_mode"] = "deterministic_fallback"
        trace["degraded_reason"] = "answer_model_unavailable"
        context.run.degraded_reason = "answer_model_unavailable"
        return fallback, trace
    trace["answer_mode"] = "model_grounded"
    trace["confidence"] = answer.confidence
    trace["cited_source_ids"] = list(answer.cited_source_ids)
    if answer.limitation:
        trace["limitation"] = answer.limitation
    return answer.text, trace


def _tool_for_intent(intent: str) -> str:
    return {
        "human_handoff": "support.create_platform_ticket",
        "policy_qa": "rag.policy.search",
        "product_search": "catalog.search_products",
        "personalized_recommendation": "catalog.search_products",
        "order_lookup": "order.list_user_orders",
        "logistics_lookup": "logistics.get_user_order_shipments",
        "refund_eligibility": "after_sale.build_refund_draft",
        "refund_progress": "after_sale.list_user_refunds",
    }.get(intent, "unknown")


def _render(plan: ExclusiveAgentPlan, data: Mapping[str, Any]) -> str:
    items = data.get("items")
    if plan.intent in {"product_search", "personalized_recommendation"}:
        if not isinstance(items, list) or not items:
            return "暂未找到符合当前条件的公开在售商品。你可以补充品类、用途或预算。"
        lines = ["找到这些公开在售商品候选:"]
        for item in items[:8]:
            if isinstance(item, dict):
                price_value = item.get("price")
                price: Mapping[str, Any] = price_value if isinstance(price_value, dict) else {}
                lines.append(
                    f"- {safe_untrusted_excerpt(item.get('name'), 120)} "
                    f"({safe_untrusted_excerpt(item.get('store_name'), 120)}): "
                    f"{price.get('currency', 'CNY')} {int(price.get('min_amount', 0)) / 100:.2f} 起"
                )
        lines.append("价格与库存以商品详情和结算页实时结果为准。")
        return "\n".join(lines)
    if plan.intent == "order_lookup":
        if "order_id" in data:
            status_value = data.get("status")
            status: Mapping[str, Any] = status_value if isinstance(status_value, dict) else {}
            return (
                f"订单 {data.get('order_id')} 当前状态: 订单 {status.get('order')}，"
                f"支付 {status.get('payment')}，履约 {status.get('fulfillment')}，"
                f"售后 {status.get('after_sale')}。可用操作以订单详情页为准。"
            )
        if not isinstance(items, list) or not items:
            return "你的账号下暂未查询到可见订单。"
        return "最近订单:\n" + "\n".join(
            f"- {item.get('order_id')} ({safe_untrusted_excerpt(item.get('store_name'), 120)}): "
            f"{_nested_value(item, 'status', 'order')}"
            for item in items
            if isinstance(item, dict)
        )
    if plan.intent == "logistics_lookup":
        if not isinstance(items, list) or not items:
            return "该订单当前没有可见物流包裹。"
        return "订单物流包裹:\n" + "\n".join(
            f"- {item.get('carrier_name')} {item.get('tracking_no_masked')}: "
            f"{item.get('shipment_status')}{_delivery_estimate_text(item)}"
            for item in items
            if isinstance(item, dict)
        )
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
        lines = ["根据当前已发布平台规则:"]
        lines.extend(
            f"- {safe_untrusted_excerpt(item.get('title'), 160)} "
            f"(版本 {item.get('version')}): {safe_untrusted_excerpt(item.get('content'), 500)}"
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict)
        )
        lines.extend(
            f"- {safe_untrusted_excerpt(item.get('title'), 160)} "
            f"(知识版本 {item.get('version')}): "
            f"{safe_untrusted_excerpt(item.get('excerpt'), 500)}"
            for item in (knowledge if isinstance(knowledge, list) else [])
            if isinstance(item, dict)
        )
        return "\n".join(lines)
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


def _nested_value(value: Mapping[str, Any], outer: str, inner: str) -> object:
    nested = value.get(outer)
    return nested.get(inner) if isinstance(nested, dict) else None


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
