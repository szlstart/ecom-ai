from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.pagination import CursorCodec
from app.core.security import SecurityService, utc_now
from app.modules.after_sale.models import (
    RefundAppeal,
    RefundAppealEvent,
    RefundApplication,
    RefundEvent,
    RefundItem,
    RefundPaymentEvent,
    RefundPaymentRecord,
    RefundShipment,
)
from app.modules.agent_runtime.checkpoints import AgentCheckpointStore
from app.modules.agent_runtime.context_window import ContextWindowBuilder
from app.modules.agent_runtime.conversation_state import ConversationStateRuntime
from app.modules.agent_runtime.conversation_summary import attach_rolling_summary
from app.modules.agent_runtime.deadline import AgentStreamGate, hard_deadline
from app.modules.agent_runtime.delegation import (
    DelegationBudget,
    DelegationPacket,
    DelegationPlan,
    MultiAgentOrchestrator,
    MultiAgentRoutingPolicy,
    SpecialistResult,
    TrustedDelegationScope,
)
from app.modules.agent_runtime.delegation_ledger import SessionDelegationLedger
from app.modules.agent_runtime.handoff_intent import is_explicit_handoff_request
from app.modules.agent_runtime.model_gateway import ModelGatewayError
from app.modules.agent_runtime.models import (
    AgentDefinition,
    AgentDelegation,
    AgentRun,
    AgentToolAudit,
    AgentVersion,
)
from app.modules.agent_runtime.operations_approval import (
    appears_to_request_operations_write,
    approval_for_run,
    build_operations_approval,
    execute_operations_approval,
    prepare_operations_action,
)
from app.modules.agent_runtime.operations_context import (
    OperationsContextBuilder,
    TrustedOperationsContext,
)
from app.modules.agent_runtime.prompt_safety import detects_prompt_injection
from app.modules.agent_runtime.provider_gateway import (
    AgentStreamCallback,
    OperationsSupervisorPlan,
    OperationsSupervisorSubtask,
    ProviderOperationsModelGateway,
    model_failure_code,
)
from app.modules.agent_runtime.public_trace import public_trace
from app.modules.agent_runtime.store_agent import _model_invocation_trace
from app.modules.cart.models import Cart, CartItem
from app.modules.catalog.models import (
    Product,
    ProductAttribute,
    ProductContentVersion,
    ProductContentVersionFile,
    ProductFaq,
    ProductFaqVersion,
    ProductFavorite,
    ProductFulfillmentProfile,
    ProductImage,
    ProductSku,
)
from app.modules.evaluation.models import AiEvaluationRun
from app.modules.evaluation.service import (
    BASELINE_TYPE,
    BASELINE_VERSION,
    CANDIDATE_TYPE,
    CANDIDATE_VERSION,
    DATASET_CASE_COUNT,
    DATASET_MANIFEST,
    DATASET_VERSION,
)
from app.modules.files.models import FileObject
from app.modules.finance.models import UserWallet, WalletTransaction
from app.modules.identity.models import AuthSession, User, UserAddress
from app.modules.inventory.models import Inventory, InventoryLog
from app.modules.knowledge.contracts import ToolResult, ToolScope
from app.modules.knowledge.mcp_host import McpHost, ToolAdapter
from app.modules.knowledge.mcp_registry import database_kill_switch_checker
from app.modules.knowledge.models import (
    AgentSkillBinding,
    KnowledgeDocument,
    SkillDefinition,
    SkillToolBinding,
    SkillVersion,
    ToolDefinition,
    ToolVersion,
)
from app.modules.knowledge.service import KnowledgeService
from app.modules.logistics.models import Shipment, ShipmentItem, ShipmentTrack
from app.modules.messaging.human_schemas import HumanHandoffRequest
from app.modules.messaging.models import (
    Conversation,
    ConversationContext,
    HumanServiceTicket,
    Message,
)
from app.modules.messaging.repository import MessagingRepository
from app.modules.messaging.sequence import lock_conversation_for_append
from app.modules.messaging.service import MessagingService
from app.modules.orders.models import Order, OrderAddress, OrderItem, TradeOrder
from app.modules.payments.models import Payment, PaymentCallback, PaymentEvent
from app.modules.rbac.models import Role, UserRole
from app.modules.reviews.models import Review, ReviewReply
from app.modules.stores.models import ShippingTemplate, Store, StoreFollow, StoreServicePolicy
from app.modules.system.models import AdminBatchJob, DeadLetterEvent, OutboxEvent

logger = structlog.get_logger(__name__)

# Keep model stages bounded independently from the provider transport timeout.  A slow
# planner or writer must never hide already available operating data or confirmation UI.
MODEL_PLANNING_BUDGET_SECONDS = 12.0
# The provider synthesis includes a second grounding-verification request.  A
# 35-second budget made an otherwise complete, card-backed operations answer
# keep the UI in "thinking" for too long.  Planning and business tools have
# already produced verified evidence, so fall back to that structured result
# promptly when prose synthesis is slow.
MODEL_ANSWER_BUDGET_SECONDS = 15.0


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = ""


MERCHANT_REVIEW_PAGE_SIZE = 5
MERCHANT_CONVERSATION_PAGE_SIZE = 8


def _is_operations_page_follow_up(value: str) -> bool:
    """Recognize a conversational list continuation, not a new business goal."""

    compact = re.sub(r"[\s，,。.!！?？]+", "", value).casefold()
    return compact in {
        "下一页",
        "下一批",
        "继续",
        "继续看",
        "查看更多",
        "更多",
        "还有吗",
        "后面的",
    }


def _is_platform_customer_clause(now: datetime) -> Any:
    """Select customer identities without leaking merchant/admin identities.

    All three portals authenticate against ``users``.  The platform-level ``user``
    role is the canonical discriminator already used by the admin dashboard, so AI
    governance queries must use the same boundary.
    """

    return (
        select(UserRole.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            UserRole.user_id == User.id,
            UserRole.grant_status == "active",
            or_(UserRole.expires_at.is_(None), UserRole.expires_at > now),
            UserRole.scope_type == "platform",
            UserRole.scope_id == 0,
            Role.role_code == "user",
            Role.scope_type == "platform",
            Role.role_status == "active",
            Role.deleted_at.is_(None),
        )
        .exists()
    )


def _explicit_operations_display_focus(value: str) -> str | None:
    """Return the one domain the operator explicitly asked the UI to show.

    A Supervisor may legitimately use related specialists to verify ownership and
    current state.  Those supporting reads are execution evidence, not permission
    to flood the chat with every supporting domain's cards.
    """

    compact = re.sub(r"\s+", "", value).casefold()
    only = r"(?:只看|仅看|只展示|仅展示|只显示|仅显示|只要)"
    focused_patterns = (
        ("after_sale", rf"{only}.{{0,24}}(?:售后|退款申请|退款单)"),
        ("users", rf"{only}.{{0,24}}(?:用户|账号|顾客|客户)"),
        ("stores", rf"{only}.{{0,24}}(?:店铺|专卖店|商家|商户)"),
        ("catalog", rf"{only}.{{0,24}}(?:商品|款式|sku)"),
        ("orders", rf"{only}.{{0,24}}(?:订单|物流|包裹|支付)"),
        ("runtime", rf"{only}.{{0,24}}(?:agent|模型|运行|故障|死信)"),
    )
    for domain, pattern in focused_patterns:
        if re.search(pattern, compact):
            return domain
    return None


def _requests_single_focused_card(value: str, focus: str) -> bool:
    compact = re.sub(r"\s+", "", value).casefold()
    if not any(marker in compact for marker in ("只展示", "仅展示", "只显示", "仅显示")):
        return False
    if focus == "after_sale":
        return "售后卡片" in compact and any(
            marker in compact for marker in ("这笔", "该笔", "第一笔", "第1笔", "单笔")
        )
    return False


def _operations_continuation_intent(context_window: Any, user_text: str) -> str | None:
    """Keep a bare “下一页” attached to the latest typed business domain."""

    if not _is_operations_page_follow_up(user_text):
        return None
    state = getattr(context_window, "conversation_state", None)
    payload = getattr(state, "payload", None)
    if not isinstance(payload, Mapping):
        return None
    active_intent = str(payload.get("active_intent") or "")
    return active_intent if active_intent in {"reviews", "service"} else None


def _operations_cursor_filter_key(
    context: TrustedOperationsContext,
    tool_code: str,
) -> str:
    store_no = context.store.store_no if context.store is not None else "platform"
    return (
        f"agent-operations:{context.audience}:{context.conversation.conversation_no}:"
        f"{store_no}:{tool_code}:v1"
    )


async def _latest_operations_continuation(
    session: AsyncSession,
    context: TrustedOperationsContext,
    tool_code: str,
) -> dict[str, object] | None:
    """Read the latest persisted continuation envelope for this conversation/tool."""

    messages = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == context.conversation.id,
                    Message.sender_type == "agent",
                    Message.message_status == "sent",
                    Message.recalled_at.is_(None),
                )
                .order_by(Message.sequence_no.desc())
                .limit(20)
            )
        ).all()
    )
    for message in messages:
        payload = message.content_payload if isinstance(message.content_payload, Mapping) else {}
        continuations = payload.get("continuations")
        if not isinstance(continuations, Mapping):
            continue
        continuation = continuations.get(tool_code)
        if isinstance(continuation, Mapping):
            return dict(continuation)
    return None


def _operations_continuations(data: Mapping[str, Any]) -> dict[str, dict[str, object]]:
    """Collect pagination envelopes from direct and delegated specialist results."""

    sources: list[Mapping[str, Any]] = [data]
    specialists = data.get("specialists")
    if isinstance(specialists, Mapping):
        for result in specialists.values():
            if isinstance(result, Mapping) and isinstance(result.get("data"), Mapping):
                sources.append(result["data"])
    continuations: dict[str, dict[str, object]] = {}
    for source in sources:
        pagination = source.get("pagination")
        if not isinstance(pagination, Mapping):
            continue
        tool_code = str(pagination.get("tool_code") or "")
        if tool_code:
            continuations[tool_code] = dict(pagination)
    return continuations


async def process_operations_run(
    session: AsyncSession,
    run: AgentRun,
    *,
    checkpoint_store: AgentCheckpointStore,
    model_gateway: ProviderOperationsModelGateway | None,
    security: SecurityService | None = None,
    stream_callback: AgentStreamCallback | None = None,
) -> None:
    try:
        context = await OperationsContextBuilder(session).build(run)
    except Exception as exc:
        run.run_status = "failed"
        run.current_phase = "failed"
        run.error_code = getattr(exc, "code", "AGENT_TRUSTED_SCOPE_UNAVAILABLE")
        run.version += 1
        return
    if context.conversation.conversation_status != "active":
        run.run_status = "cancelled"
        run.current_phase = "cancelled"
        run.error_code = "AGENT_DISABLED_BY_CONVERSATION_STATE"
        run.version += 1
        return
    try:
        await checkpoint_store.initialize_operations(context)
        await checkpoint_store.write(run.run_no, "planning", _checkpoint(context, None))
    except Exception:
        await checkpoint_store.session.rollback()
        await _complete(
            session,
            context,
            "智能协作状态暂时不可用，本次没有执行任何业务操作，请稍后重试。",
            "overview",
            {},
            degraded_reason="checkpoint_unavailable",
        )
        return

    run.run_status = "running"
    run.current_phase = "planning"
    run.version += 1
    user_text = context.trigger.text_content or ""
    if detects_prompt_injection(user_text):
        await _complete(
            session,
            context,
            "检测到可能要求绕过权限或泄露敏感信息的指令。本次不会查询业务数据，也不会执行操作。",
            "security_refusal",
            {},
            degraded_reason="prompt_injection_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    existing_approval = await approval_for_run(session, context)
    if existing_approval is not None:
        status, answer, result, error_code = await execute_operations_approval(
            session,
            context,
            existing_approval,
            postgres=checkpoint_store.session,
        )
        if status == "waiting":
            run.run_status = "waiting"
            run.current_phase = "waiting_confirmation"
            run.version += 1
            return
        await _complete(
            session,
            context,
            answer,
            "operations_action_result",
            {
                "operation_result": {
                    "status": status,
                    "action_type": existing_approval.action_type,
                    "target_label": (existing_approval.action_payload or {}).get("target_label"),
                    "result": result,
                    "error_code": error_code,
                }
            },
            tool_code=str((existing_approval.action_payload or {}).get("tool_code") or ""),
            degraded_reason=error_code,
            trace_extra={
                "steps": [
                    {
                        "kind": "approval",
                        "label": "核验操作确认",
                        "status": "completed",
                        "approval_id": existing_approval.approval_no,
                    },
                    {
                        "kind": "tool",
                        "label": "执行受控写操作并回读",
                        "status": "completed" if status == "succeeded" else status,
                        "tool_code": (existing_approval.action_payload or {}).get("tool_code"),
                    },
                    {"kind": "answer", "label": "返回执行结果", "status": "completed"},
                ],
                "source_ids": [
                    f"approval:{existing_approval.approval_no}",
                    f"tool:{(existing_approval.action_payload or {}).get('tool_code')}",
                ],
                "approval_id": existing_approval.approval_no,
                "action_type": existing_approval.action_type,
                "execution_status": status,
            },
        )
        await _finish_checkpoint(checkpoint_store, context, "operations_action_result")
        return
    structured_write_requested = context.trigger.message_type == "agent_asset"
    write_requested = structured_write_requested or appears_to_request_operations_write(
        user_text, context.audience
    )
    write_supervisor_plan: OperationsSupervisorPlan | None = None
    write_planning_source: str | None = None
    write_planning_trace: dict[str, object] | None = None
    if write_requested:
        write_context_window = await _load_operations_context_window(
            session,
            checkpoint_store,
            context,
            security,
        )
        (
            write_supervisor_plan,
            write_planning_source,
            write_planning_trace,
        ) = await _plan_operations_supervisor(
            context,
            user_text,
            write_context_window,
            model_gateway,
        )
    prepared_action, action_issue = await prepare_operations_action(session, context, user_text)
    if prepared_action is not None:
        assert write_supervisor_plan is not None
        assert write_planning_source is not None
        assert write_planning_trace is not None
        required_domain = _operations_action_domain(prepared_action.action_type)
        planned_domains = {task.intent for task in write_supervisor_plan.tasks}
        if required_domain is not None and required_domain not in planned_domains:
            deterministic_plan = _deterministic_operations_plan(user_text, context.audience)
            write_supervisor_plan = deterministic_plan
            write_planning_source = "deterministic_action_coverage_fallback"
            write_planning_trace = {
                **write_planning_trace,
                "status": "rejected",
                "fallback_used": True,
                "reason": ("模型计划没有覆盖待执行动作所属领域，已退回受控规划并保留原始目标。"),
                "required_domain": required_domain,
            }
        if prepared_action.tool_code not in context.allowed_tools:
            await _complete(
                session,
                context,
                (
                    "当前发布的 Agent 版本没有被授予这项写操作工具，"
                    "本次没有创建确认卡，也没有修改数据。"
                ),
                "security_refusal",
                {},
                degraded_reason="tool_not_allowed",
            )
            await _finish_checkpoint(checkpoint_store, context, "security_refusal")
            return
        approval_trace = public_trace(
            run_id=context.run.run_no,
            agent=context.agent_definition.display_name,
            model=context.agent_version.model_profile,
            question=user_text,
            intent="operations_action_preview",
            data={
                "action_type": prepared_action.action_type,
                "target_label": prepared_action.target_label,
            },
            steps=[
                {
                    "kind": "model",
                    "label": "Supervisor 理解并拆解操作目标",
                    "status": "completed",
                    **write_planning_trace,
                },
                *[
                    {
                        "kind": "delegation",
                        "label": f"委派 {task.intent} 领域核对目标",
                        "status": "completed",
                        "subtask_key": task.subtask_key,
                        "objective": task.objective,
                    }
                    for task in write_supervisor_plan.tasks
                ],
                {
                    "kind": "tool",
                    "label": "生成受控操作预览",
                    "status": "completed",
                    "tool_code": prepared_action.tool_code,
                },
                {"kind": "approval", "label": "等待管理员明确确认", "status": "waiting"},
            ],
            source_ids=[f"tool:{prepared_action.tool_code}"],
            tool_code=prepared_action.tool_code,
            extra={
                "planning_source": write_planning_source,
                "goal_ledger": _operations_goal_ledger(write_supervisor_plan),
                "coverage_complete": write_supervisor_plan.coverage_complete,
                "planned_tasks": [
                    {
                        "subtask_key": task.subtask_key,
                        "intent": task.intent,
                        "objective": task.objective,
                    }
                    for task in write_supervisor_plan.tasks
                ],
                "action_type": prepared_action.action_type,
            },
        )
        # The model/planning phase has finished once the confirmation card is
        # materialized.  Keep the public trace aligned with the persisted Run
        # state so the right rail does not remain stuck on "思考中" while the
        # only outstanding step is the administrator's decision.
        approval_trace["status"] = "waiting_confirmation"
        approval = await build_operations_approval(
            session,
            context,
            prepared_action,
            execution_trace=approval_trace,
        )
        await checkpoint_store.write(
            run.run_no,
            "waiting_confirmation",
            {
                **_checkpoint(context, "operations_action_preview"),
                "approval_id": approval.approval_no,
                "action_type": approval.action_type,
            },
        )
        return
    if action_issue is not None and (
        structured_write_requested
        or appears_to_request_operations_write(user_text, context.audience)
    ):
        await _complete(
            session,
            context,
            action_issue,
            "operations_action_clarification",
            {},
        )
        await _finish_checkpoint(checkpoint_store, context, "operations_action_clarification")
        return
    if context.audience == "merchant" and _requests_direct_merchant_write(user_text):
        await _complete(
            session,
            context,
            (
                "AI 经营助理当前只做查询、诊断和行动建议，不会直接改价、上下架、删除商品"
                "或修改库存，更不会绕过你的确认批量操作。你可以先让我筛出目标商品和风险，"
                "再到商品管理页逐项核对处理。"
            ),
            "security_refusal",
            {},
            degraded_reason="protected_action_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    if context.audience == "merchant" and await _requests_other_store_operations(
        session, context.store.id if context.store else 0, user_text
    ):
        await _complete(
            session,
            context,
            (
                "AI 经营助理只能读取当前店铺的经营数据，不能查看其他店铺的营业额、订单、"
                "库存或顾客信息。跨店数据仅能由具备相应权限的超级管理员在管理端核对。"
            ),
            "security_refusal",
            {},
            degraded_reason="data_scope_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    if context.audience == "admin" and _requests_direct_admin_write(user_text):
        await _complete(
            session,
            context,
            (
                "AI 管家当前只做跨域查询、诊断和治理建议，不能在对话中直接修改余额、密码、"
                "账号状态、店铺、商品或订单，也不能跳过审计和确认。请进入对应管理页面核对"
                "目标后再执行。本次没有修改任何数据。"
            ),
            "security_refusal",
            {},
            degraded_reason="protected_action_blocked",
        )
        await _finish_checkpoint(checkpoint_store, context, "security_refusal")
        return
    operation_guide = _operations_how_to_guide(user_text, context.audience)
    if operation_guide is not None:
        await _complete(
            session,
            context,
            str(operation_guide["answer"]),
            "operation_guide",
            {"operation_guide": operation_guide},
        )
        await _finish_checkpoint(checkpoint_store, context, "operation_guide")
        return

    # Build the read-only context without triggering unrelated pending ORM writes.
    # Delegation and MCP audit records are persisted through the shared session
    # below so they cannot deadlock on the parent AgentRun transaction.
    context_window = await _load_operations_context_window(
        session,
        checkpoint_store,
        context,
        security,
    )
    supervisor_plan, planning_source, planning_model_trace = await _plan_operations_supervisor(
        context, user_text, context_window, model_gateway
    )
    deterministic_plan = _deterministic_operations_plan(user_text, context.audience)
    planned_intents = tuple(task.intent for task in supervisor_plan.tasks)
    intent = planned_intents[0] if planned_intents else "overview"
    complex_domains = tuple(
        item for item in planned_intents if item not in {"overview", "human_handoff"}
    )
    if len(complex_domains) >= 2:
        intent = (
            "complex_platform_diagnosis"
            if context.audience == "admin"
            else "complex_store_diagnosis"
        )
    await checkpoint_store.write(run.run_no, "tool_planned", _checkpoint(context, intent))

    if intent == "human_handoff":
        if context.audience == "merchant":
            ticket = await MessagingService(session).request_human_from_agent(
                context.user,
                context.conversation.conversation_no,
                HumanHandoffRequest(
                    ticket_type="general",
                    summary="商家专属客服转平台人工",
                    message_refs=[context.trigger.message_no],
                ),
                context.run.run_no,
            )
            await _complete(
                session,
                context,
                "我正在帮你转接平台人工客服。转接期间我会暂停回复，人工服务结束后我会继续协助你。",
                intent,
                {"ticket_id": ticket.ticket_id, "ticket_status": ticket.ticket_status},
                tool_code="support.create_platform_ticket",
            )
        else:
            await _complete(
                session,
                context,
                (
                    "你当前已在超级管理端。AI 管家不会把管理员会话转交给普通客服。"
                    "请直接使用管理工作台处理，或联系系统运维负责人。"
                ),
                intent,
                {},
            )
        await _finish_checkpoint(checkpoint_store, context, intent)
        return

    # Capability/help text must never pre-empt a compound operational request just
    # because one of its goals mentions a customer-service queue.  Only consider
    # the deterministic small-talk path when the supervisor found no business
    # domain to execute.
    small_talk_reply = (
        _operations_small_talk_reply(user_text, context.audience)
        if intent == "overview" and not complex_domains
        else None
    )
    if small_talk_reply is not None:
        small_evidence: dict[str, object] = {
            "assistant_scope": (
                "商家经营、商品、库存、订单与平台服务"
                if context.audience == "merchant"
                else "平台用户、店铺、商品、交易与系统运行的只读分析"
            )
        }
        small_answer = small_talk_reply
        small_answer_mode = "deterministic_fallback"
        small_citations: tuple[str, ...] = ("context:assistant_scope",)
        small_confidence = "high"
        small_analysis: dict[str, object] = {}
        if model_gateway is not None and _allows_operations_model_synthesis("general_chat"):
            run.current_phase = "answering"
            run.version += 1
            try:
                stream_gate = AgentStreamGate(stream_callback)
                grounded = await hard_deadline(
                    model_gateway.synthesize(
                        agent_prompt=context.agent_version.system_prompt,
                        user_text=user_text,
                        intent="general_chat",
                        evidence=small_evidence,
                        source_ids=small_citations,
                        stream_callback=stream_gate.publish,
                    ),
                    budget_seconds=MODEL_ANSWER_BUDGET_SECONDS,
                )
                stream_gate.close()
                small_answer = _normalize_operations_answer(grounded.text)
                if stream_callback is not None and small_answer != grounded.text:
                    await stream_callback("answer_replace", small_answer)
                small_answer_mode = "model_grounded"
                small_confidence = grounded.confidence
                small_citations = grounded.cited_source_ids or small_citations
                small_analysis = _grounded_analysis_trace(grounded)
            except (ModelGatewayError, TimeoutError) as exc:
                if "stream_gate" in locals():
                    stream_gate.close()
                run.degraded_reason = model_failure_code(exc, "answer")
        await _complete(
            session,
            context,
            small_answer,
            "general_chat",
            small_evidence,
            trace_extra={
                "answer_mode": small_answer_mode,
                "confidence": small_confidence,
                "cited_source_ids": list(small_citations),
                "planning_source": planning_source,
                "model_invocation": planning_model_trace,
                "goal_ledger": _operations_goal_ledger(supervisor_plan),
                "coverage_complete": supervisor_plan.coverage_complete,
                "supervisor_tasks": [
                    {
                        "subtask_key": task.subtask_key,
                        "intent": task.intent,
                        "objective": task.objective,
                    }
                    for task in supervisor_plan.tasks
                ],
                "subtasks": _operations_subtask_trace(
                    supervisor_plan.tasks, context.audience, user_text
                ),
                **small_analysis,
            },
        )
        await _finish_checkpoint(checkpoint_store, context, "general_chat")
        return

    if intent in {"complex_platform_diagnosis", "complex_store_diagnosis"}:
        complex_tasks = tuple(
            task for task in supervisor_plan.tasks if task.intent in complex_domains
        )
        multi_response = await _execute_operations_multi_agent(session, context, complex_tasks)
        if multi_response is not None:
            evidence, trace_steps, source_ids = multi_response
            if context.audience == "merchant" and "policy" in complex_domains:
                await _attach_merchant_policy_knowledge(
                    session,
                    checkpoint_store,
                    context,
                    evidence,
                )
            evidence["conversation_window"] = context_window.model_projection()
            priority_focus = _priority_focus_index(user_text)
            if priority_focus is not None:
                evidence["priority_focus"] = priority_focus
            requested_card_limit = _requested_priority_count(user_text)
            if requested_card_limit is not None:
                evidence["requested_card_limit"] = requested_card_limit
            is_admin_priority_follow_up = (
                context.audience == "admin"
                and _requests_priority_follow_up(re.sub(r"\s+", "", user_text).casefold())
            )
            is_merchant_priority_follow_up = (
                context.audience == "merchant"
                and _requests_priority_follow_up(re.sub(r"\s+", "", user_text).casefold())
            )
            answer = (
                _render_admin_priority_follow_up(evidence, user_text=user_text)
                if is_admin_priority_follow_up
                else _render_merchant_priority_follow_up(evidence, user_text=user_text)
                if is_merchant_priority_follow_up
                else _render_merchant_multi_agent(evidence)
                if context.audience == "merchant"
                else _render_multi_agent(evidence, user_text=user_text)
            )
            answer_mode = "deterministic_fallback"
            confidence = "high"
            multi_citations = source_ids
            multi_analysis: dict[str, object] = {}
            if model_gateway is not None and _allows_operations_model_synthesis(intent):
                run.current_phase = "answering"
                run.version += 1
                logger.info(
                    "operations_agent_model_answer_started",
                    run_no=run.run_no,
                    audience=context.audience,
                    intent=intent,
                    domain_count=len(complex_domains),
                )
                try:
                    stream_gate = AgentStreamGate(stream_callback)
                    grounded = await hard_deadline(
                        model_gateway.synthesize(
                            agent_prompt=context.agent_version.system_prompt,
                            user_text=user_text,
                            intent=intent,
                            evidence=evidence,
                            source_ids=source_ids,
                            stream_callback=stream_gate.publish,
                        ),
                        budget_seconds=MODEL_ANSWER_BUDGET_SECONDS,
                    )
                    stream_gate.close()
                    multi_analysis = _grounded_analysis_trace(grounded)
                    candidate_answer = _normalize_operations_answer(grounded.text)
                    if _operations_answer_satisfies_request(user_text, candidate_answer):
                        answer = candidate_answer
                        answer_mode = "model_grounded"
                    else:
                        multi_analysis["response_quality_guard"] = {
                            "status": "rejected",
                            "reason": "answer_did_not_preserve_requested_focus",
                        }
                        answer_mode = "model_guarded_fallback"
                    if stream_callback is not None and answer != grounded.text:
                        await stream_callback("answer_replace", answer)
                    confidence = grounded.confidence
                    multi_citations = grounded.cited_source_ids or source_ids
                    logger.info(
                        "operations_agent_model_answer_completed",
                        run_no=run.run_no,
                        audience=context.audience,
                        intent=intent,
                    )
                except (ModelGatewayError, TimeoutError) as exc:
                    if "stream_gate" in locals():
                        stream_gate.close()
                    run.degraded_reason = model_failure_code(exc, "answer")
                    logger.warning(
                        "operations_agent_model_answer_degraded",
                        run_no=run.run_no,
                        audience=context.audience,
                        intent=intent,
                        reason=run.degraded_reason,
                        error_detail=str(exc)[:300],
                    )
            logger.info(
                "operations_agent_completion_started",
                run_no=run.run_no,
                audience=context.audience,
                intent=intent,
                answer_mode=answer_mode,
            )
            await _complete(
                session,
                context,
                answer,
                intent,
                evidence,
                trace_extra={
                    "steps": trace_steps,
                    "source_ids": list(source_ids),
                    "cited_source_ids": list(multi_citations),
                    "orchestration_mode": "multi_agent",
                    "execution_strategy": (
                        "sequential_batches_parallel_within_batch_shared_session"
                        if len(complex_domains) > 6
                        else "parallel_domains_serialized_shared_session"
                    ),
                    "delegation_count": sum(
                        str(step.get("kind") or "") == "delegation" for step in trace_steps
                    ),
                    "batch_count": max(
                        1,
                        (
                            sum(str(step.get("kind") or "") == "delegation" for step in trace_steps)
                            + 5
                        )
                        // 6,
                    ),
                    "max_parallel_per_batch": 3,
                    "answer_mode": answer_mode,
                    "confidence": confidence,
                    "planning_source": planning_source,
                    "model_invocation": planning_model_trace,
                    "goal_ledger": _operations_goal_ledger(supervisor_plan),
                    "coverage_complete": supervisor_plan.coverage_complete,
                    "supervisor_tasks": [
                        {
                            "subtask_key": task.subtask_key,
                            "intent": task.intent,
                            "objective": task.objective,
                        }
                        for task in supervisor_plan.tasks
                    ],
                    "deterministic_tasks": [
                        {
                            "subtask_key": task.subtask_key,
                            "intent": task.intent,
                            "objective": task.objective,
                        }
                        for task in deterministic_plan.tasks
                    ],
                    "subtasks": [
                        {
                            "subtask_key": str(step.get("delegation_id") or ""),
                            "specialist": _specialist_public_name(
                                str(step.get("specialist") or "")
                            ),
                            "specialist_code": str(step.get("specialist") or ""),
                            "intent": str(step.get("intent") or ""),
                            "objective": str(step.get("objective") or ""),
                            "allowed_tools": (
                                step.get("allowed_tools")
                                if isinstance(step.get("allowed_tools"), list)
                                else []
                            ),
                            "depth": (
                                step.get("depth") if isinstance(step.get("depth"), int) else 1
                            ),
                            "status": str(step.get("status") or "completed"),
                        }
                        for step in trace_steps
                        if step.get("kind") == "delegation"
                    ],
                    **multi_analysis,
                },
            )
            logger.info(
                "operations_agent_completion_persisted",
                run_no=run.run_no,
                audience=context.audience,
                intent=intent,
                answer_mode=answer_mode,
            )
            await _finish_checkpoint(checkpoint_store, context, intent)
            return
    tool_code = _tool_for_query(intent, context.audience, user_text)
    if tool_code not in context.allowed_tools:
        tool_code = (
            "store_ops.overview"
            if context.audience == "merchant"
            else "governance.platform_overview"
        )
        intent = "overview"
    tool_result = await _execute_tool(session, context, tool_code, query_text=user_text)
    if tool_result.status != "succeeded":
        await _complete(
            session,
            context,
            "当前数据范围内无法安全完成查询。本次没有执行任何写操作，请稍后重试或联系人工客服。",
            intent,
            {},
            tool_code=tool_code,
            degraded_reason=tool_result.error_code or "tool_failed",
        )
        await _finish_checkpoint(checkpoint_store, context, intent)
        return

    evidence = dict(tool_result.safe_data)
    if context.audience == "merchant" and intent == "policy":
        await _attach_merchant_policy_knowledge(
            session,
            checkpoint_store,
            context,
            evidence,
        )
    if context_window.recent_turns or context_window.summary_no:
        evidence["conversation_window"] = context_window.model_projection()
    selected_customer_conversation = evidence.get("selected_conversation")
    customer_reply_draft_requested = (
        context.audience == "merchant"
        and intent == "service"
        and isinstance(selected_customer_conversation, Mapping)
        and _requests_customer_reply_draft(user_text)
    )
    if customer_reply_draft_requested and isinstance(selected_customer_conversation, Mapping):
        evidence["requested_output"] = {
            "type": "customer_reply_draft",
            "preview_only": True,
            "must_not_claim_sent": True,
            "style": "简洁、礼貌、先回应顾客当前问题，证据不足时说明正在核对，不承诺未证实结果",
        }
    answer = _render(context, intent, evidence, user_text=user_text)
    answer_mode = "deterministic_fallback"
    confidence = "high"
    citations: tuple[str, ...] = (f"tool:{tool_code}",)
    grounded_analysis: dict[str, object] = {}
    if (
        model_gateway is not None
        and _allows_operations_model_synthesis(intent)
        and not _is_operations_page_follow_up(user_text)
    ):
        run.current_phase = "answering"
        run.version += 1
        try:
            stream_gate = AgentStreamGate(stream_callback)
            grounded = await hard_deadline(
                model_gateway.synthesize(
                    agent_prompt=context.agent_version.system_prompt,
                    user_text=user_text,
                    intent=("service_reply_draft" if customer_reply_draft_requested else intent),
                    evidence=evidence,
                    source_ids=citations,
                    stream_callback=stream_gate.publish,
                ),
                budget_seconds=MODEL_ANSWER_BUDGET_SECONDS,
            )
            stream_gate.close()
            answer = _normalize_operations_answer(grounded.text)
            if stream_callback is not None and answer != grounded.text:
                await stream_callback("answer_replace", answer)
            answer_mode = "model_grounded"
            confidence = grounded.confidence
            citations = grounded.cited_source_ids or citations
            grounded_analysis = _grounded_analysis_trace(grounded)
        except (ModelGatewayError, TimeoutError) as exc:
            if "stream_gate" in locals():
                stream_gate.close()
            run.degraded_reason = model_failure_code(exc, "answer")
            if stream_callback is not None:
                await stream_callback("answer_replace", answer)
    if customer_reply_draft_requested and isinstance(selected_customer_conversation, Mapping):
        evidence["reply_draft"] = {
            "content": answer,
            "preview_only": True,
            "conversation_id": str(selected_customer_conversation.get("conversation_id") or ""),
            "customer_name": str(selected_customer_conversation.get("customer_name") or "顾客"),
        }
    await _complete(
        session,
        context,
        answer,
        intent,
        evidence,
        tool_code=tool_code,
        trace_extra={
            "answer_mode": answer_mode,
            "confidence": confidence,
            "cited_source_ids": list(citations),
            "planning_source": planning_source,
            "model_invocation": planning_model_trace,
            "goal_ledger": _operations_goal_ledger(supervisor_plan),
            "coverage_complete": supervisor_plan.coverage_complete,
            "supervisor_tasks": [
                {
                    "subtask_key": task.subtask_key,
                    "intent": task.intent,
                    "objective": task.objective,
                }
                for task in supervisor_plan.tasks
            ],
            "subtasks": _operations_subtask_trace(
                supervisor_plan.tasks, context.audience, user_text
            ),
            **grounded_analysis,
        },
    )
    await _finish_checkpoint(checkpoint_store, context, intent)


def _requests_direct_merchant_write(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    informational = any(
        marker in normalized
        for marker in ("如何", "怎么", "流程", "在哪里", "入口", "说明", "解释")
    )
    explicit_execution = any(
        marker in normalized
        for marker in ("直接", "替我", "全部", "批量", "不要让我确认", "无需确认")
    )
    direct = any(
        marker in normalized
        for marker in (
            "直接",
            "帮我",
            "给我",
            "替我",
            "全部",
            "批量",
            "不要让我确认",
            "无需确认",
        )
    )
    mutation = any(
        marker in normalized
        for marker in (
            "改价",
            "价格减",
            "价格改",
            "涨价",
            "降价",
            "下架",
            "上架",
            "发布商品",
            "删除商品",
            "修改库存",
            "调整库存",
        )
    )
    return direct and mutation and (explicit_execution or not informational)


def _requests_direct_admin_write(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    informational = any(
        marker in normalized
        for marker in ("如何", "怎么", "流程", "在哪里", "入口", "说明", "解释")
    )
    direct = any(
        marker in normalized
        for marker in ("直接", "帮我", "给我", "替我", "全部", "批量", "不要确认", "无需确认")
    )
    mutation = any(
        marker in normalized
        for marker in (
            "余额改",
            "充值",
            "冻结",
            "强制下线",
            "修改密码",
            "删除用户",
            "删除店铺",
            "暂停营业",
            "改价",
            "下架",
            "修改订单",
        )
    )
    bypass = any(
        marker in normalized for marker in ("跳过审计", "绕过审计", "不要确认", "无需确认")
    )
    explicit_execution = any(marker in normalized for marker in ("直接", "替我", "全部", "批量"))
    return mutation and (direct or bypass) and (explicit_execution or not informational)


def _allows_operations_model_synthesis(intent: str) -> bool:
    """Let the model reason over trusted evidence where synthesis adds value.

    Tools remain the source of truth and cards remain deterministic.  The model
    is used for open-ended analysis, policy explanation and cross-domain result
    synthesis so the operations assistants behave as Agents rather than a menu
    of fixed templates.  Every path retains the grounded fallback when the
    provider is unavailable or exceeds its time budget.
    """

    return intent in {
        "overview",
        "policy",
        "service",
        "support",
        "complex_store_diagnosis",
        "complex_platform_diagnosis",
    }


def _requests_customer_reply_draft(value: str) -> bool:
    """Recognize a merchant asking for a preview, never an implicit send."""

    compact = re.sub(r"\s+", "", value).casefold()
    return any(
        marker in compact
        for marker in (
            "拟回复",
            "回复草稿",
            "帮我回复",
            "建议怎么回复",
            "应该怎么回复",
            "怎么回这个顾客",
            "怎么回复这个顾客",
        )
    )


def _operations_how_to_guide(value: str, audience: str) -> dict[str, object] | None:
    normalized = re.sub(r"\s+", "", value).casefold()
    asks_how = any(
        marker in normalized for marker in ("如何", "怎么", "流程", "在哪里", "步骤", "操作说明")
    )
    if not asks_how and "入口" in normalized:
        asks_how = any(
            marker in normalized
            for marker in (
                "上架入口",
                "下架入口",
                "改价入口",
                "发货入口",
                "售后入口",
                "充值入口",
                "冻结入口",
            )
        )
    if not asks_how:
        return None

    if audience == "merchant":
        if any(marker in normalized for marker in ("下架", "上架", "改价", "价格", "库存")):
            sku_match = re.search(r"([A-Za-z0-9\u4e00-\u9fff]{1,20}(?:支装|件装|个装|盒装))", value)
            quantity_match = re.search(r"(?:补到|改到|设为|设置为)\s*(\d+)\s*件", value)
            sku_label = sku_match.group(1) if sku_match is not None else "目标款式"
            quantity_label = (
                f"{quantity_match.group(1)} 件" if quantity_match is not None else "目标数量"
            )
            return {
                "title": "在商品管理中核对后操作",
                "answer": (
                    f"进入“我的商品”，打开目标商品并选中“{sku_label}”，将库存核对后设置为"
                    f"“{quantity_label}”，再保存或提交审核。"
                    "上架、下架、改价和库存修改都由你在商品页确认，AI 经营助理不会代为执行。"
                ),
                "steps": [
                    "打开我的商品并选择目标商品",
                    f"选中{sku_label}",
                    f"核对当前库存并填写{quantity_label}",
                    "确认保存或提交",
                ],
                "path": "/merchant/products",
                "label": "打开我的商品",
            }
        if any(marker in normalized for marker in ("发货", "订单", "售后", "退款")):
            return {
                "title": "在本店订单中处理",
                "answer": (
                    "进入“我的订单”，按状态找到目标订单并先核对顾客、商品、金额和当前履约状态，"
                    "再执行页面提供的发货或售后操作。AI 经营助理只提供查询和建议。"
                ),
                "steps": ["打开我的订单", "筛选订单状态", "核对订单与履约信息", "执行可用操作"],
                "path": "/merchant/orders",
                "label": "打开我的订单",
            }
    else:
        if any(marker in normalized for marker in ("充值", "余额")):
            return {
                "title": "给用户调整账户余额",
                "answer": (
                    "进入“用户与权限”，搜索并打开目标用户，在“账户余额”区域输入本次充值金额并确认。"
                    "提交前请核对用户名、当前余额和金额，完成后检查新余额与审计记录。本次只说明步骤，"
                    "没有修改任何账户。"
                ),
                "steps": [
                    "打开用户与权限",
                    "搜索并打开目标用户",
                    "核对账户与当前余额",
                    "输入金额并确认",
                    "复核结果",
                ],
                "path": "/admin/users",
                "label": "打开用户与权限",
            }
        if any(
            marker in normalized for marker in ("冻结", "强制下线", "删除用户", "改密码", "邮箱")
        ):
            return {
                "title": "管理用户账号",
                "answer": (
                    "进入“用户与权限”，搜索目标用户并打开详情，在账号资料中核对身份和当前状态后，"
                    "使用页面上的冻结、强制下线或资料维护操作。本次没有修改任何用户数据。"
                ),
                "steps": ["打开用户与权限", "搜索目标用户", "核对账号状态", "选择并确认操作"],
                "path": "/admin/users",
                "label": "打开用户与权限",
            }
        if any(marker in normalized for marker in ("店铺", "暂停营业", "商品下架", "商品删除")):
            return {
                "title": "管理店铺与店内商品",
                "answer": (
                    "进入“店铺运营”，搜索并打开目标店铺，先核对营业状态和店铺资料，再从店铺商品或"
                    "店铺订单区域处理。AI 管家不会在对话中直接修改业务数据。"
                ),
                "steps": [
                    "打开店铺运营",
                    "搜索目标店铺",
                    "核对状态与资料",
                    "进入商品或订单区域处理",
                ],
                "path": "/admin/stores",
                "label": "打开店铺运营",
            }
    return None


async def _requests_other_store_operations(
    session: AsyncSession, current_store_id: int, value: str
) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    sensitive = any(
        marker in normalized
        for marker in ("营业额", "订单", "库存", "顾客", "客户", "销量", "收入")
    )
    if not sensitive:
        return False
    names = list(
        await session.scalars(
            select(Store.store_name).where(
                Store.id != current_store_id,
                Store.store_status.in_(("active", "suspended")),
            )
        )
    )
    return any(re.sub(r"\s+", "", name).casefold() in normalized for name in names if name)


def _grounded_analysis_trace(answer: object) -> dict[str, object]:
    summary = getattr(answer, "analysis_summary", None)
    details = getattr(answer, "analysis_details", ())
    thinking_used = getattr(answer, "thinking_used", False)
    grounding_verified = getattr(answer, "grounding_verified", None)
    evidence_truncated = getattr(answer, "evidence_truncated", False)
    truncated_evidence_fields = getattr(answer, "truncated_evidence_fields", ())
    result: dict[str, object] = {
        "thinking_mode": "enabled" if thinking_used else "not_reported",
        "grounding_verified": grounding_verified,
        "evidence_truncated": evidence_truncated,
        "truncated_evidence_fields": list(truncated_evidence_fields),
        "model_invocation": _model_invocation_trace(answer),
    }
    if isinstance(summary, str) and summary:
        result["analysis_summary"] = summary
    if isinstance(details, tuple) and details:
        result["analysis_details"] = list(details)
    return result


def _normalize_operations_answer(text: str) -> str:
    """Keep operator-facing prose free of internal enum values.

    The model is instructed to localize statuses, but the durable response must not
    depend on prompt compliance alone. These narrow replacements preserve the
    factual counts while translating the enum tokens that can occur in overview
    answers. They intentionally do not rewrite arbitrary user-authored text.
    """

    normalized = re.sub(
        r"(\d+\s*个店铺(?:处于|为))\s*active\b",
        r"\1营业中",
        text,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"(\d+\s*个用户(?:处于|为))\s*active\b",
        r"\1正常状态",
        normalized,
        flags=re.IGNORECASE,
    )
    status_labels = {
        "shipped": "已发货",
        "completed": "已完成",
        "cancelled": "已取消",
        "closed": "已关闭",
        "on_sale": "销售中",
        "off_shelf": "已下架",
        "pending_review": "审核中",
        "needs_revision": "需修改",
        "draft": "草稿",
    }
    for code, label in status_labels.items():
        normalized = re.sub(
            rf"((?:处于|状态为|为))\s*{re.escape(code)}\b",
            rf"\1{label}",
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(rf"\b{re.escape(code)}\b", label, normalized, flags=re.IGNORECASE)
    return normalized


async def _execute_operations_multi_agent(
    session: AsyncSession,
    context: TrustedOperationsContext,
    tasks: tuple[OperationsSupervisorSubtask, ...],
) -> tuple[dict[str, object], list[dict[str, object]], tuple[str, ...]] | None:
    routing = MultiAgentRoutingPolicy.from_agent_version_policy(context.agent_version.policy_config)
    parent_scope = TrustedDelegationScope(
        user_no=context.user.user_no,
        conversation_no=context.conversation.conversation_no,
    )
    session_lock = asyncio.Lock()
    all_safe_output: dict[str, object] = {}
    all_delegation_traces: list[Any] = []
    delegation_specs: dict[str, tuple[OperationsSupervisorSubtask, str, str]] = {}
    batch_size = 6
    resolved_tasks = _resolve_operations_delegations(
        tasks,
        audience=context.audience,
        allowed_tools=context.allowed_tools,
    )
    task_batches = [
        resolved_tasks[index : index + batch_size]
        for index in range(0, len(resolved_tasks), batch_size)
    ]
    for batch in task_batches:
        deadline = time.monotonic() + 5.0
        parent_budget = DelegationBudget(
            deadline_monotonic=deadline,
            token_limit=4_800,
            tool_call_limit=batch_size,
            model_call_limit=0,
        )
        packets: list[DelegationPacket] = []
        specialists: dict[str, Any] = {}
        for task, specialist_code, tool_code, default_objective in batch:
            domain = task.intent
            objective = task.objective.strip() or default_objective
            delegation_no = new_prefixed_ulid("dlg_")
            packet = DelegationPacket(
                delegation_no=delegation_no,
                parent_run_no=context.run.run_no,
                subtask_key=(
                    f"{context.audience}-diagnosis:{domain}:{tool_code.replace('.', '_')}"
                ),
                specialist_code=specialist_code,
                specialist_version="v1",
                objective=objective,
                depth=1,
                trusted_scope=parent_scope,
                resource_refs=(),
                user_constraints=(),
                allowed_tools=frozenset({tool_code}),
                budget=parent_budget.child(
                    token_limit=800,
                    tool_call_limit=1,
                    model_call_limit=0,
                ),
                ancestor_agents=(
                    "admin_copilot" if context.audience == "admin" else "merchant_copilot",
                ),
            )
            packets.append(packet)
            delegation_specs[delegation_no] = (task, specialist_code, tool_code)
            specialists[specialist_code] = _admin_specialist_executor(
                session,
                session_lock,
                context,
                specialist_code,
            )
        decision = routing.decide(
            intent=(
                "complex_platform_diagnosis"
                if context.audience == "admin"
                else "complex_store_diagnosis"
            ),
            independent_read_subtasks=len(packets),
            has_write_intent=False,
            confidence=1.0,
        )
        if len(packets) >= 2 and decision.mode != "multi_agent":
            return None
        orchestrator = MultiAgentOrchestrator(
            specialists,
            ledger=SessionDelegationLedger(session, session_lock),
            max_parallel=3,
        )
        safe_output, delegation_traces = await orchestrator.execute(
            DelegationPlan(tuple(packets)),
            parent_tools=context.allowed_tools,
            parent_scope=parent_scope,
            parent_resource_refs=frozenset(),
            budget=parent_budget,
        )
        all_safe_output.update(safe_output)
        all_delegation_traces.extend(delegation_traces)
    successful_statuses = {"succeeded", "reused"}
    partial_statuses = {"partial"}
    subtask_results: list[dict[str, object]] = []
    for trace in all_delegation_traces:
        task, specialist_code, tool_code = delegation_specs[trace.delegation_no]
        status = str(trace.status)
        subtask_results.append(
            {
                "subtask_key": trace.delegation_no,
                "specialist": specialist_code,
                "intent": task.intent,
                "objective": task.objective,
                "tool_code": tool_code,
                "status": status,
                "error_code": trace.error_code,
                "retry_prompt": f"只重试这项任务：{task.objective}",
            }
        )
    success_count = sum(str(result["status"]) in successful_statuses for result in subtask_results)
    partial_count = sum(str(result["status"]) in partial_statuses for result in subtask_results)
    failed_count = len(subtask_results) - success_count - partial_count
    overall_status = (
        "succeeded"
        if subtask_results and failed_count == 0 and partial_count == 0
        else "failed"
        if subtask_results and success_count == 0 and partial_count == 0
        else "partial"
    )
    evidence: dict[str, object] = {
        "specialists": dict(all_safe_output),
        "audience": context.audience,
        "result_policy": "只合并授权范围内、带工具审计的只读结果",
        "subtask_summary": {
            "status": overall_status,
            "success_count": success_count,
            "partial_count": partial_count,
            "failed_count": failed_count,
            "total_count": len(subtask_results),
        },
        "subtask_results": subtask_results,
    }
    evidence["_audit_tool_calls"] = [
        {
            "sequence": index,
            "tool_code": delegation_specs[trace.delegation_no][2],
            "arguments": {},
            "status": trace.status,
            "result": all_safe_output.get(trace.delegation_no, {}),
            "result_count": 1 if all_safe_output.get(trace.delegation_no) else 0,
            "error_code": trace.error_code,
            "latency_ms": trace.elapsed_ms,
            "record_source": "delegation_result",
        }
        for index, trace in enumerate(all_delegation_traces, start=1)
    ]
    if context.store is not None:
        evidence["store"] = {
            "store_id": context.store.store_no,
            "store_name": context.store.store_name,
        }
    steps: list[dict[str, object]] = [
        {"kind": "plan", "label": "识别跨域只读诊断", "status": "completed"},
        {
            "kind": "supervisor",
            "label": "分派必要的领域助手并协同核对",
            "status": "completed",
            "delegation_count": len(all_delegation_traces),
            "batch_count": len(task_batches),
            "max_parallel_per_batch": 3,
            "execution_strategy": "sequential_batches_parallel_within_batch_shared_session",
        },
    ]
    for trace in all_delegation_traces:
        task, _specialist_code, tool_code = delegation_specs[trace.delegation_no]
        steps.append(
            {
                "kind": "delegation",
                "label": _specialist_label(trace.specialist_code),
                "status": trace.status,
                "delegation_id": trace.delegation_no,
                "specialist": trace.specialist_code,
                "intent": task.intent,
                "objective": task.objective,
                "allowed_tools": [tool_code] if tool_code else [],
                "depth": 1,
                "tool_code": tool_code,
                "latency_ms": trace.elapsed_ms,
                "tool_calls": trace.tool_calls,
                "tokens_used": trace.tokens_used,
                "error_code": trace.error_code,
            }
        )
    steps.append(
        {
            "kind": "answer",
            "label": "合并可信诊断结果",
            "status": "completed" if overall_status == "succeeded" else overall_status,
            "success_count": success_count,
            "partial_count": partial_count,
            "failed_count": failed_count,
        }
    )
    source_ids = tuple(
        dict.fromkeys(
            f"tool:{delegation_specs[trace.delegation_no][2]}" for trace in all_delegation_traces
        )
    )
    return evidence, steps, source_ids


def _narrow_operations_specialist(default_code: str, tool_code: str) -> str:
    # The model plans by business domain, while the query selector may narrow a
    # task to a more specific tool after inspecting the objective.  Always move
    # the packet to the policy that owns that tool; otherwise a harmless phrase
    # such as "不要展示普通订单卡片" can leave an after-sale tool attached to an
    # order specialist and fail the whole run at the permission gateway.
    specialist_by_tool = {
        "governance.trade.payment_timeline": "governance_payments",
        "governance.trade.shipments.get": "governance_logistics",
        "governance.metrics.query": "governance_metrics",
        "governance.after_sale_summary": "governance_after_sale",
        "governance.after_sale.timeline": "governance_after_sale",
        "store_ops.after_sale.list": "merchant_after_sale",
        "store_ops.revenue_metrics": "merchant_orders",
        "store_ops.orders.list": "merchant_orders",
        "store_ops.orders.get": "merchant_orders",
        "store_ops.inventory.get_skus": "merchant_inventory",
        "store_ops.catalog.get_product": "merchant_catalog",
        "store_ops.reviews.list": "merchant_review_service",
        "store_ops.conversations.list": "merchant_customer_service",
    }
    return specialist_by_tool.get(tool_code, default_code)


def _resolve_operations_delegations(
    tasks: tuple[OperationsSupervisorSubtask, ...],
    *,
    audience: str,
    allowed_tools: frozenset[str],
) -> tuple[tuple[OperationsSupervisorSubtask, str, str, str], ...]:
    """Resolve and coalesce Agent/Tool routes before creating packets.

    A provider may split one factual need into two semantic goals, for example
    "售后记录" and "该售后关联订单".  If both goals resolve to the same narrow
    read Tool, executing it twice adds latency and makes the public trace look
    like duplicate Agents.  Keep one route and prefer the task whose declared
    domain natively owns the resolved specialist.
    """

    resolved: list[tuple[OperationsSupervisorSubtask, str, str, str, bool]] = []
    route_index: dict[tuple[str, str], int] = {}
    for task in tasks:
        default_specialist, default_tool, default_objective = _operations_specialist(
            audience, task.intent
        )
        tool_code = _tool_for_query(task.intent, audience, task.objective)
        if tool_code not in allowed_tools:
            tool_code = default_tool
        specialist = _narrow_operations_specialist(default_specialist, tool_code)
        native_owner = specialist == default_specialist
        route = (specialist, tool_code)
        previous_index = route_index.get(route)
        candidate = (task, specialist, tool_code, default_objective, native_owner)
        if previous_index is None:
            route_index[route] = len(resolved)
            resolved.append(candidate)
        elif native_owner and not resolved[previous_index][4]:
            resolved[previous_index] = candidate
    return tuple(item[:4] for item in resolved)


def _admin_specialist_executor(
    session: AsyncSession,
    session_lock: asyncio.Lock,
    context: TrustedOperationsContext,
    specialist_code: str,
) -> Any:
    async def execute(packet: DelegationPacket, budget: DelegationBudget) -> SpecialistResult:
        budget.validate()
        if len(packet.allowed_tools) != 1:
            raise ValueError("operations specialist packet must contain exactly one tool")
        tool_code = next(iter(packet.allowed_tools))
        async with session_lock:
            result = await _execute_tool(
                session,
                context,
                tool_code,
                query_text=packet.objective,
            )
        return SpecialistResult(
            specialist_code=specialist_code,
            status=result.status,
            safe_data=result.safe_data,
            tokens_used=0,
            tool_calls=1,
            model_calls=0,
            scope=packet.trusted_scope,
            error_code=result.error_code,
        )

    return execute


def _admin_complex_domains(value: str) -> tuple[str, ...]:
    compact = re.sub(r"\s+", "", value).casefold()
    # A dated platform KPI request is one atomic metrics query even when it
    # names several dimensions (orders, GMV, users, stores, products).  Do not
    # fan it out to the generic user/order dashboard specialists: the metrics
    # tool applies one shared time window and one consistent business basis.
    if _is_admin_business_metrics_query(compact):
        return ("orders",)
    if _is_admin_user_asset_query(compact):
        return ("users",)
    if _is_admin_store_service_profile_query(compact):
        return ("stores",)
    if any(marker in compact for marker in ("ref_", "rfd_", "rap_", "rfp_")):
        return ("after_sale",)
    # A named user profile query may mention that user's order count.  Keep it as
    # one user specialist task instead of also appending the platform-wide order
    # summary, which would dilute the requested single-user answer.
    if any(
        marker in compact for marker in ("只展示这个用户", "这个用户的账号", "用户资料", "用户详情")
    ):
        return ("users",)
    if any(marker in compact for marker in ("只展示这家店", "这家店的经营", "店铺详情")):
        return ("stores",)
    if any(marker in compact for marker in ("只展示这个商品", "商品详情")):
        return ("catalog",)
    if any(term in compact for term in ("平台最需要处理", "平台风险", "运营风险", "最大风险")):
        return ("users", "stores", "orders", "runtime")
    if _requests_priority_follow_up(compact):
        return ("users", "stores", "orders", "runtime")
    explicit_runtime = any(
        term in compact
        for term in ("运行", "告警", "积压", "故障", "死信", "重放", "worker", "trace", "run_")
    )
    explicit_ai_governance = any(
        term in compact
        for term in (
            "知识库",
            "rag",
            "skill",
            "mcp",
            "模型配置",
            "模型质量",
            "评估",
            "发布门禁",
            "系统提示词",
            "提示词",
            "prompt",
            "agent配置",
            "agent版本",
        )
    )
    explicit_business_domain = any(
        term in compact
        for term in (
            "用户",
            "账号",
            "注册",
            "登录",
            "店铺",
            "商家",
            "商品",
            "上架",
            "下架",
            "审核",
            "库存",
            "订单",
            "支付",
            "物流",
            "履约",
            "营业额",
            "售后",
            "退款",
            "退货",
            "申诉",
            "客服",
            "工单",
        )
    )
    if explicit_runtime and not explicit_ai_governance and not explicit_business_domain:
        return ("runtime",)
    domains: list[str] = []
    rules = (
        ("users", ("用户", "账号", "注册", "登录")),
        ("stores", ("店铺", "商家")),
        ("catalog", ("商品", "上架", "下架", "审核", "库存")),
        ("orders", ("订单", "支付", "物流", "履约", "营业额")),
        ("after_sale", ("售后", "退款", "退货", "申诉")),
        ("support", ("客服", "工单", "人工队列", "接待")),
        (
            "ai_governance",
            (
                "知识库",
                "rag",
                "skill",
                "mcp",
                "模型配置",
                "模型质量",
                "评估",
                "系统提示词",
                "提示词",
                "prompt",
                "agent配置",
                "agent版本",
            ),
        ),
        (
            "runtime",
            (
                "运行",
                "告警",
                "积压",
                "故障",
                "死信",
                "重放",
                "worker",
                "trace",
                "run_",
            ),
        ),
    )
    for domain, terms in rules:
        if domain == "catalog" and "店铺商品" in compact:
            continue
        if any(term in compact for term in terms):
            domains.append(domain)
    return tuple(domains)


def _requires_strict_single_domain(value: str, audience: str) -> bool:
    compact = re.sub(r"\s+", "", value).casefold()
    if audience == "merchant":
        # Platform merchant rules are a policy/RAG question even though the
        # wording naturally contains "商品" and "审核".  Do not let a model
        # planner append an unrelated catalog diagnosis to this explicit,
        # single-domain request.
        return _merchant_complex_domains(value) == ("policy",)
    if audience != "admin":
        return False
    return (
        _is_admin_business_metrics_query(compact)
        or _is_admin_user_asset_query(compact)
        or _is_admin_store_service_profile_query(compact)
        or any(marker in compact for marker in ("只展示这个用户", "只展示这家店", "只展示这个商品"))
    )


def _is_admin_business_metrics_query(compact: str) -> bool:
    return any(
        marker in compact
        for marker in (
            "平台指标",
            "经营指标",
            "gmv",
            "成交额",
            "订单量",
            "新增用户",
            "新增店铺",
            "新增商品",
        )
    )


def _is_admin_user_asset_query(compact: str) -> bool:
    return "用户" in compact and any(
        marker in compact
        for marker in (
            "收货地址",
            "地址",
            "购物车",
            "收藏",
            "关注店铺",
            "余额",
            "资金流水",
            "钱包流水",
            "购买订单",
            "订单列表",
            "订单详情",
        )
    )


def _is_admin_store_service_profile_query(compact: str) -> bool:
    """Identify store service-profile requests without confusing them with after-sale cases."""

    return any(marker in compact for marker in ("店铺", "商家", "本店", "店")) and any(
        marker in compact
        for marker in (
            "服务资料",
            "发货地",
            "发货时效",
            "默认快递",
            "配送资料",
            "售后政策",
            "退换政策",
        )
    )


async def _load_operations_context_window(
    session: AsyncSession,
    checkpoint_store: AgentCheckpointStore,
    context: TrustedOperationsContext,
    security: SecurityService | None,
) -> Any:
    """Load continuity hints once; volatile facts are still read by domain tools."""

    with session.no_autoflush:
        context_window = await ContextWindowBuilder(session).build(
            context.conversation, context.trigger
        )
        if security is not None:
            context_window = await attach_rolling_summary(
                context_window,
                mysql=session,
                postgres=checkpoint_store.session,
                security=security,
                conversation=context.conversation,
                trigger=context.trigger,
                user_no=context.user.user_no,
                store_no=context.store.store_no if context.store else None,
            )
        return context_window.with_conversation_state(
            await ConversationStateRuntime(session).load(
                context.conversation,
                before_sequence=context.trigger.sequence_no,
            )
        )


async def _plan_operations_supervisor(
    context: TrustedOperationsContext,
    user_text: str,
    context_window: Any,
    model_gateway: ProviderOperationsModelGateway | None,
) -> tuple[OperationsSupervisorPlan, str, dict[str, object]]:
    continuation_intent = _operations_continuation_intent(context_window, user_text)
    continuation_label = "评价" if continuation_intent == "reviews" else "顾客会话"
    deterministic_plan = (
        OperationsSupervisorPlan(
            tasks=(
                OperationsSupervisorSubtask(
                    "task_1",
                    continuation_intent,
                    f"继续读取上一批{continuation_label}结果",
                ),
            ),
            coverage_complete=True,
        )
        if continuation_intent is not None
        else _deterministic_operations_plan(user_text, context.audience)
    )
    supervisor_plan = deterministic_plan
    planning_source = "deterministic_supervisor"
    planning_model_trace: dict[str, object] = {
        "status": "not_invoked",
        "provider_request_sent": False,
        "stage": "supervisor_planning",
        "reason": "当前运行未配置外部模型规划器，使用受控本地规划。",
    }
    if continuation_intent is not None:
        return (
            supervisor_plan,
            "typed_state_cursor_continuation",
            {
                "status": "not_invoked",
                "provider_request_sent": False,
                "stage": "supervisor_planning",
                "reason": "用户请求继续上一批列表，已按版本化会话状态恢复领域和签名游标。",
            },
        )
    if model_gateway is None:
        return supervisor_plan, planning_source, planning_model_trace
    planning_started_at = time.monotonic()
    try:
        provider_plan = await hard_deadline(
            model_gateway.plan_tasks(
                context_window.planning_input(user_text),
                context.agent_definition.agent_code,
            ),
            budget_seconds=MODEL_PLANNING_BUDGET_SECONDS,
        )
        required_routes = {
            (task.intent, _tool_for_query(task.intent, context.audience, task.objective))
            for task in deterministic_plan.tasks
        }
        provider_routes = {
            (task.intent, _tool_for_query(task.intent, context.audience, task.objective))
            for task in provider_plan.tasks
        }
        missing_routes = sorted(required_routes - provider_routes)
        missing_domains = [f"{intent}:{tool}" for intent, tool in missing_routes]
        repair_attempted = bool(missing_routes)
        repair_succeeded = False
        if missing_domains:
            repair_input = (
                context_window.planning_input(user_text)
                + "\n\nSupervisor verification feedback: the previous plan omitted "
                + "these explicitly "
                + "requested business domains: "
                + ", ".join(missing_domains)
                + ". Re-plan the original current message, preserve every user goal, and do not "
                + "collapse named domains into overview."
            )
            repaired_plan = await hard_deadline(
                model_gateway.plan_tasks(
                    repair_input,
                    context.agent_definition.agent_code,
                ),
                budget_seconds=MODEL_PLANNING_BUDGET_SECONDS,
            )
            repaired_routes = {
                (task.intent, _tool_for_query(task.intent, context.audience, task.objective))
                for task in repaired_plan.tasks
            }
            if required_routes.issubset(repaired_routes):
                provider_plan = repaired_plan
                repair_succeeded = True
        supervisor_plan = _merge_operations_supervisor_plans(
            provider_plan,
            deterministic_plan,
            audience=context.audience,
            request_text=user_text,
        )
        planning_source = (
            "provider_model_supervisor_repaired"
            if repair_succeeded
            else "deterministic_goal_coverage_fallback"
            if repair_attempted
            else "provider_model_supervisor"
        )
        planning_model_trace = {
            "status": "completed",
            "provider_request_sent": True,
            "stage": "supervisor_planning",
            "model": model_gateway.model_name,
            "model_latency_ms": int((time.monotonic() - planning_started_at) * 1000),
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "usage_status": "provider_usage_not_returned_by_planning_contract",
            "coverage_repair_attempted": repair_attempted,
            "coverage_repair_succeeded": repair_succeeded,
            "missing_domains_before_repair": missing_domains,
        }
    except (ModelGatewayError, TimeoutError) as exc:
        context.run.degraded_reason = model_failure_code(exc, "planning")
        planning_model_trace = {
            "status": "failed",
            "provider_request_sent": True,
            "stage": "supervisor_planning",
            "model": model_gateway.model_name,
            "model_latency_ms": int((time.monotonic() - planning_started_at) * 1000),
            "error_code": context.run.degraded_reason,
            "fallback_used": True,
        }
    return supervisor_plan, planning_source, planning_model_trace


def _operations_action_domain(action_type: str) -> str | None:
    if action_type.startswith("merchant_store_"):
        return "profile"
    if action_type.startswith("merchant_product_") or action_type in {
        "merchant_inventory_set",
        "merchant_price_set",
    }:
        return "catalog"
    if action_type.startswith("merchant_shipment_"):
        return "orders"
    if action_type == "merchant_review_reply":
        return "reviews"
    if action_type.startswith("merchant_refund_"):
        return "after_sale"
    if action_type.startswith("merchant_support_"):
        return "service"
    if action_type == "merchant_store_policy_manage":
        return "policy"
    if action_type.startswith("admin_user_"):
        return "users"
    if action_type.startswith("admin_store_"):
        return "stores"
    if action_type.startswith("admin_product_"):
        return "catalog"
    if action_type.startswith("admin_shipment_"):
        return "orders"
    if action_type == "admin_order_cancel":
        return "orders"
    if action_type.startswith("admin_refund_"):
        return "after_sale"
    if action_type.startswith("admin_support_"):
        return "support"
    if action_type.startswith("admin_ai_") or action_type.startswith("admin_knowledge_"):
        return "ai_governance"
    if action_type.startswith("admin_dead_letter_"):
        return "runtime"
    return None


def _explicit_goal_domains(
    deterministic_plan: OperationsSupervisorPlan, request_text: str
) -> tuple[str, ...]:
    """Return named multi-domain goals used to verify, not replace, model planning."""

    if len(deterministic_plan.tasks) < 2:
        return ()
    current = re.sub(r"\s+", "", request_text).casefold()
    separators = ("、", "，", ",", "；", ";", "同时", "以及", "并且", "和")
    if not any(marker in current for marker in separators):
        return ()
    return tuple(task.intent for task in deterministic_plan.tasks)


def _deterministic_operations_plan(value: str, audience: str) -> OperationsSupervisorPlan:
    primary = _deterministic_intent(value, audience)
    domains = (
        _admin_complex_domains(value) if audience == "admin" else _merchant_complex_domains(value)
    )
    if _requires_strict_single_domain(value, audience) and domains:
        primary = domains[0]
    intents: list[str] = [primary]
    for domain in domains:
        if domain not in intents:
            intents.append(domain)
    if len(intents) > 1 and "overview" in intents:
        intents.remove("overview")
    task_specs: list[tuple[str, str]] = []
    compact = re.sub(r"\s+", "", value).casefold()
    for intent in intents:
        if audience == "admin" and intent == "runtime":
            runtime_objectives: list[str] = []
            if any(marker in compact for marker in ("死信", "失败事件", "待处理死信")):
                runtime_objectives.append("查询平台待处理死信")
            if any(
                marker in compact
                for marker in (
                    "失败的agent运行",
                    "失败agent运行",
                    "失败运行",
                    "最近失败",
                    "agent故障",
                )
            ):
                runtime_objectives.append("查询最近失败的 Agent 运行")
            if runtime_objectives:
                task_specs.extend((intent, objective) for objective in runtime_objectives)
                continue
        task_specs.append((intent, value[:200]))
    return OperationsSupervisorPlan(
        tuple(
            OperationsSupervisorSubtask(f"task_{index}", intent, objective[:200])
            for index, (intent, objective) in enumerate(task_specs[:8], start=1)
        )
    )


def _merge_operations_supervisor_plans(
    provider: OperationsSupervisorPlan,
    deterministic: OperationsSupervisorPlan,
    *,
    audience: str,
    request_text: str = "",
) -> OperationsSupervisorPlan:
    """Normalize a valid model plan without appending keyword-derived domains."""

    def normalize(intent: str) -> str:
        if audience == "admin" and intent == "inventory":
            return "catalog"
        if audience == "merchant" and intent in {"users", "stores", "runtime"}:
            return "overview"
        return intent

    provider_tasks = [
        OperationsSupervisorSubtask(task.subtask_key, normalize(task.intent), task.objective)
        for task in provider.tasks
    ]
    provider_was_changed = any(
        normalized.intent != original.intent
        for normalized, original in zip(provider_tasks, provider.tasks, strict=True)
    )
    if len(provider_tasks) > 1:
        without_overview = [task for task in provider_tasks if task.intent != "overview"]
        provider_was_changed = provider_was_changed or len(without_overview) != len(provider_tasks)
        provider_tasks = without_overview
    deterministic_tasks = [
        OperationsSupervisorSubtask(task.subtask_key, normalize(task.intent), task.objective)
        for task in deterministic.tasks
    ]
    display_focus = _explicit_operations_display_focus(request_text)
    if (
        display_focus
        and len(deterministic_tasks) == 1
        and deterministic_tasks[0].intent == display_focus
    ):
        # "只展示售后卡片，不要展示普通订单卡片" is an explicit scope
        # constraint, not three business goals.  Keep a model planner from
        # treating the negative examples as work that should be delegated.
        provider_was_changed = provider_was_changed or provider_tasks != deterministic_tasks
        provider_tasks = deterministic_tasks
    if _requires_strict_single_domain(request_text, audience):
        provider_was_changed = provider_was_changed or provider_tasks != deterministic_tasks
        provider_tasks = deterministic_tasks
    required_domains = _explicit_goal_domains(deterministic, request_text)
    provider_domains = {task.intent for task in provider_tasks}
    deterministic_routes = {
        (task.intent, _tool_for_query(task.intent, audience, task.objective))
        for task in deterministic_tasks
    }
    provider_routes = {
        (task.intent, _tool_for_query(task.intent, audience, task.objective))
        for task in provider_tasks
    }
    if required_domains and (
        any(intent not in provider_domains for intent in required_domains)
        or not deterministic_routes.issubset(provider_routes)
    ):
        provider_tasks = deterministic_tasks
        provider_was_changed = True
    if not provider.coverage_complete:
        provider_tasks = deterministic_tasks
        provider_was_changed = True
    unique: list[OperationsSupervisorSubtask] = []
    seen: set[tuple[str, str]] = set()
    for task in provider_tasks or deterministic_tasks:
        route = (task.intent, _tool_for_query(task.intent, audience, task.objective))
        if route in seen:
            provider_was_changed = True
            continue
        seen.add(route)
        unique.append(
            OperationsSupervisorSubtask(
                f"task_{len(unique) + 1}", task.intent, task.objective[:200]
            )
        )
    # An ordinal follow-up to a ranked merchant diagnosis has one conversational
    # goal but several volatile evidence dependencies.  A model may correctly
    # select only the apparent leading domain; however, ranking it against
    # inventory, fulfilment and catalog conditions requires refreshing all three
    # facts.  Close this evidence dependency set without rewriting ordinary
    # model plans or adding new user goals.
    if audience == "merchant" and _requests_priority_follow_up(
        re.sub(r"\s+", "", request_text).casefold()
    ):
        required_intents = ("catalog", "inventory", "orders")
        deterministic_by_intent = {task.intent: task for task in deterministic_tasks}
        for required_intent in required_intents:
            if any(intent == required_intent for intent, _tool in seen):
                continue
            dependency = deterministic_by_intent.get(required_intent)
            if dependency is None:
                continue
            seen.add(
                (
                    required_intent,
                    _tool_for_query(required_intent, audience, dependency.objective),
                )
            )
            unique.append(
                OperationsSupervisorSubtask(
                    f"task_{len(unique) + 1}",
                    required_intent,
                    dependency.objective[:200],
                )
            )
            provider_was_changed = True
    if not provider_was_changed and len(unique) == len(provider.tasks):
        return provider
    return OperationsSupervisorPlan(tuple(unique[:8]), confidence=provider.confidence)


def _operations_goal_ledger(plan: OperationsSupervisorPlan) -> list[dict[str, str]]:
    return [
        {
            "goal_key": goal.goal_key,
            "description": goal.description,
            "assigned_task_key": goal.assigned_task_key,
        }
        for goal in plan.goal_ledger
    ]


def _merchant_complex_domains(value: str) -> tuple[str, ...]:
    compact = re.sub(r"\s+", "", value).casefold()
    # Excluded presentation examples describe what the user does *not* want.
    # Remove those clauses before detecting positive business goals so words
    # such as "商品" and "普通订单" cannot create irrelevant delegations.
    compact = re.sub(
        r"(?:不要|不需要|无需|不必)(?:展示|显示)[^。！？!?；;]*",
        "",
        compact,
    )
    if _requests_priority_follow_up(compact):
        return ("catalog", "inventory", "orders")
    domains: list[str] = []
    if any(
        term in compact
        for term in ("店铺资料", "店铺简介", "店铺名称", "店名", "logo", "发货地", "营业状态")
    ):
        domains.append("profile")
    inventory_requested = any(term in compact for term in ("库存", "缺货", "现货", "补货", "超卖"))
    policy_requested = any(
        term in compact for term in ("政策", "规则", "退换", "包邮", "发货承诺", "禁售", "违禁")
    )
    policy_only_catalog_phrase = policy_requested and any(
        term in compact
        for term in ("商品审核", "审核规则", "禁售商品", "禁售规则", "违禁商品", "上架规则")
    )
    catalog_requested = (
        any(term in compact for term in ("价格", "在售", "本店商品", "商品列表", "商品详情"))
        or ("商品" in compact and not policy_only_catalog_phrase)
    ) or (not inventory_requested and any(term in compact for term in ("款式", "sku")))
    if catalog_requested:
        domains.append("catalog")
    if inventory_requested:
        domains.append("inventory")
    if any(term in compact for term in ("订单", "履约", "发货", "运输", "营业额", "收益")):
        domains.append("orders")
    if any(term in compact for term in ("售后", "退款申请", "退货", "退款进度")):
        domains.append("after_sale")
    if any(term in compact for term in ("评价", "评分", "回复评价", "差评")):
        domains.append("reviews")
    if any(term in compact for term in ("顾客咨询", "客服", "工单", "接待", "未读消息")):
        domains.append("service")
    if policy_requested:
        domains.append("policy")
    return tuple(domains)


def _requests_priority_follow_up(compact: str) -> bool:
    ordinal = any(
        term in compact for term in ("第一项", "第二项", "第三项", "第1项", "第2项", "第3项")
    )
    priority_reference = any(
        term in compact for term in ("优先项", "最优先", "排在最前", "先做什么", "先处理什么")
    )
    return ordinal or priority_reference


def _priority_focus_index(value: str) -> int | None:
    compact = re.sub(r"\s+", "", value).casefold()
    for index, markers in (
        (1, ("第一项", "第1项")),
        (2, ("第二项", "第2项")),
        (3, ("第三项", "第3项")),
    ):
        if any(marker in compact for marker in markers):
            return index
    return 1 if any(marker in compact for marker in ("最优先", "排在最前", "排第一")) else None


def _operations_answer_satisfies_request(user_text: str, answer: str) -> bool:
    """Validate small user-visible commitments after free-form model synthesis."""

    focus = _priority_focus_index(user_text)
    if focus is None:
        return True
    markers = {
        1: ("第一项", "第1项", "第 1 项"),
        2: ("第二项", "第2项", "第 2 项"),
        3: ("第三项", "第3项", "第 3 项"),
    }
    return any(marker in answer for marker in markers[focus])


def _requested_priority_count(value: str) -> int | None:
    compact = re.sub(r"\s+", "", value).casefold()
    match = re.search(r"(?:只列|列出|给我)([一二两三四五\d]+)项", compact)
    if match is None:
        return None
    raw = match.group(1)
    number = (
        int(raw)
        if raw.isdigit()
        else {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}.get(raw, 3)
    )
    return max(1, min(number, 4))


def _operations_specialist(audience: str, domain: str) -> tuple[str, str, str]:
    return _admin_specialist(domain) if audience == "admin" else _merchant_specialist(domain)


def _admin_specialist(domain: str) -> tuple[str, str, str]:
    return {
        "users": ("governance_users", "governance.users.search", "查询匹配用户并核对账号状态"),
        "stores": ("governance_stores", "governance.stores.search", "查询匹配店铺并核对经营状态"),
        "catalog": (
            "governance_catalog",
            "governance.catalog.search",
            "查询匹配商品并核对销售状态",
        ),
        "orders": ("governance_orders", "governance.order_summary", "核对订单状态汇总"),
        "after_sale": (
            "governance_after_sale",
            "governance.after_sale_summary",
            "核对售后申请状态与积压",
        ),
        "support": (
            "governance_support",
            "governance.support_summary",
            "核对平台人工服务队列",
        ),
        "ai_governance": (
            "governance_ai",
            "governance.ai_summary",
            "核对 Agent、知识与质量治理状态",
        ),
        "runtime": ("observability", "observability.runtime_health", "核对运行时健康和积压"),
    }[domain]


def _merchant_specialist(domain: str) -> tuple[str, str, str]:
    return {
        "profile": (
            "merchant_profile",
            "store_ops.profile.get",
            "核对当前店铺公开资料、营业状态和资料版本",
        ),
        "catalog": (
            "merchant_catalog",
            "store_ops.catalog_summary",
            "核对本店在售商品、款式和实时可售库存",
        ),
        "inventory": (
            "merchant_inventory",
            "store_ops.inventory_risks",
            "核对本店缺货和低库存风险",
        ),
        "orders": ("merchant_orders", "store_ops.order_summary", "核对本店订单履约与已确认营业额"),
        "reviews": (
            "merchant_review_service",
            "store_ops.review_summary",
            "核对本店评价、评分与待回复评价",
        ),
        "service": (
            "merchant_customer_service",
            "store_ops.service_summary",
            "核对本店顾客咨询与人工服务队列",
        ),
        "policy": (
            "merchant_policy",
            "store_ops.policy_summary",
            "核对本店已发布服务政策",
        ),
        "after_sale": (
            "merchant_after_sale",
            "store_ops.after_sale.list",
            "核对本店售后申请、处理状态和待办",
        ),
    }[domain]


def _specialist_label(code: str) -> str:
    return {
        "governance_users": "用户治理助手: 已核对用户状态",
        "governance_stores": "店铺治理助手: 已核对店铺与商品状态",
        "governance_catalog": "商品治理助手: 已核对匹配商品与销售状态",
        "governance_orders": "订单助手: 已核对订单状态",
        "governance_payments": "支付治理助手: 已核对支付流水与回调",
        "governance_logistics": "物流治理助手: 已核对包裹与轨迹",
        "governance_metrics": "经营指标助手: 已核对平台经营指标",
        "observability": "运行诊断助手: 已核对服务健康",
        "merchant_catalog": "商品助手: 已核对商品、款式和实时库存",
        "merchant_profile": "店铺资料助手: 已核对公开资料与营业状态",
        "merchant_inventory": "库存助手: 已核对缺货和低库存风险",
        "merchant_orders": "履约助手: 已核对订单与营业额",
        "merchant_review_service": "评价助手: 已核对评分与待回复评价",
        "merchant_customer_service": "客服助手: 已核对顾客咨询与人工队列",
        "merchant_policy": "规则助手: 已核对本店已发布政策",
        "merchant_after_sale": "售后助手: 已核对本店售后申请与待办",
        "governance_after_sale": "售后治理助手: 已核对售后状态",
        "governance_support": "客服治理助手: 已核对人工服务队列",
        "governance_ai": "AI 治理助手: 已核对 Agent 与知识状态",
    }.get(code, "领域助手: 已完成只读核对")


def _specialist_public_name(code: str) -> str:
    return {
        "governance_users": "用户治理 Agent",
        "governance_stores": "店铺治理 Agent",
        "governance_catalog": "商品治理 Agent",
        "governance_orders": "订单治理 Agent",
        "governance_payments": "支付治理 Agent",
        "governance_logistics": "物流治理 Agent",
        "governance_metrics": "经营指标 Agent",
        "observability": "运行诊断 Agent",
        "merchant_catalog": "商品运营 Agent",
        "merchant_profile": "店铺与商品运营 Agent",
        "merchant_inventory": "库存风险 Agent",
        "merchant_orders": "订单履约 Agent",
        "merchant_review_service": "评价运营 Agent",
        "merchant_customer_service": "顾客服务 Agent",
        "merchant_policy": "店铺政策 Agent",
        "merchant_after_sale": "售后评价 Agent",
        "governance_after_sale": "售后治理 Agent",
        "governance_support": "客服治理 Agent",
        "governance_ai": "AI 治理 Agent",
    }.get(code, "受限领域 Agent")


def _specialist_tool_code(code: str) -> str:
    return {
        "governance_users": "governance.users.search",
        "governance_stores": "governance.stores.search",
        "governance_catalog": "governance.catalog.search",
        "governance_orders": "governance.order_summary",
        "governance_payments": "governance.trade.payment_timeline",
        "governance_logistics": "governance.trade.shipments.get",
        "governance_metrics": "governance.metrics.query",
        "observability": "observability.runtime_health",
        "merchant_catalog": "store_ops.catalog_summary",
        "merchant_profile": "store_ops.profile.get",
        "merchant_inventory": "store_ops.inventory_risks",
        "merchant_orders": "store_ops.order_summary",
        "merchant_review_service": "store_ops.review_summary",
        "merchant_customer_service": "store_ops.service_summary",
        "merchant_policy": "store_ops.policy_summary",
        "merchant_after_sale": "store_ops.after_sale.list",
        "governance_after_sale": "governance.after_sale_summary",
        "governance_support": "governance.support_summary",
        "governance_ai": "governance.ai_summary",
    }.get(code, "")


def _operations_partial_result_suffix(data: Mapping[str, Any]) -> str:
    summary = data.get("subtask_summary")
    if not isinstance(summary, Mapping) or summary.get("status") == "succeeded":
        return ""
    success_count = int(summary.get("success_count") or 0)
    partial_count = int(summary.get("partial_count") or 0)
    failed_count = int(summary.get("failed_count") or 0)
    retained_count = success_count + partial_count
    if retained_count and failed_count:
        return (
            f" 其中 {failed_count} 项未完成，已保留 {retained_count} 项可靠结果；"
            "可在失败卡片中只重试对应任务。"
        )
    if partial_count:
        return " 部分结果仍不完整，可在下方卡片中只重试对应任务。"
    if failed_count:
        return f" 本次 {failed_count} 项均未完成，可在失败卡片中逐项重试。"
    return ""


def _render_merchant_multi_agent(data: Mapping[str, Any]) -> str:
    specialists = data.get("specialists")
    if not isinstance(specialists, dict) or not specialists:
        return (
            "本次经营诊断没有取得足够的可信结果，请从失败卡片逐项重试。"
            + _operations_partial_result_suffix(data)
        )
    products: list[Mapping[str, Any]] = []
    products_loaded = False
    order_counts: Mapping[str, Any] = {}
    low_stock_count = 0
    inventory_skus: list[Mapping[str, Any]] = []
    completed_revenue: Mapping[str, Any] = {}
    thirty_day_revenue: Mapping[str, Any] = {}
    retrieved_domains: set[str] = set()
    for result in specialists.values():
        if not isinstance(result, dict):
            continue
        safe_data = result.get("data")
        if not isinstance(safe_data, dict):
            continue
        specialist_code = str(result.get("specialist") or "")
        if specialist_code:
            retrieved_domains.add(specialist_code)
        candidate_products = safe_data.get("on_sale_products")
        if isinstance(candidate_products, list):
            products = [item for item in candidate_products if isinstance(item, Mapping)]
            products_loaded = True
        candidate_counts = safe_data.get("order_status_counts")
        if isinstance(candidate_counts, Mapping):
            order_counts = candidate_counts
        candidate_revenue = safe_data.get("completed_order_revenue")
        if isinstance(candidate_revenue, Mapping):
            completed_revenue = candidate_revenue
        candidate_thirty_day_revenue = safe_data.get("thirty_day_revenue")
        if isinstance(candidate_thirty_day_revenue, Mapping):
            thirty_day_revenue = candidate_thirty_day_revenue
        candidate_inventory_skus = safe_data.get("inventory_skus")
        if isinstance(candidate_inventory_skus, list):
            inventory_skus.extend(
                item for item in candidate_inventory_skus if isinstance(item, Mapping)
            )
        low_stock_count = max(low_stock_count, int(safe_data.get("low_stock_sku_count", 0)))
    risks: list[str] = []
    if low_stock_count:
        risks.append(f"{low_stock_count} 个款式达到低库存或缺货阈值")
    pending_fulfillment = sum(
        int(order_counts.get(key, 0)) for key in ("paid", "pending_shipment", "shipped")
    )
    if pending_fulfillment:
        risks.append(f"{pending_fulfillment} 单仍在待履约或运输阶段")
    store = data.get("store")
    store_name = str(store.get("store_name")) if isinstance(store, Mapping) else "本店"
    domain_labels = {
        "merchant_profile": "店铺公开资料",
        "merchant_policy": "已发布服务政策",
        "merchant_after_sale": "售后申请与待办",
        "merchant_review_service": "评价与待回复事项",
        "merchant_customer_service": "顾客咨询与人工队列",
    }
    informational_domains = [
        label for code, label in domain_labels.items() if code in retrieved_domains
    ]
    commerce_domains = {
        "merchant_catalog",
        "merchant_inventory",
        "merchant_orders",
    }
    if informational_domains and not (retrieved_domains & commerce_domains):
        return (
            f"已协同核对{store_name}的{'、'.join(informational_domains)}。"
            "当前结果与处理入口已分区整理在下方卡片中。"
        ) + _operations_partial_result_suffix(data)
    if inventory_skus and thirty_day_revenue:
        zero_stock_count = sum(
            int(item.get("available_quantity", 0)) == 0 for item in inventory_skus
        )
        return (
            f"已协同核对{store_name}的款式库存与收入：当前有 {zero_stock_count} 个款式"
            f"实时可售为 0，近 30 天已确认营业额为 "
            f"{thirty_day_revenue.get('display', '¥0.00')}。"
            "两项结果和处理入口已分别整理在下方卡片中；本次只查询，没有修改数据。"
        ) + _operations_partial_result_suffix(data)
    checked_scopes = ["库存", "订单"]
    if products_loaded:
        checked_scopes.insert(0, f"{len(products)} 件在售商品")
    overview = (
        f"已协同核对{store_name}的{'、'.join(checked_scopes)}，已确认营业额 "
        f"{completed_revenue.get('display', '¥0.00')}。"
    )
    if risks:
        return (
            overview
            + "建议按优先级先处理"
            + "、".join(risks)
            + "，再复盘在售商品表现。三项行动和入口已整理在下方卡片中。"
        ) + _operations_partial_result_suffix(data)
    zero_sales = sum(int(product.get("sales_count", 0)) <= 0 for product in products)
    growth_hint = (
        f"{zero_sales} 件商品暂无销量，先复盘商品信息和价格"
        if zero_sales
        else "先复盘在售商品表现并放大已有销量"
    )
    return (
        overview
        + "当前没有紧急库存或履约告警。建议依次处理: "
        + growth_hint
        + ", 检查订单与已确认营业额, 保持库存风险巡检。三项行动和入口已整理在下方卡片中。"
    ) + _operations_partial_result_suffix(data)


def _render_multi_agent(data: Mapping[str, Any], *, user_text: str = "") -> str:
    specialists = data.get("specialists")
    if not isinstance(specialists, dict) or not specialists:
        return (
            "跨域诊断没有取得足够的可信结果，请从失败卡片逐项重试。"
            + _operations_partial_result_suffix(data)
        )
    specialist_data: list[Mapping[str, Any]] = []
    completed_specialists: set[str] = set()
    for result in specialists.values():
        if not isinstance(result, Mapping):
            continue
        safe_data = result.get("data")
        if isinstance(safe_data, Mapping):
            specialist_data.append(safe_data)
            specialist_code = str(result.get("specialist") or "")
            if specialist_code:
                completed_specialists.add(specialist_code)
    if _explicit_operations_display_focus(user_text) == "after_sale":
        refunds: list[Mapping[str, Any]] = []
        for result in specialists.values():
            if not isinstance(result, Mapping):
                continue
            specialist_code = str(result.get("specialist") or "")
            if specialist_code not in {"governance_after_sale", "merchant_after_sale"}:
                continue
            safe_data = result.get("data")
            raw_refunds = (
                safe_data.get(
                    "refunds" if specialist_code == "governance_after_sale" else "recent_refunds"
                )
                if isinstance(safe_data, Mapping)
                else None
            )
            if isinstance(raw_refunds, list):
                refunds.extend(item for item in raw_refunds if isinstance(item, Mapping))
        pending = [
            item
            for item in refunds
            if item.get("status")
            in {
                "submitted",
                "merchant_review",
                "approved",
                "waiting_return",
                "returning",
                "received",
                "refunding",
            }
        ]
        focused = pending if len(pending) == 1 else refunds if len(refunds) == 1 else []
        if focused:
            refund = focused[0]
            status = str(refund.get("status") or "")
            status_label = {
                "submitted": "待受理",
                "merchant_review": "商家审核中",
                "approved": "已同意",
                "waiting_return": "待顾客退货",
                "returning": "退货中",
                "received": "商家已收货",
                "refunding": "退款中",
            }.get(status, status or "状态未知")
            amount = refund.get("requested_amount")
            amount_display = (
                str(amount.get("display") or "¥0.00") if isinstance(amount, Mapping) else "¥0.00"
            )
            next_step = {
                "submitted": "下一步由平台或店铺先受理申请并核对材料",
                "merchant_review": "下一步由店铺核对申请原因、订单商品与履约情况后完成审核",
                "approved": "下一步按售后类型等待退货或进入退款处理",
                "waiting_return": "下一步等待顾客按页面指引寄回商品",
                "returning": "下一步跟踪退货包裹并等待店铺收货",
                "received": "下一步进入退款处理",
                "refunding": "下一步等待支付渠道返回退款结果",
            }.get(status, "下一步请从售后卡片进入治理页核对")
            store_text = f"，店铺“{refund.get('store_name')}”" if refund.get("store_name") else ""
            subject_text = (
                f"已定位这笔待处理售后：顾客 {refund.get('customer_name') or '未知'}{store_text}，"
            )
            return (
                subject_text
                + (
                    f"对应订单 {refund.get('order_id') or '—'}，申请金额 {amount_display}，"
                    f"当前为{status_label}。{next_step}；本次只查询，没有执行审批或退款。"
                )
                + _operations_partial_result_suffix(data)
            )
        if completed_specialists.intersection({"governance_after_sale", "merchant_after_sale"}):
            return (
                "已定位刚才那笔待处理售后。关联顾客、店铺、订单、申请金额和当前状态"
                "已集中在下方售后卡片中；下一步由对应店铺核对申请原因、订单商品与"
                "履约情况后进入售后审核节点。本次只查询，没有执行审批或退款。"
            ) + _operations_partial_result_suffix(data)
    for safe_data in specialist_data:
        if safe_data.get("query_mode") not in {"list", "detail"}:
            continue
        recent_orders = safe_data.get("recent_orders")
        if not isinstance(recent_orders, list):
            continue
        filters = safe_data.get("applied_filters")
        scope_parts: list[str] = []
        if isinstance(filters, Mapping):
            customer_name = filters.get("customer_name")
            store_name = filters.get("store_name")
            statuses = filters.get("statuses")
            if customer_name:
                scope_parts.append(f"用户 {customer_name}")
            if store_name:
                scope_parts.append(f"{store_name}")
            if isinstance(statuses, list) and statuses:
                scope_parts.append("指定状态")
        # A broad platform diagnosis also delegates to the order specialist.
        # Its unfiltered order list is only one evidence set and must not
        # replace the Supervisor's cross-domain risk synthesis.  A unique
        # detail result, a scoped order query, or an order-only run may use the
        # concise order response directly.
        order_focused = (
            safe_data.get("query_mode") == "detail" or bool(scope_parts) or len(specialists) == 1
        )
        if not order_focused:
            continue
        matched = len(recent_orders)
        if matched == 0:
            return (
                "当前没有符合顾客、店铺、状态、时间或订单号筛选条件的订单。"
                + _operations_partial_result_suffix(data)
            )
        if safe_data.get("query_mode") == "detail":
            return (
                "已找到该订单，顾客、店铺、商品、金额与状态已整理在可操作订单卡片中。"
                + _operations_partial_result_suffix(data)
            )
        scope = "、".join(scope_parts)
        prefix = f"{scope}的" if scope else "符合条件的"
        return (
            f"已找到{prefix} {matched} 笔订单，商品、金额与状态已整理在下方可操作卡片中。"
            + _operations_partial_result_suffix(data)
        )
    cross_domain_results: list[str] = []
    for safe_data in specialist_data:
        shipments = safe_data.get("shipments")
        if isinstance(shipments, list):
            cross_domain_results.append(f"运输中物流包裹 {len(shipments)} 个")
        dead_letters = safe_data.get("dead_letters")
        if isinstance(dead_letters, list):
            open_count = int(safe_data.get("open_count") or 0)
            cross_domain_results.append(f"待处理死信 {open_count} 条")
        runs = safe_data.get("runs")
        if isinstance(runs, list):
            qualifier = "失败的 " if safe_data.get("status_filter") == "failed" else ""
            cross_domain_results.append(f"最近{qualifier}Agent 运行 {len(runs)} 条")
    if len(cross_domain_results) >= 2:
        return (
            "已由对应领域 Agent 完成只读核对："
            + "、".join(cross_domain_results)
            + "。三类结果已分别整理为可操作卡片；本次没有执行重放或修改。"
        ) + _operations_partial_result_suffix(data)
    narrow_domain_labels = {
        "governance_metrics": "平台指标",
        "governance_payments": "支付流水与回调",
        "governance_logistics": "物流包裹与轨迹",
    }
    completed_narrow_domains = [
        label
        for specialist, label in narrow_domain_labels.items()
        if specialist in completed_specialists
    ]
    if completed_narrow_domains:
        scopes = "、".join(completed_narrow_domains)
        return (
            f"已由对应领域 Agent 完成{scopes}的只读核对。"
            "实时事实、不可变事件和可用治理入口已分别整理在下方卡片中。"
        ) + _operations_partial_result_suffix(data)
    all_metrics: dict[str, int] = {}
    for safe_data in specialist_data:
        metrics = _flatten_summary(safe_data)
        all_metrics.update({key: value for key, value in metrics.items() if isinstance(value, int)})
    risks: list[str] = []
    if all_metrics.get("stale_pending_outbox_events", 0) > 0:
        risks.append(
            f"仍有 {all_metrics['stale_pending_outbox_events']} 条 Outbox 事件超过 5 分钟未处理"
        )
    if all_metrics.get("unrecovered_agent_failures", 0) > 0:
        risks.append(f"存在 {all_metrics['unrecovered_agent_failures']} 个尚未恢复的 Agent 故障")
    if (
        "product_status_counts.on_sale" in all_metrics
        and all_metrics["product_status_counts.on_sale"] == 0
    ):
        risks.append("平台当前没有在售商品")
    failed_runs_24h = all_metrics.get("failed_agent_runs_24h", 0)
    recovered_runs = all_metrics.get("successful_runs_after_latest_failure", 0)
    recovery = ""
    if failed_runs_24h > 0 and all_metrics.get("unrecovered_agent_failures", 0) == 0:
        recovery = (
            f"过去 24 小时的 {failed_runs_24h} 次失败后已有 {recovered_runs} 次成功运行，"
            "当前判定已恢复。"
        )
    checked = len([item for item in specialists.values() if isinstance(item, Mapping)])
    if risks:
        return (
            f"已由 {checked} 个专业 Agent 协同完成只读诊断。需要优先关注: "
            + "、".join(risks)
            + "。具体指标和治理入口已整理在下方卡片中。"
            + recovery
        ) + _operations_partial_result_suffix(data)
    return (
        f"已由 {checked} 个专业 Agent 协同完成只读诊断，当前未发现事件积压、"
        "未恢复的 Agent 故障或无在售商品风险。具体指标已整理在下方卡片中。" + recovery
    ) + _operations_partial_result_suffix(data)


def _render_admin_priority_follow_up(data: Mapping[str, Any], *, user_text: str = "") -> str:
    diagnosis_prefix = "专业 Agent 已重新完成只读诊断: "
    specialists = data.get("specialists")
    metrics: dict[str, int] = {}
    if isinstance(specialists, Mapping):
        for result in specialists.values():
            if not isinstance(result, Mapping) or not isinstance(result.get("data"), Mapping):
                continue
            flattened = _flatten_summary(result["data"])
            metrics.update(
                {key: value for key, value in flattened.items() if isinstance(value, int)}
            )
    stale_events = metrics.get("stale_pending_outbox_events", 0)
    unrecovered = metrics.get("unrecovered_agent_failures", 0)
    failed = metrics.get("failed_agent_runs_24h", 0)
    recovered = metrics.get("successful_runs_after_latest_failure", 0)
    focus = _priority_focus_index(user_text)
    if focus == 2:
        completed = metrics.get("order_status_counts.completed", 0)
        shipped = metrics.get("order_status_counts.shipped", 0)
        pending_shipment = metrics.get("order_status_counts.pending_shipment", 0)
        return diagnosis_prefix + (
            "第二项是交易履约，因为它直接关系到顾客是否按时收到商品。"
            f"当前已完成 {completed} 笔、运输中 {shipped} 笔、待发货 {pending_shipment} 笔。"
            "先打开“平台订单状态”卡片进入订单治理，优先核对待发货和运输异常; 本次只读，"
            "没有修改订单。"
        )
    if focus == 3:
        active_stores = metrics.get("store_status_counts.active", 0)
        on_sale = metrics.get("product_status_counts.on_sale", 0)
        return diagnosis_prefix + (
            f"第三项是店铺与商品治理。当前营业中店铺 {active_stores} 家、在售商品 {on_sale} 件。"
            "先打开“店铺与商品状态”卡片抽查暂停店铺、无在售商品店铺和异常商品状态; "
            "没有证据时不要批量暂停或下架。"
        )
    if stale_events:
        return diagnosis_prefix + (
            f"第一项最重要，因为有 {stale_events} 条 Outbox 事件已超过 5 分钟仍未投递，"
            "它可能延迟订单、消息或通知链路。今天先打开“Agent 与事件链路”卡片，"
            "核对最早事件的类型、创建时间和重试状态。确认影响范围前不要删除或强制重放。"
        )
    if unrecovered:
        return diagnosis_prefix + (
            f"第一项最重要，因为当前仍有 {unrecovered} 个 Agent 故障尚未出现成功恢复证据。"
            "今天先打开“Agent 与事件链路”卡片，定位最新失败运行的错误码和关联请求。"
        )
    if failed:
        return diagnosis_prefix + (
            f"第一项是运行诊断。重新核对后没有未恢复故障，也没有超过 5 分钟的事件积压。"
            "过去 24 小时虽有 "
            f"{failed} 次失败，但之后已有 {recovered} 次成功运行，所以不应把它当成当前阻断。"
            "今天先打开“Agent 与事件链路”卡片抽查最新一次失败原因，再保持常规监控。"
        )
    return diagnosis_prefix + (
        "第一项是运行诊断。重新核对后没有未恢复 Agent 故障或超过 5 分钟的事件积压，"
        "目前没有必须立即处理的平台级风险。今天先从运行诊断卡片做一次例行抽查，再查看"
        "订单和店铺状态。"
    )


def _render_merchant_priority_follow_up(data: Mapping[str, Any], *, user_text: str = "") -> str:
    specialists = data.get("specialists")
    products: list[Mapping[str, Any]] = []
    order_counts: Mapping[str, Any] = {}
    low_stock = 0
    if isinstance(specialists, Mapping):
        for result in specialists.values():
            if not isinstance(result, Mapping) or not isinstance(result.get("data"), Mapping):
                continue
            safe_data = result["data"]
            values = safe_data.get("on_sale_products")
            if isinstance(values, list):
                products = [item for item in values if isinstance(item, Mapping)]
            counts = safe_data.get("order_status_counts")
            if isinstance(counts, Mapping):
                order_counts = counts
            low_stock = max(low_stock, int(safe_data.get("low_stock_sku_count", 0)))
    pending = sum(int(order_counts.get(key, 0)) for key in ("paid", "pending_shipment", "shipped"))
    zero_sales = sum(int(item.get("sales_count", 0)) <= 0 for item in products)
    priorities: list[str] = []
    if low_stock:
        priorities.append("inventory")
    if pending:
        priorities.append("orders")
    priorities.extend(
        domain for domain in ("catalog", "orders", "inventory") if domain not in priorities
    )
    focus = _priority_focus_index(user_text) or 1
    selected = priorities[min(focus - 1, len(priorities) - 1)]
    if selected == "orders":
        return (
            f"第{focus}项是履约跟进: 当前有 {pending} 笔订单仍在待履约或运输阶段，"
            "延迟处理会直接影响顾客体验。现在先打开“经营快照”卡片进入本店订单，"
            "依次核对待发货、物流长时间未更新和售后状态。本次没有修改订单。"
        )
    if selected == "catalog":
        return (
            f"第{focus}项是商品复盘: 当前 {len(products)} 件在售商品中有 {zero_sales} 件暂无销量。"
            "先从商品管理入口核对主图、标题、价格、款式库存和购买入口，先记录问题，"
            "不要在没有数据依据时批量降价。"
        )
    if selected == "inventory" and not low_stock:
        return (
            f"第{focus}项是库存巡检: 当前没有款式达到低库存或缺货阈值，因此它不是紧急风险。"
            "打开商品管理做例行复核即可，不需要立即补货或下架。"
        )
    if low_stock:
        return (
            f"第{focus}项最急，因为有 {low_stock} 个款式已经达到低库存或缺货阈值，继续售卖可能"
            "影响下单与履约。现在先打开“库存守卫”卡片进入商品管理，核对具体款式的可售库存"
            "和安全库存线。确认实际库存后再补货或调整上架状态。"
        )
    if pending:
        return (
            f"第一项最急，因为有 {pending} 笔订单仍在待履约或运输阶段，延迟处理会直接影响"
            "顾客体验。现在先打开“经营快照”卡片进入本店订单，按待发货、运输中顺序核对。"
        )
    zero_sales = sum(int(item.get("sales_count", 0)) <= 0 for item in products)
    if zero_sales:
        return (
            f"当前没有低库存或履约告警，第一项应先复盘 {zero_sales} 件暂无销量的在售商品。"
            "现在从商品管理入口抽查主图、标题、价格、款式库存和购买入口，先记录问题，不要"
            "直接批量降价。"
        )
    return (
        "当前没有低库存、缺货或待履约告警。今天先从经营首页复核订单与营业额，再保持每日"
        "库存巡检。目前没有证据支持立即改价或下架。"
    )


def _flatten_summary(value: Mapping[str, Any]) -> dict[str, str | int]:
    result: dict[str, str | int] = {}
    for key, item in value.items():
        if isinstance(item, (str, int)):
            result[str(key)] = item
        elif isinstance(item, dict):
            for nested_key, nested_value in item.items():
                if isinstance(nested_value, (str, int)):
                    result[f"{key}.{nested_key}"] = nested_value
    return result


async def _execute_tool(
    session: AsyncSession,
    context: TrustedOperationsContext,
    tool_code: str,
    *,
    query_text: str = "",
) -> ToolResult:
    async def handler(arguments: BaseModel, _scope: ToolScope) -> Mapping[str, Any]:
        query = arguments.query if isinstance(arguments, EmptyArguments) else ""
        return await _snapshot(session, context, tool_code, query_text=query)

    host = McpHost(
        [ToolAdapter(tool_code, EmptyArguments, handler)],
        database_kill_switch_checker(session),
    )
    return await host.execute(
        session,
        run_id=context.run.id,
        tool_code=tool_code,
        untrusted_arguments={"query": query_text[:500]},
        trusted_scope=ToolScope(
            user_no=context.user.user_no,
            conversation_no=context.conversation.conversation_no,
            store_no=context.store.store_no if context.store else None,
            context_no=None,
            context_version=None,
        ),
        allowed_tools=context.allowed_tools,
    )


async def _attach_merchant_policy_knowledge(
    mysql: AsyncSession,
    checkpoint_store: AgentCheckpointStore,
    context: TrustedOperationsContext,
    evidence: dict[str, object],
) -> None:
    """Attach ACL-filtered merchant policy evidence to one operations run.

    Store-published policies are queried from the business database. Platform merchant
    rules are retrieved from the published knowledge index. Keeping both sources in the
    same evidence envelope lets the Supervisor distinguish a store promise from a
    platform rule instead of turning either into an unsupported statement.
    """

    if context.store is None:
        return
    query = context.trigger.text_content or "平台商家规则"
    service = KnowledgeService(mysql, checkpoint_store.session)
    try:
        platform_result = await service.search_for_agent(
            query=query,
            scope_type="platform",
            scope_no="platform",
            limit=8,
            trace_id=context.run.trace_id,
        )
    except SQLAlchemyError:
        await checkpoint_store.session.rollback()
        evidence["rag"] = {
            "scope": "platform:platform",
            "returned_count": 0,
            "degraded": True,
            "error_code": "RAG_RETRIEVAL_UNAVAILABLE",
        }
        return
    sources: list[dict[str, object]] = []
    seen_documents: set[str] = set()
    # Parent-section expansion may return several sections from one document.
    # Rank by the retrieval score before deduplication so the section that best
    # matches the current question survives (for example "自动审核和发布"
    # instead of a generic section from the same merchant-rule document).
    for item in sorted(platform_result.items, key=lambda value: value.score, reverse=True):
        if item.document_id in seen_documents:
            continue
        seen_documents.add(item.document_id)
        sources.append(
            {
                "document_id": item.document_id,
                "title": item.title.removeprefix("[系统] "),
                "version": item.content_version,
                "excerpt": item.excerpt,
                "score": round(item.score, 6),
                "scope": "platform:platform",
            }
        )
    retrieved_source_count = len(sources)
    sources = _compact_merchant_policy_sources(query, sources)
    evidence["knowledge_sources"] = sources
    rag_trace: dict[str, object] = {
        "scope": "platform:platform",
        "returned_count": retrieved_source_count,
        "used_count": len(sources),
        "degraded": platform_result.degraded,
        "retrieval_mode": "keyword_only" if platform_result.degraded else "hybrid",
    }
    evidence["rag"] = rag_trace
    specialists = evidence.get("specialists")
    if isinstance(specialists, dict):
        policy_specialist = specialists.get("merchant_policy")
        if isinstance(policy_specialist, dict):
            policy_data = policy_specialist.get("data")
            if isinstance(policy_data, dict):
                policy_data["knowledge_sources"] = sources
                policy_data["rag"] = dict(rag_trace)


def _compact_merchant_policy_sources(
    query: str, sources: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Keep only policy evidence that answers the merchant's current topic."""

    compact_query = re.sub(r"\s+", "", query).casefold()
    topic_groups = (
        (
            ("自动审核", "商品审核", "禁售", "违禁", "上架规则"),
            ("自动审核", "商品审核", "禁售", "违禁", "上架", "需修改"),
        ),
        (
            ("物流", "快递", "包裹", "运单", "签收", "轨迹"),
            ("物流", "快递", "包裹", "运单", "签收", "轨迹"),
        ),
        (
            ("支付", "余额", "营业额", "结算", "退款到账"),
            ("支付", "余额", "营业额", "结算", "退款"),
        ),
        (
            ("售后", "退换", "质量问题", "退货"),
            ("售后", "退换", "质量问题", "退货", "退款"),
        ),
    )
    evidence_terms: tuple[str, ...] = ()
    for query_terms, candidate_terms in topic_groups:
        if any(term in compact_query for term in query_terms):
            evidence_terms = candidate_terms
            break
    if evidence_terms:
        matched = [
            source
            for source in sources
            if any(term in str(source.get("excerpt") or "") for term in evidence_terms)
        ]
        if matched:
            return matched[:3]
    return sources[:3]


async def _merchant_revenue_windows(session: AsyncSession, store_id: int) -> dict[str, object]:
    """Return one consistent set of merchant revenue windows.

    Both the dedicated revenue tool and a compound order/revenue query use this
    helper so a Supervisor does not have to choose between time-window revenue
    facts and the requested order list.
    """

    now = utc_now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    thirty_day_start = today_start - timedelta(days=29)

    async def completed_net_since(start: datetime, end: datetime | None = None) -> tuple[int, int]:
        conditions = [
            Order.store_id == store_id,
            Order.order_status == "completed",
            Order.completed_at.is_not(None),
            Order.completed_at >= start,
        ]
        if end is not None:
            conditions.append(Order.completed_at < end)
        row = (
            await session.execute(
                select(
                    func.coalesce(func.sum(Order.paid_amount - Order.refunded_amount), 0),
                    func.count(Order.id),
                ).where(*conditions)
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0)

    today_amount, today_count = await completed_net_since(today_start)
    yesterday_amount, yesterday_count = await completed_net_since(yesterday_start, today_start)
    thirty_day_amount, thirty_day_count = await completed_net_since(thirty_day_start)
    return {
        "metrics_as_of": now.isoformat(),
        "today_revenue": {
            "minor_units": today_amount,
            "currency": "CNY",
            "display": _money_display(today_amount, "CNY"),
            "completed_orders": today_count,
        },
        "yesterday_revenue": {
            "minor_units": yesterday_amount,
            "currency": "CNY",
            "display": _money_display(yesterday_amount, "CNY"),
            "completed_orders": yesterday_count,
        },
        "thirty_day_revenue": {
            "minor_units": thirty_day_amount,
            "currency": "CNY",
            "display": _money_display(thirty_day_amount, "CNY"),
            "completed_orders": thirty_day_count,
        },
    }


async def _snapshot(
    session: AsyncSession,
    context: TrustedOperationsContext,
    tool_code: str,
    *,
    query_text: str = "",
) -> dict[str, object]:
    if context.audience == "merchant":
        assert context.store is not None
        store_id = context.store.id
        product_counts = await _counts(
            session,
            Product.product_status,
            Product.store_id == store_id,
            Product.deleted_at.is_(None),
        )
        order_counts = await _counts(session, Order.order_status, Order.store_id == store_id)
        revenue = int(
            await session.scalar(
                select(func.coalesce(func.sum(Order.paid_amount - Order.refunded_amount), 0)).where(
                    Order.store_id == store_id, Order.order_status == "completed"
                )
            )
            or 0
        )
        unsettled_paid_amount = int(
            await session.scalar(
                select(func.coalesce(func.sum(Order.paid_amount - Order.refunded_amount), 0)).where(
                    Order.store_id == store_id,
                    Order.payment_status == "paid",
                    Order.order_status.not_in(("completed", "cancelled", "closed")),
                )
            )
            or 0
        )
        if tool_code == "store_ops.profile.get":
            published_policies = list(
                (
                    await session.execute(
                        select(
                            StoreServicePolicy.policy_type,
                            StoreServicePolicy.title,
                            StoreServicePolicy.policy_version,
                        )
                        .where(
                            StoreServicePolicy.store_id == store_id,
                            StoreServicePolicy.policy_status == "published",
                        )
                        .order_by(
                            StoreServicePolicy.policy_type, StoreServicePolicy.policy_version.desc()
                        )
                        .limit(20)
                    )
                ).all()
            )
            return {
                "store_id": context.store.store_no,
                "store_profile": {
                    "store_name": context.store.store_name,
                    "description": context.store.description or "尚未填写",
                    "status": context.store.store_status,
                    "logo_configured": bool(context.store.logo_object_key),
                    "rating_score": float(context.store.rating_score or 0),
                    "rating_count": int(context.store.rating_count or 0),
                    "follower_count": int(context.store.follower_count or 0),
                    "sales_count": int(context.store.sales_count or 0),
                    "version": int(context.store.version),
                },
                "published_policies": [
                    {
                        "policy_type": policy_type,
                        "title": title,
                        "version": policy_version,
                    }
                    for policy_type, title, policy_version in published_policies
                ],
            }
        if tool_code == "store_ops.revenue_metrics":
            revenue_windows = await _merchant_revenue_windows(session, store_id)
            return {
                "store_id": context.store.store_no,
                **revenue_windows,
                "revenue_basis": "仅统计已完成订单的实付金额减已退款金额",
                "completed_order_revenue": {
                    "minor_units": revenue,
                    "currency": "CNY",
                    "display": _money_display(revenue, "CNY"),
                    "meaning": "累计已确认营业额",
                },
                "unsettled_paid_amount": {
                    "minor_units": unsettled_paid_amount,
                    "currency": "CNY",
                    "display": _money_display(unsettled_paid_amount, "CNY"),
                    "meaning": "已支付但尚未完成，不计入营业额",
                },
            }
        if tool_code == "store_ops.catalog.get_product":
            compact_query = re.sub(r"\s+", "", query_text).casefold()
            store_products = list(
                (
                    await session.scalars(
                        select(Product)
                        .where(
                            Product.store_id == store_id,
                            Product.deleted_at.is_(None),
                        )
                        .order_by(Product.updated_at.desc(), Product.id.desc())
                        .limit(100)
                    )
                ).all()
            )
            matched_products = [
                product
                for product in store_products
                if _query_mentions_catalog_product(
                    compact_query, product.product_name, product.product_no
                )
            ]
            selected_product = (
                matched_products[0]
                if len(matched_products) == 1
                else store_products[0]
                if len(store_products) == 1
                else None
            )
            candidate_products = [
                {
                    "product_id": product.product_no,
                    "name": product.product_name,
                    "status": product.product_status,
                    "version": int(product.version),
                }
                for product in (matched_products or store_products[:12])
            ]
            if selected_product is None:
                return {
                    "store_id": context.store.store_no,
                    "query_mode": "product_selection",
                    "product_status_counts": product_counts,
                    "candidate_products": candidate_products,
                    "selection_required": bool(candidate_products),
                }

            sku_rows = list(
                (
                    await session.execute(
                        select(ProductSku, Inventory)
                        .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
                        .where(ProductSku.product_id == selected_product.id)
                        .order_by(ProductSku.id)
                    )
                ).all()
            )
            image_rows = list(
                (
                    await session.execute(
                        select(ProductImage, FileObject)
                        .join(FileObject, FileObject.id == ProductImage.file_id)
                        .where(ProductImage.product_id == selected_product.id)
                        .order_by(ProductImage.sku_id, ProductImage.sort_order, ProductImage.id)
                    )
                ).all()
            )
            images_by_sku: dict[int, list[dict[str, object]]] = {}
            for image, file_object in image_rows:
                images_by_sku.setdefault(image.sku_id, []).append(
                    {
                        "file_id": file_object.file_no,
                        "image_id": image.id,
                        "image_type": image.image_type,
                        "sort_order": image.sort_order,
                        "status": image.image_status,
                        "scan_status": file_object.scan_status,
                        "ocr_status": file_object.ocr_status,
                        "image_url": (
                            f"/api/v1/files/{file_object.file_no}?variant=thumbnail"
                            if file_object.file_status == "active"
                            and file_object.scan_status == "safe"
                            else None
                        ),
                    }
                )

            attributes = list(
                (
                    await session.scalars(
                        select(ProductAttribute)
                        .where(ProductAttribute.product_id == selected_product.id)
                        .order_by(ProductAttribute.sort_order, ProductAttribute.id)
                    )
                ).all()
            )
            content = (
                await session.scalar(
                    select(ProductContentVersion).where(
                        ProductContentVersion.id
                        == selected_product.current_detail_content_version_id,
                        ProductContentVersion.product_id == selected_product.id,
                    )
                )
                if selected_product.current_detail_content_version_id is not None
                else None
            )
            content_files = (
                list(
                    (
                        await session.scalars(
                            select(FileObject)
                            .join(
                                ProductContentVersionFile,
                                ProductContentVersionFile.file_id == FileObject.id,
                            )
                            .where(ProductContentVersionFile.content_version_id == content.id)
                            .order_by(ProductContentVersionFile.id)
                        )
                    ).all()
                )
                if content is not None
                else []
            )
            faq_rows = list(
                (
                    await session.execute(
                        select(ProductFaq, ProductFaqVersion)
                        .outerjoin(
                            ProductFaqVersion,
                            ProductFaqVersion.id == ProductFaq.current_content_version_id,
                        )
                        .where(ProductFaq.product_id == selected_product.id)
                        .order_by(ProductFaq.sort_order, ProductFaq.id)
                    )
                ).all()
            )
            fulfillment = await session.scalar(
                select(ProductFulfillmentProfile).where(
                    ProductFulfillmentProfile.product_id == selected_product.id
                )
            )
            active_sku_ids = {sku.id for sku, _inventory in sku_rows if sku.sku_status == "active"}
            image_sku_ids = {
                image.sku_id
                for image, file_object in image_rows
                if image.sku_id is not None
                and image.image_status == "active"
                and file_object.file_status == "active"
                and file_object.scan_status == "safe"
            }
            submission_checks = {
                "basic": bool(selected_product.product_name and selected_product.category_id),
                "sku": bool(active_sku_ids),
                "sku_images": bool(active_sku_ids) and active_sku_ids.issubset(image_sku_ids),
                "fulfillment": fulfillment is not None,
                "detail_content": bool(content and content.security_scan_status == "passed"),
            }
            required_submission_checks = (
                "basic",
                "sku",
                "sku_images",
                "fulfillment",
                "detail_content",
            )
            missing_submission_items = [
                key for key in required_submission_checks if not submission_checks[key]
            ]
            product_detail = {
                "product_id": selected_product.product_no,
                "name": selected_product.product_name,
                "status": selected_product.product_status,
                "subtitle": selected_product.subtitle,
                "description": selected_product.description,
                "sales_count": int(selected_product.sales_count),
                "review_count": int(selected_product.review_count),
                "rating_score": float(selected_product.rating_score or 0),
                "version": int(selected_product.version),
                "published_at": (
                    selected_product.published_at.isoformat()
                    if selected_product.published_at
                    else None
                ),
                "skus": [
                    {
                        "sku_id": sku.sku_no,
                        "name": sku.sku_name,
                        "status": sku.sku_status,
                        "spec_values": sku.spec_values,
                        "price": {
                            "minor_units": sku.sale_price_amount,
                            "currency": sku.currency,
                            "display": _money_display(sku.sale_price_amount, sku.currency),
                        },
                        "inventory": {
                            "on_hand": inventory.on_hand_quantity if inventory else 0,
                            "reserved": inventory.reserved_quantity if inventory else 0,
                            "available": (
                                inventory.on_hand_quantity - inventory.reserved_quantity
                                if inventory
                                else 0
                            ),
                            "safety_stock": inventory.safety_stock_quantity if inventory else 0,
                            "version": int(inventory.version) if inventory else None,
                        },
                        "images": images_by_sku.get(sku.id, []),
                        "version": int(sku.version),
                    }
                    for sku, inventory in sku_rows
                ],
                "attributes": [
                    {
                        "code": item.attribute_code,
                        "name": item.attribute_name,
                        "value": item.value_text,
                        "unit": item.unit,
                    }
                    for item in attributes
                ],
                "content": (
                    {
                        "content_version_id": content.content_version_no,
                        "content_version": content.content_version,
                        "status": content.version_status,
                        "scan_status": content.security_scan_status,
                        "format": content.public_content_format,
                        "blocks": content.safe_blocks,
                        "safe_text": content.safe_text[:6000],
                    }
                    if content is not None
                    else None
                ),
                "ocr_results": [
                    {
                        "file_id": file.file_no,
                        "status": file.ocr_status,
                        "text": (file.ocr_text or "")[:1500] or None,
                        "engine": file.ocr_engine,
                        "processed_at": (
                            file.ocr_processed_at.isoformat() if file.ocr_processed_at else None
                        ),
                    }
                    for file in content_files
                ],
                "faqs": [
                    {
                        "faq_id": faq.faq_no,
                        "question": faq.question,
                        "answer": faq_version.safe_text if faq_version is not None else None,
                        "status": faq.faq_status,
                        "version": (
                            faq_version.content_version if faq_version is not None else None
                        ),
                    }
                    for faq, faq_version in faq_rows
                ],
                "fulfillment": (
                    {
                        "origin_region_code": fulfillment.origin_region_code,
                        "dispatch_min_hours": fulfillment.dispatch_min_hours,
                        "dispatch_max_hours": fulfillment.dispatch_max_hours,
                        "purchase_notice": fulfillment.purchase_notice,
                        "version": fulfillment.profile_version,
                    }
                    if fulfillment is not None
                    else None
                ),
                "submission_readiness": {
                    "ready": not missing_submission_items,
                    "checks": submission_checks,
                    "missing_items": missing_submission_items,
                    "note": "这是提交前完整性预检，最终仍以确认时的安全扫描和自动审核结果为准。",
                },
            }
            return {
                "store_id": context.store.store_no,
                "query_mode": "product_detail",
                "product_status_counts": product_counts,
                "product_detail": product_detail,
                "candidate_products": candidate_products,
            }
        if tool_code == "store_ops.catalog_summary":
            product_rows = (
                await session.execute(
                    select(Product, ProductSku, Inventory)
                    .join(ProductSku, ProductSku.product_id == Product.id)
                    .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
                    .where(
                        Product.store_id == store_id,
                        Product.deleted_at.is_(None),
                        Product.product_status == "on_sale",
                        ProductSku.sku_status == "active",
                    )
                    .order_by(Product.sales_count.desc(), Product.id, ProductSku.id)
                    .limit(50)
                )
            ).all()
            products: dict[int, dict[str, object]] = {}
            for product, sku, inventory in product_rows:
                item = products.setdefault(
                    product.id,
                    {
                        "product_id": product.product_no,
                        "name": product.product_name,
                        "status": product.product_status,
                        "sales_count": product.sales_count,
                        "skus": [],
                    },
                )
                sku_items = item["skus"]
                assert isinstance(sku_items, list)
                sku_items.append(
                    {
                        "sku_id": sku.sku_no,
                        "name": sku.sku_name,
                        "price": {
                            "minor_units": sku.sale_price_amount,
                            "currency": sku.currency,
                            "display": _money_display(sku.sale_price_amount, sku.currency),
                        },
                        "inventory": {
                            "on_hand": inventory.on_hand_quantity if inventory else 0,
                            "reserved": inventory.reserved_quantity if inventory else 0,
                            "available": (
                                inventory.on_hand_quantity - inventory.reserved_quantity
                                if inventory
                                else 0
                            ),
                        },
                    }
                )
            return {
                "store_id": context.store.store_no,
                "product_status_counts": product_counts,
                "on_sale_products": list(products.values()),
                "truncated": len(product_rows) >= 50,
            }
        if tool_code in {
            "store_ops.order_summary",
            "store_ops.orders.list",
            "store_ops.orders.get",
        }:
            compact_query = re.sub(r"\s+", "", query_text).casefold()
            merchant_order_statuses: set[str] = set()
            status_markers = {
                "pending_payment": ("待付款", "未付款"),
                "paid": ("已付款",),
                "pending_shipment": ("待发货",),
                "shipped": ("运输中", "在途", "物流中"),
                "completed": ("已完成", "完成订单", "待评价"),
            }
            for status, markers in status_markers.items():
                if any(marker in compact_query for marker in markers):
                    merchant_order_statuses.add(status)

            now = utc_now()
            created_after: datetime | None = None
            created_before: datetime | None = None
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if "今天" in compact_query or "今日" in compact_query:
                created_after = today_start
            elif "昨天" in compact_query or "昨日" in compact_query:
                created_after = today_start - timedelta(days=1)
                created_before = today_start
            elif any(marker in compact_query for marker in ("近30日", "近三十日", "最近30天")):
                created_after = today_start - timedelta(days=29)
            elif any(marker in compact_query for marker in ("近7日", "最近7天", "本周")):
                created_after = today_start - timedelta(days=6)

            explicit_order_no = next(
                iter(re.findall(r"ord_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
            )
            merchant_order_customer_rows = list(
                (
                    await session.execute(
                        select(User.id, User.username)
                        .join(Order, Order.user_id == User.id)
                        .where(Order.store_id == store_id)
                        .distinct()
                    )
                ).all()
            )
            merchant_order_customer = next(
                (
                    (customer_id, username)
                    for customer_id, username in merchant_order_customer_rows
                    if len(username) >= 2 and username.casefold() in compact_query
                ),
                None,
            )

            order_conditions = [
                Order.store_id == store_id,
                Order.order_status.not_in(("cancelled", "closed")),
            ]
            if merchant_order_statuses:
                order_conditions.append(Order.order_status.in_(merchant_order_statuses))
            if created_after is not None:
                order_conditions.append(Order.created_at >= created_after)
            if created_before is not None:
                order_conditions.append(Order.created_at < created_before)
            if explicit_order_no is not None:
                order_conditions.append(Order.order_no == explicit_order_no)
            if merchant_order_customer is not None:
                order_conditions.append(Order.user_id == merchant_order_customer[0])

            result_limit = 1 if tool_code == "store_ops.orders.get" else 12
            recent_order_rows = (
                await session.execute(
                    select(Order, User)
                    .join(User, User.id == Order.user_id)
                    .where(*order_conditions)
                    .order_by(Order.created_at.desc(), Order.id.desc())
                    .limit(result_limit)
                )
            ).all()
            order_ids = [order.id for order, _customer in recent_order_rows]
            merchant_order_item_rows = (
                list(
                    (
                        await session.scalars(
                            select(OrderItem)
                            .where(OrderItem.order_id.in_(order_ids))
                            .order_by(OrderItem.order_id, OrderItem.id)
                        )
                    ).all()
                )
                if order_ids
                else []
            )
            items_by_order: dict[int, list[OrderItem]] = {}
            for order_item in merchant_order_item_rows:
                items_by_order.setdefault(order_item.order_id, []).append(order_item)

            merchant_order_image_keys = {
                order_item.image_object_key
                for order_item in merchant_order_item_rows
                if order_item.image_object_key is not None
            }
            if context.store.logo_object_key:
                merchant_order_image_keys.add(context.store.logo_object_key)
            merchant_order_image_files = (
                {
                    file.object_key: file
                    for file in (
                        await session.scalars(
                            select(FileObject).where(
                                FileObject.object_key.in_(merchant_order_image_keys),
                                FileObject.file_status == "active",
                                FileObject.scan_status == "safe",
                            )
                        )
                    ).all()
                }
                if merchant_order_image_keys
                else {}
            )

            detail_address: OrderAddress | None = None
            if tool_code == "store_ops.orders.get" and recent_order_rows:
                detail_address = await session.scalar(
                    select(OrderAddress).where(OrderAddress.order_id == recent_order_rows[0][0].id)
                )
            security = SecurityService(get_settings()) if detail_address is not None else None
            recent_orders: list[dict[str, object]] = []
            for order, customer in recent_order_rows:
                order_items = items_by_order.get(order.id, [])
                first_item = order_items[0] if order_items else None
                address_payload: dict[str, object] | None = None
                if detail_address is not None and detail_address.order_id == order.id and security:
                    address_payload = {
                        "recipient_name": security.decrypt(
                            "address-recipient", detail_address.recipient_name_ciphertext
                        ),
                        "phone_masked": f"*** **** {detail_address.phone_last4}",
                        "country_code": detail_address.country_code,
                        "province_code": detail_address.province_code,
                        "city_code": detail_address.city_code,
                        "district_code": detail_address.district_code,
                        "address": security.decrypt(
                            "address-detail", detail_address.address_ciphertext
                        ),
                    }
                recent_orders.append(
                    {
                        "order_id": order.order_no,
                        "display_order_id": f"{order.order_no[:8]}…{order.order_no[-4:]}",
                        "customer_name": customer.username,
                        "product_name": first_item.product_name if first_item else "本店订单",
                        "sku_name": first_item.sku_name if first_item else None,
                        "quantity": first_item.quantity if first_item else None,
                        "items": [
                            {
                                "product_id": item.product_no,
                                "sku_id": item.sku_no,
                                "product_name": item.product_name,
                                "sku_name": item.sku_name,
                                "quantity": item.quantity,
                                "image_url": (
                                    f"/api/v1/files/{merchant_order_image_files[item.image_object_key].file_no}"
                                    "?variant=thumbnail"
                                    if item.image_object_key in merchant_order_image_files
                                    else None
                                ),
                            }
                            for item in order_items
                        ],
                        "item_count": len(order_items),
                        "total_quantity": sum(item.quantity for item in order_items),
                        "status": order.order_status,
                        "payment_status": order.payment_status,
                        "fulfillment_status": order.fulfillment_status,
                        "after_sale_status": order.after_sale_status,
                        "has_pending_review": any(
                            item.review_status == "pending" for item in order_items
                        ),
                        "amount": {
                            "minor_units": order.paid_amount,
                            "currency": order.currency,
                            "display": _money_display(order.paid_amount, order.currency),
                        },
                        "payable_amount": {
                            "minor_units": str(order.payable_amount),
                            "currency": order.currency,
                        },
                        "refunded_amount": {
                            "minor_units": order.refunded_amount,
                            "currency": order.currency,
                            "display": _money_display(order.refunded_amount, order.currency),
                        },
                        "store": {
                            "store_id": context.store.store_no,
                            "store_name": context.store.store_name,
                            "logo_url": (
                                f"/api/v1/files/{merchant_order_image_files[context.store.logo_object_key].file_no}"
                                if context.store.logo_object_key in merchant_order_image_files
                                else None
                            ),
                        },
                        "address": address_payload,
                        "buyer_remark": order.buyer_remark,
                        "version": int(order.version),
                        "created_at": order.created_at.isoformat(),
                    }
                )
            revenue_windows = await _merchant_revenue_windows(session, store_id)
            return {
                "store_id": context.store.store_no,
                **revenue_windows,
                "revenue_basis": "仅统计已完成订单的实付金额减已退款金额",
                "query_mode": "detail" if tool_code == "store_ops.orders.get" else "list",
                "applied_filters": {
                    "statuses": sorted(merchant_order_statuses),
                    "customer_name": (
                        merchant_order_customer[1] if merchant_order_customer else None
                    ),
                    "order_id": explicit_order_no,
                    "created_after": created_after.isoformat() if created_after else None,
                    "created_before": created_before.isoformat() if created_before else None,
                },
                "order_status_counts": order_counts,
                "recent_orders": recent_orders,
                "completed_order_revenue": {
                    "minor_units": revenue,
                    "currency": "CNY",
                    "display": _money_display(revenue, "CNY"),
                    "meaning": "仅统计已完成订单，用户确认收货后才计入",
                },
                "unsettled_paid_amount": {
                    "minor_units": unsettled_paid_amount,
                    "currency": "CNY",
                    "display": _money_display(unsettled_paid_amount, "CNY"),
                    "meaning": "已支付但尚未完成的订单金额，不是已确认营业额",
                },
            }
        if tool_code == "store_ops.inventory.get_skus":
            zero_stock_only = any(
                marker in re.sub(r"\s+", "", query_text).casefold()
                for marker in (
                    "可售为0",
                    "可售0",
                    "库存为0",
                    "库存0",
                    "零库存",
                    "没有库存",
                )
            )
            inventory_rows = list(
                (
                    await session.execute(
                        select(Product, ProductSku, Inventory)
                        .join(ProductSku, ProductSku.product_id == Product.id)
                        .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
                        .where(
                            Product.store_id == store_id,
                            Product.deleted_at.is_(None),
                            ProductSku.sku_status == "active",
                        )
                        .order_by(Product.sales_count.desc(), Product.id, ProductSku.id)
                        .limit(50)
                    )
                ).all()
            )
            inventory_ids = [
                inventory.id
                for _product, _sku, inventory in inventory_rows
                if inventory is not None
            ]
            movement_rows = (
                list(
                    (
                        await session.scalars(
                            select(InventoryLog)
                            .where(InventoryLog.inventory_id.in_(inventory_ids))
                            .order_by(InventoryLog.created_at.desc(), InventoryLog.id.desc())
                            .limit(120)
                        )
                    ).all()
                )
                if inventory_ids
                else []
            )
            movements_by_inventory: dict[int, list[dict[str, object]]] = {}
            for movement in movement_rows:
                bucket = movements_by_inventory.setdefault(movement.inventory_id, [])
                if len(bucket) >= 3:
                    continue
                bucket.append(
                    {
                        "operation": movement.operation_type,
                        "on_hand_delta": movement.on_hand_delta,
                        "reserved_delta": movement.reserved_delta,
                        "on_hand_after": movement.on_hand_after,
                        "reserved_after": movement.reserved_after,
                        "reference_type": movement.reference_type,
                        "reason": movement.reason,
                        "occurred_at": movement.created_at.isoformat(),
                    }
                )
            return {
                "store_id": context.store.store_no,
                "inventory_skus": [
                    {
                        "product_id": product.product_no,
                        "product_name": product.product_name,
                        "product_status": product.product_status,
                        "sku_id": sku.sku_no,
                        "sku_name": sku.sku_name,
                        "on_hand_quantity": inventory.on_hand_quantity if inventory else 0,
                        "reserved_quantity": inventory.reserved_quantity if inventory else 0,
                        "available_quantity": (
                            inventory.on_hand_quantity - inventory.reserved_quantity
                            if inventory
                            else 0
                        ),
                        "safety_stock_quantity": (
                            inventory.safety_stock_quantity if inventory else 0
                        ),
                        "version": int(inventory.version) if inventory else None,
                        "recent_movements": (
                            movements_by_inventory.get(inventory.id, [])
                            if inventory is not None
                            else []
                        ),
                    }
                    for product, sku, inventory in inventory_rows
                    if not zero_stock_only
                    or inventory is None
                    or inventory.on_hand_quantity - inventory.reserved_quantity == 0
                ],
                "applied_filters": {"zero_stock_only": zero_stock_only},
                "truncated": len(inventory_rows) >= 50,
            }
        if tool_code == "store_ops.inventory_risks":
            risk_rows = (
                await session.execute(
                    select(Product, ProductSku, Inventory)
                    .join(ProductSku, ProductSku.product_id == Product.id)
                    .join(Inventory, Inventory.sku_id == ProductSku.id)
                    .where(
                        Product.store_id == store_id,
                        Product.deleted_at.is_(None),
                        Product.product_status == "on_sale",
                        ProductSku.sku_status == "active",
                        Inventory.inventory_status == "active",
                        Inventory.on_hand_quantity - Inventory.reserved_quantity
                        <= Inventory.safety_stock_quantity,
                    )
                    .order_by(
                        (Inventory.on_hand_quantity - Inventory.reserved_quantity).asc(),
                        Product.id,
                        ProductSku.id,
                    )
                    .limit(20)
                )
            ).all()
            return {
                "store_id": context.store.store_no,
                "low_stock_sku_count": len(risk_rows),
                "low_stock_skus": [
                    {
                        "product_id": product.product_no,
                        "product_name": product.product_name,
                        "sku_id": sku.sku_no,
                        "sku_name": sku.sku_name,
                        "available_quantity": inventory.on_hand_quantity
                        - inventory.reserved_quantity,
                        "safety_stock_quantity": inventory.safety_stock_quantity,
                    }
                    for product, sku, inventory in risk_rows
                ],
                "truncated": len(risk_rows) >= 20,
            }
        if tool_code in {"store_ops.review_summary", "store_ops.reviews.list"}:
            review_counts = await _counts(
                session,
                Review.review_status,
                Review.store_id == store_id,
            )
            published_count = int(
                await session.scalar(
                    select(func.count(Review.id)).where(
                        Review.store_id == store_id,
                        Review.review_status == "published",
                    )
                )
                or 0
            )
            replied_count = int(
                await session.scalar(
                    select(func.count(ReviewReply.id)).where(
                        ReviewReply.store_id == store_id,
                        ReviewReply.reply_status == "published",
                    )
                )
                or 0
            )
            average_rating = await session.scalar(
                select(func.avg(Review.rating)).where(
                    Review.store_id == store_id,
                    Review.review_status == "published",
                )
            )
            reply_bucket = case((ReviewReply.id.is_(None), 0), else_=1)
            review_sort_time = func.coalesce(Review.published_at, Review.created_at)
            review_statement = (
                select(Review, Product, User, ReviewReply)
                .join(Product, Product.id == Review.product_id)
                .join(User, User.id == Review.user_id)
                .outerjoin(
                    ReviewReply,
                    (ReviewReply.review_id == Review.id)
                    & (ReviewReply.reply_status == "published"),
                )
                .where(
                    Review.store_id == store_id,
                    Review.review_status == "published",
                )
            )
            if tool_code == "store_ops.review_summary":
                review_statement = review_statement.where(ReviewReply.id.is_(None))
            page_size = MERCHANT_REVIEW_PAGE_SIZE if tool_code == "store_ops.reviews.list" else 6
            pagination_requested = (
                tool_code == "store_ops.reviews.list" and _is_operations_page_follow_up(query_text)
            )
            previous_continuation = (
                await _latest_operations_continuation(session, context, tool_code)
                if pagination_requested
                else None
            )
            cursor_token = (
                str(previous_continuation.get("next_cursor"))
                if isinstance(previous_continuation, Mapping)
                and previous_continuation.get("next_cursor")
                else None
            )
            if pagination_requested and previous_continuation is not None and not cursor_token:
                review_rows: list[Any] = []
                has_more = False
            else:
                if cursor_token:
                    position = CursorCodec(
                        get_settings().security_hmac_secret.get_secret_value()
                    ).decode(
                        cursor_token,
                        filter_key=_operations_cursor_filter_key(context, tool_code),
                    )
                    try:
                        assert position is not None and len(position.values) == 3
                        cursor_bucket = int(position.values[0])
                        cursor_time = datetime.fromisoformat(position.values[1])
                        cursor_id = int(position.values[2])
                    except (AssertionError, TypeError, ValueError) as exc:
                        raise ApplicationError(
                            status=400,
                            code="PAGINATION_CURSOR_INVALID",
                            title="Invalid pagination cursor",
                            detail="评价分页位置无效，请重新查询评价列表。",
                        ) from exc
                    review_statement = review_statement.where(
                        or_(
                            reply_bucket > cursor_bucket,
                            and_(
                                reply_bucket == cursor_bucket,
                                or_(
                                    review_sort_time < cursor_time,
                                    and_(
                                        review_sort_time == cursor_time,
                                        Review.id < cursor_id,
                                    ),
                                ),
                            ),
                        )
                    )
                fetched_review_rows = list(
                    (
                        await session.execute(
                            review_statement.order_by(
                                reply_bucket.asc(),
                                review_sort_time.desc(),
                                Review.id.desc(),
                            ).limit(page_size + (1 if tool_code == "store_ops.reviews.list" else 0))
                        )
                    ).all()
                )
                has_more = (
                    tool_code == "store_ops.reviews.list" and len(fetched_review_rows) > page_size
                )
                review_rows = fetched_review_rows[:page_size]
            next_cursor: str | None = None
            if tool_code == "store_ops.reviews.list" and has_more and review_rows:
                last_review, _last_product, _last_customer, last_reply = review_rows[-1]
                last_sort_time = last_review.published_at or last_review.created_at
                next_cursor = CursorCodec(
                    get_settings().security_hmac_secret.get_secret_value()
                ).encode(
                    filter_key=_operations_cursor_filter_key(context, tool_code),
                    values=(
                        "0" if last_reply is None else "1",
                        last_sort_time.isoformat(),
                        str(last_review.id),
                    ),
                )
            review_result: dict[str, object] = {
                "store_id": context.store.store_no,
                "review_status_counts": review_counts,
                "published_review_count": published_count,
                "replied_review_count": replied_count,
                "pending_reply_count": max(0, published_count - replied_count),
                "average_rating": round(float(average_rating or 0), 2),
                "pending_reviews": [
                    {
                        "review_id": review.review_no,
                        "product_id": product.product_no,
                        "product_name": product.product_name,
                        "customer_name": customer.username,
                        "rating": review.rating,
                        "content": review.content or "顾客未填写文字评价",
                        "published_at": (
                            review.published_at.isoformat() if review.published_at else None
                        ),
                    }
                    for review, product, customer, reply in review_rows
                    if reply is None
                ],
                "recent_reviews": [
                    {
                        "review_id": review.review_no,
                        "product_id": product.product_no,
                        "product_name": product.product_name,
                        "customer_name": customer.username,
                        "rating": review.rating,
                        "content": review.content or "顾客未填写文字评价",
                        "published_at": (
                            review.published_at.isoformat() if review.published_at else None
                        ),
                        "has_reply": reply is not None,
                        "reply_content": reply.content if reply else None,
                    }
                    for review, product, customer, reply in review_rows
                ],
                "query_mode": (
                    "history" if tool_code == "store_ops.reviews.list" else "pending_summary"
                ),
            }
            if tool_code == "store_ops.reviews.list":
                review_result["pagination"] = {
                    "tool_code": tool_code,
                    "page_size": page_size,
                    "returned_count": len(review_rows),
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                    "continued": pagination_requested,
                }
            return review_result
        if tool_code in {"store_ops.service_summary", "store_ops.conversations.list"}:
            conversation_counts = await _counts(
                session,
                Conversation.conversation_status,
                Conversation.store_id == store_id,
                Conversation.deleted_at.is_(None),
            )
            ticket_counts = await _counts(
                session,
                HumanServiceTicket.ticket_status,
                HumanServiceTicket.store_id == store_id,
            )
            active_ticket_rows = (
                await session.execute(
                    select(HumanServiceTicket, User)
                    .join(User, User.id == HumanServiceTicket.user_id)
                    .where(
                        HumanServiceTicket.store_id == store_id,
                        HumanServiceTicket.ticket_status.in_(
                            ("queued", "assigned", "active", "waiting_user")
                        ),
                    )
                    .order_by(
                        HumanServiceTicket.priority.desc(),
                        HumanServiceTicket.created_at,
                    )
                    .limit(8)
                )
            ).all()
            conversation_sort_time = func.coalesce(
                Conversation.last_message_at,
                Conversation.created_at,
            )
            conversation_statement = (
                select(Conversation, User, HumanServiceTicket)
                .join(User, User.id == Conversation.user_id)
                .outerjoin(
                    HumanServiceTicket,
                    HumanServiceTicket.id == Conversation.human_ticket_id,
                )
                .where(
                    Conversation.store_id == store_id,
                    Conversation.conversation_type == "store",
                    Conversation.deleted_at.is_(None),
                    User.id != context.user.id,
                )
            )
            conversation_page_size = (
                MERCHANT_CONVERSATION_PAGE_SIZE
                if tool_code == "store_ops.conversations.list"
                else 20
            )
            conversation_pagination_requested = (
                tool_code == "store_ops.conversations.list"
                and _is_operations_page_follow_up(query_text)
            )
            previous_continuation = (
                await _latest_operations_continuation(session, context, tool_code)
                if conversation_pagination_requested
                else None
            )
            cursor_token = (
                str(previous_continuation.get("next_cursor"))
                if isinstance(previous_continuation, Mapping)
                and previous_continuation.get("next_cursor")
                else None
            )
            if (
                conversation_pagination_requested
                and previous_continuation is not None
                and not cursor_token
            ):
                conversation_rows: list[Any] = []
                conversation_has_more = False
            else:
                if cursor_token:
                    position = CursorCodec(
                        get_settings().security_hmac_secret.get_secret_value()
                    ).decode(
                        cursor_token,
                        filter_key=_operations_cursor_filter_key(context, tool_code),
                    )
                    try:
                        assert position is not None and len(position.values) == 2
                        cursor_time = datetime.fromisoformat(position.values[0])
                        cursor_id = int(position.values[1])
                    except (AssertionError, TypeError, ValueError) as exc:
                        raise ApplicationError(
                            status=400,
                            code="PAGINATION_CURSOR_INVALID",
                            title="Invalid pagination cursor",
                            detail="顾客会话分页位置无效，请重新查询会话列表。",
                        ) from exc
                    conversation_statement = conversation_statement.where(
                        or_(
                            conversation_sort_time < cursor_time,
                            and_(
                                conversation_sort_time == cursor_time,
                                Conversation.id < cursor_id,
                            ),
                        )
                    )
                fetched_conversation_rows = list(
                    (
                        await session.execute(
                            conversation_statement.order_by(
                                conversation_sort_time.desc(),
                                Conversation.id.desc(),
                            ).limit(
                                conversation_page_size
                                + (1 if tool_code == "store_ops.conversations.list" else 0)
                            )
                        )
                    ).all()
                )
                conversation_has_more = (
                    tool_code == "store_ops.conversations.list"
                    and len(fetched_conversation_rows) > conversation_page_size
                )
                conversation_rows = fetched_conversation_rows[:conversation_page_size]
            conversation_next_cursor: str | None = None
            if (
                tool_code == "store_ops.conversations.list"
                and conversation_has_more
                and conversation_rows
            ):
                last_conversation, _last_customer, _last_ticket = conversation_rows[-1]
                last_sort_time = last_conversation.last_message_at or last_conversation.created_at
                conversation_next_cursor = CursorCodec(
                    get_settings().security_hmac_secret.get_secret_value()
                ).encode(
                    filter_key=_operations_cursor_filter_key(context, tool_code),
                    values=(last_sort_time.isoformat(), str(last_conversation.id)),
                )
            unread_counts = await MessagingRepository(session).operator_unread_counts(
                {conversation.id for conversation, _customer, _ticket in conversation_rows},
                context.user.id,
            )
            conversations: list[dict[str, object]] = []
            selected_conversation: dict[str, object] | None = None
            for conversation, customer, ticket in conversation_rows:
                last_message = (
                    await session.get(Message, conversation.last_message_id)
                    if conversation.last_message_id
                    else None
                )
                preview = "暂无消息"
                if last_message is not None:
                    if last_message.text_content:
                        preview = last_message.text_content[:80]
                    elif last_message.message_type == "product_card":
                        preview = "[商品卡片]"
                    elif last_message.message_type == "order_card":
                        preview = "[订单卡片]"
                    elif last_message.sender_type == "system":
                        preview = "[系统消息]"
                    else:
                        preview = "[业务卡片]"
                conversation_item: dict[str, object] = {
                    "conversation_id": conversation.conversation_no,
                    "customer_name": customer.username,
                    "conversation_status": conversation.conversation_status,
                    "unread_count": unread_counts.get(conversation.id, 0),
                    "last_message_preview": preview,
                    "last_message_at": (
                        conversation.last_message_at.isoformat()
                        if conversation.last_message_at
                        else None
                    ),
                    "service_mode": (
                        "human"
                        if ticket is not None
                        and ticket.ticket_status in {"queued", "assigned", "active", "waiting_user"}
                        else "ai"
                    ),
                    "ticket_status": ticket.ticket_status if ticket else None,
                }
                conversations.append(conversation_item)
                if _query_mentions_identifier(
                    query_text, customer.username, conversation.conversation_no
                ):
                    if selected_conversation is None:
                        recent_messages = list(
                            (
                                await session.scalars(
                                    select(Message)
                                    .where(
                                        Message.conversation_id == conversation.id,
                                        Message.message_status == "sent",
                                    )
                                    .order_by(Message.sequence_no.desc())
                                    .limit(20)
                                )
                            ).all()
                        )
                        context_rows = list(
                            (
                                await session.scalars(
                                    select(ConversationContext)
                                    .where(
                                        ConversationContext.conversation_id == conversation.id,
                                        ConversationContext.context_status == "active",
                                    )
                                    .order_by(ConversationContext.created_at.desc())
                                    .limit(8)
                                )
                            ).all()
                        )
                        selected_conversation = {
                            **conversation_item,
                            "recent_messages": [
                                {
                                    "message_id": message.message_no,
                                    "sequence": int(message.sequence_no),
                                    "sender": message.sender_type,
                                    "type": message.message_type,
                                    "text": (message.text_content or "")[:500] or None,
                                    "card": message.content_payload,
                                    "sent_at": message.sent_at.isoformat(),
                                }
                                for message in reversed(recent_messages)
                            ],
                            "active_contexts": [
                                {
                                    "type": item.context_type,
                                    "resource_id": item.resource_no,
                                    "resource_version": item.resource_version,
                                    "display": item.display_snapshot,
                                }
                                for item in context_rows
                            ],
                        }
            conversation_result: dict[str, object] = {
                "store_id": context.store.store_no,
                "conversation_status_counts": conversation_counts,
                "ticket_status_counts": ticket_counts,
                "waiting_human_count": sum(
                    int(ticket_counts.get(key, 0)) for key in ("queued", "assigned", "active")
                ),
                "active_tickets": [
                    {
                        "ticket_id": ticket.ticket_no,
                        "customer_name": customer.username,
                        "status": ticket.ticket_status,
                        "priority": ticket.priority,
                        "summary": ticket.handoff_summary,
                        "created_at": ticket.created_at.isoformat(),
                    }
                    for ticket, customer in active_ticket_rows
                ],
                "conversations": conversations,
                "selected_conversation": selected_conversation,
                "total_unread_count": sum(unread_counts.values()),
                "query_mode": (
                    "conversation_detail"
                    if selected_conversation is not None
                    else "conversation_list"
                    if tool_code == "store_ops.conversations.list"
                    else "service_summary"
                ),
            }
            if tool_code == "store_ops.conversations.list":
                conversation_result["pagination"] = {
                    "tool_code": tool_code,
                    "page_size": conversation_page_size,
                    "returned_count": len(conversation_rows),
                    "has_more": conversation_has_more,
                    "next_cursor": conversation_next_cursor,
                    "continued": conversation_pagination_requested,
                }
            return conversation_result
        if tool_code == "store_ops.after_sale.list":
            compact_query = re.sub(r"\s+", "", query_text).casefold()
            explicit_refund_no = next(
                iter(re.findall(r"(?:ref|rfd)_[0-9a-z]+", compact_query, flags=re.IGNORECASE)),
                None,
            )
            explicit_appeal_no = next(
                iter(re.findall(r"rap_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
            )
            explicit_refund_payment_no = next(
                iter(re.findall(r"rfp_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
            )
            explicit_order_no = next(
                iter(re.findall(r"ord_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
            )
            merchant_refund_statuses: set[str] = set()
            for status, markers in {
                "submitted": ("待受理", "已提交"),
                "merchant_review": ("待处理", "商家审核"),
                "approved": ("已同意", "已批准"),
                "waiting_return": ("待退货",),
                "returning": ("退货中",),
                "received": ("已收到退货", "商家收货"),
                "refunding": ("退款中",),
                "succeeded": ("退款成功",),
                "rejected": ("已拒绝", "退款拒绝"),
                "cancelled": ("已取消",),
                "closed": ("已关闭",),
            }.items():
                if any(marker in compact_query for marker in markers):
                    merchant_refund_statuses.add(status)

            merchant_refund_customer_rows = list(
                (
                    await session.execute(
                        select(User.id, User.username, User.user_no)
                        .join(RefundApplication, RefundApplication.user_id == User.id)
                        .where(RefundApplication.store_id == store_id)
                        .distinct()
                        .limit(500)
                    )
                ).all()
            )
            merchant_refund_customer = next(
                (
                    (customer_id, username, user_no)
                    for customer_id, username, user_no in merchant_refund_customer_rows
                    if _query_mentions_identifier(query_text, username, user_no)
                ),
                None,
            )
            qualified_customer = _qualified_scope_name(query_text, ("用户", "顾客", "客户", "账号"))
            if qualified_customer and merchant_refund_customer is None:
                return {
                    "store_id": context.store.store_no,
                    "query_mode": "after_sale_list",
                    "applied_filters": {"customer_name": qualified_customer},
                    "refund_status_counts": {},
                    "recent_refunds": [],
                    "result_count": 0,
                    "exact_target_matched": False,
                }
            refund_counts = await _counts(
                session,
                RefundApplication.refund_status,
                RefundApplication.store_id == store_id,
            )
            merchant_refund_conditions: list[Any] = [RefundApplication.store_id == store_id]
            if explicit_refund_no:
                merchant_refund_conditions.append(RefundApplication.refund_no == explicit_refund_no)
            if explicit_order_no:
                merchant_refund_conditions.append(Order.order_no == explicit_order_no)
            if merchant_refund_customer:
                merchant_refund_conditions.append(
                    RefundApplication.user_id == merchant_refund_customer[0]
                )
            if merchant_refund_statuses:
                merchant_refund_conditions.append(
                    RefundApplication.refund_status.in_(merchant_refund_statuses)
                )
            merchant_refund_statement = (
                select(RefundApplication, Order, User)
                .join(Order, Order.id == RefundApplication.order_id)
                .join(User, User.id == RefundApplication.user_id)
            )
            if explicit_appeal_no:
                merchant_refund_statement = merchant_refund_statement.join(
                    RefundAppeal, RefundAppeal.refund_id == RefundApplication.id
                )
                merchant_refund_conditions.append(RefundAppeal.appeal_no == explicit_appeal_no)
            if explicit_refund_payment_no:
                merchant_refund_statement = merchant_refund_statement.join(
                    RefundPaymentRecord,
                    RefundPaymentRecord.refund_id == RefundApplication.id,
                )
                merchant_refund_conditions.append(
                    RefundPaymentRecord.refund_payment_no == explicit_refund_payment_no
                )
            merchant_refund_rows = (
                await session.execute(
                    merchant_refund_statement.where(*merchant_refund_conditions)
                    .distinct()
                    .order_by(
                        case(
                            (RefundApplication.refund_status == "merchant_review", 0),
                            (RefundApplication.refund_status == "submitted", 1),
                            else_=2,
                        ),
                        RefundApplication.created_at.desc(),
                        RefundApplication.id.desc(),
                    )
                    .limit(10)
                )
            ).all()
            recent_refunds: list[dict[str, object]] = []
            for refund, order, customer in merchant_refund_rows:
                refund_item_rows = list(
                    (
                        await session.execute(
                            select(RefundItem, OrderItem)
                            .join(OrderItem, OrderItem.id == RefundItem.order_item_id)
                            .where(RefundItem.refund_id == refund.id)
                            .order_by(RefundItem.id)
                        )
                    ).all()
                )
                refund_events = list(
                    (
                        await session.scalars(
                            select(RefundEvent)
                            .where(RefundEvent.refund_id == refund.id)
                            .order_by(RefundEvent.created_at.desc(), RefundEvent.id.desc())
                            .limit(20)
                        )
                    ).all()
                )
                return_shipment = await session.scalar(
                    select(RefundShipment).where(RefundShipment.refund_id == refund.id)
                )
                refund_payments = list(
                    (
                        await session.scalars(
                            select(RefundPaymentRecord)
                            .where(RefundPaymentRecord.refund_id == refund.id)
                            .order_by(RefundPaymentRecord.id.desc())
                        )
                    ).all()
                )
                appeals = list(
                    (
                        await session.scalars(
                            select(RefundAppeal)
                            .where(RefundAppeal.refund_id == refund.id)
                            .order_by(RefundAppeal.created_at.desc(), RefundAppeal.id.desc())
                        )
                    ).all()
                )
                item_values: list[dict[str, object]] = []
                for refund_item, order_item in refund_item_rows:
                    image_file = (
                        await session.scalar(
                            select(FileObject).where(
                                FileObject.object_key == order_item.image_object_key,
                                FileObject.file_status == "active",
                                FileObject.scan_status == "safe",
                            )
                        )
                        if order_item.image_object_key
                        else None
                    )
                    item_values.append(
                        {
                            "order_item_id": order_item.order_item_no,
                            "product_id": order_item.product_no,
                            "product_name": order_item.product_name,
                            "sku_name": order_item.sku_name,
                            "quantity": refund_item.quantity,
                            "requested_amount": {
                                "minor_units": refund_item.requested_amount,
                                "currency": refund.currency,
                                "display": _money_display(
                                    refund_item.requested_amount, refund.currency
                                ),
                            },
                            "succeeded_amount": {
                                "minor_units": refund_item.succeeded_amount,
                                "currency": refund.currency,
                                "display": _money_display(
                                    refund_item.succeeded_amount, refund.currency
                                ),
                            },
                            "image_url": (
                                f"/api/v1/files/{image_file.file_no}?variant=thumbnail"
                                if image_file
                                else None
                            ),
                        }
                    )
                payment_values: list[dict[str, object]] = []
                for payment in refund_payments:
                    payment_events = list(
                        (
                            await session.scalars(
                                select(RefundPaymentEvent)
                                .where(RefundPaymentEvent.refund_payment_id == payment.id)
                                .order_by(
                                    RefundPaymentEvent.created_at.desc(),
                                    RefundPaymentEvent.id.desc(),
                                )
                                .limit(12)
                            )
                        ).all()
                    )
                    payment_values.append(
                        {
                            "refund_payment_id": payment.refund_payment_no,
                            "provider": payment.provider,
                            "provider_refund_no_masked": _mask_business_reference(
                                payment.provider_refund_no
                            ),
                            "status": payment.payment_status,
                            "amount": {
                                "minor_units": payment.amount,
                                "currency": payment.currency,
                                "display": _money_display(payment.amount, payment.currency),
                            },
                            "completed_at": (
                                payment.completed_at.isoformat() if payment.completed_at else None
                            ),
                            "version": int(payment.version),
                            "events": [
                                {
                                    "event_id": event.event_no,
                                    "provider_status": event.provider_status,
                                    "signature_valid": bool(event.signature_valid),
                                    "occurred_at": event.created_at.isoformat(),
                                }
                                for event in payment_events
                            ],
                        }
                    )
                appeal_values: list[dict[str, object]] = []
                for appeal in appeals:
                    appeal_events = list(
                        (
                            await session.scalars(
                                select(RefundAppealEvent)
                                .where(RefundAppealEvent.appeal_id == appeal.id)
                                .order_by(
                                    RefundAppealEvent.created_at.desc(),
                                    RefundAppealEvent.id.desc(),
                                )
                                .limit(12)
                            )
                        ).all()
                    )
                    appeal_values.append(
                        {
                            "appeal_id": appeal.appeal_no,
                            "status": appeal.appeal_status,
                            "reason": appeal.reason,
                            "resolution_detail": appeal.resolution_detail,
                            "submitted_at": appeal.created_at.isoformat(),
                            "decided_at": (
                                appeal.decided_at.isoformat() if appeal.decided_at else None
                            ),
                            "version": int(appeal.version),
                            "events": [
                                {
                                    "event_id": event.event_no,
                                    "event_type": event.event_type,
                                    "to_status": event.to_status,
                                    "actor_type": event.actor_type,
                                    "remark": event.remark,
                                    "occurred_at": event.created_at.isoformat(),
                                }
                                for event in appeal_events
                            ],
                        }
                    )
                recent_refunds.append(
                    {
                        "refund_id": refund.refund_no,
                        "order_id": order.order_no,
                        "customer_name": customer.username,
                        "product_name": (
                            str(item_values[0].get("product_name")) if item_values else "本店订单"
                        ),
                        "refund_type": refund.refund_type,
                        "status": refund.refund_status,
                        "reason_code": refund.reason_code,
                        "reason_detail": refund.reason_detail,
                        "requested_amount": {
                            "minor_units": refund.requested_amount,
                            "currency": refund.currency,
                            "display": _money_display(refund.requested_amount, refund.currency),
                        },
                        "submitted_at": refund.submitted_at.isoformat(),
                        "version": int(refund.version),
                        "items": item_values,
                        "events": [
                            {
                                "event_id": event.event_no,
                                "from_status": event.from_status,
                                "to_status": event.to_status,
                                "event_code": event.event_code,
                                "actor_type": event.actor_type,
                                "reason": event.reason,
                                "occurred_at": event.created_at.isoformat(),
                            }
                            for event in refund_events
                        ],
                        "return_shipment": (
                            {
                                "carrier_name": return_shipment.carrier_name,
                                "tracking_no_masked": return_shipment.tracking_no_masked,
                                "status": return_shipment.shipment_status,
                                "shipped_at": (
                                    return_shipment.shipped_at.isoformat()
                                    if return_shipment.shipped_at
                                    else None
                                ),
                                "received_at": (
                                    return_shipment.received_at.isoformat()
                                    if return_shipment.received_at
                                    else None
                                ),
                                "version": int(return_shipment.version),
                            }
                            if return_shipment
                            else None
                        ),
                        "refund_payments": payment_values,
                        "appeals": appeal_values,
                    }
                )
            detail_requested = bool(
                explicit_refund_no
                or explicit_order_no
                or explicit_appeal_no
                or explicit_refund_payment_no
                or any(
                    marker in compact_query
                    for marker in ("详情", "时间线", "完整", "进度", "退货物流", "退款支付", "申诉")
                )
            )
            return {
                "store_id": context.store.store_no,
                "query_mode": (
                    "after_sale_detail"
                    if detail_requested and len(recent_refunds) == 1
                    else "after_sale_list"
                ),
                "applied_filters": {
                    "refund_id": explicit_refund_no,
                    "order_id": explicit_order_no,
                    "appeal_id": explicit_appeal_no,
                    "refund_payment_id": explicit_refund_payment_no,
                    "customer_name": (
                        merchant_refund_customer[1] if merchant_refund_customer else None
                    ),
                    "statuses": sorted(merchant_refund_statuses),
                },
                "refund_status_counts": refund_counts,
                "recent_refunds": recent_refunds,
                "result_count": len(recent_refunds),
                "exact_target_matched": bool(
                    len(recent_refunds) == 1
                    and (
                        explicit_refund_no
                        or explicit_order_no
                        or explicit_appeal_no
                        or explicit_refund_payment_no
                    )
                ),
            }
        if tool_code == "store_ops.policy_summary":
            policy_counts = await _counts(
                session,
                StoreServicePolicy.policy_status,
                StoreServicePolicy.store_id == store_id,
            )
            published = list(
                (
                    await session.execute(
                        select(StoreServicePolicy.policy_type, StoreServicePolicy.title).where(
                            StoreServicePolicy.store_id == store_id,
                            StoreServicePolicy.policy_status == "published",
                        )
                    )
                ).all()
            )
            return {
                "store_id": context.store.store_no,
                "policy_status_counts": policy_counts,
                "published_policies": [
                    {"policy_type": policy_type, "title": title}
                    for policy_type, title in published[:20]
                ],
            }
        low_stock = int(
            await session.scalar(
                select(func.count(Inventory.id))
                .join(ProductSku, ProductSku.id == Inventory.sku_id)
                .join(Product, Product.id == ProductSku.product_id)
                .where(
                    ProductSku.store_id == store_id,
                    Product.deleted_at.is_(None),
                    Product.product_status == "on_sale",
                    ProductSku.sku_status == "active",
                    Inventory.inventory_status == "active",
                    Inventory.on_hand_quantity - Inventory.reserved_quantity
                    <= Inventory.safety_stock_quantity,
                )
            )
            or 0
        )
        return {
            "store": {
                "store_id": context.store.store_no,
                "name": context.store.store_name,
                "status": context.store.store_status,
                "rating": str(context.store.rating_score),
            },
            "product_status_counts": product_counts,
            "order_status_counts": order_counts,
            "completed_order_revenue": {
                "minor_units": revenue,
                "currency": "CNY",
                "display": _money_display(revenue, "CNY"),
            },
            "unsettled_paid_amount": {
                "minor_units": unsettled_paid_amount,
                "currency": "CNY",
                "display": _money_display(unsettled_paid_amount, "CNY"),
            },
            "low_stock_sku_count": low_stock,
        }

    governance_now = utc_now()
    platform_customer_clause = _is_platform_customer_clause(governance_now)
    user_counts = await _counts(
        session,
        User.user_status,
        User.deleted_at.is_(None),
        platform_customer_clause,
    )
    store_counts = await _counts(session, Store.store_status)
    order_counts = await _counts(session, Order.order_status)
    product_counts = await _counts(session, Product.product_status, Product.deleted_at.is_(None))
    if tool_code in {
        "governance.user_summary",
        "governance.users.search",
        "governance.users.addresses.list",
        "governance.users.cart.list",
        "governance.users.favorites.list",
        "governance.users.orders.list",
        "governance.users.wallet.get",
    }:
        now = governance_now
        users = list(
            (
                await session.scalars(
                    select(User)
                    .where(
                        User.deleted_at.is_(None),
                        platform_customer_clause,
                    )
                    .order_by(User.last_login_at.desc(), User.id.desc())
                    .limit(200)
                )
            ).all()
        )
        named_users = [
            user
            for user in users
            if _query_mentions_identifier(query_text, user.username, user.user_no)
        ]
        visible_users = (named_users if named_users else users)[:8]
        user_items: list[dict[str, object]] = []
        for user in visible_users:
            wallet = await session.scalar(
                select(UserWallet).where(
                    UserWallet.user_id == user.id,
                    UserWallet.currency == "CNY",
                )
            )
            order_count = int(
                await session.scalar(
                    select(func.count(Order.id)).where(
                        Order.user_id == user.id,
                        Order.order_status.not_in(("cancelled", "closed")),
                    )
                )
                or 0
            )
            active_session_count = int(
                await session.scalar(
                    select(func.count(AuthSession.id)).where(
                        AuthSession.user_id == user.id,
                        AuthSession.revoked_at.is_(None),
                        AuthSession.expires_at > now,
                    )
                )
                or 0
            )
            address_count = int(
                await session.scalar(
                    select(func.count(UserAddress.id)).where(
                        UserAddress.user_id == user.id,
                        UserAddress.deleted_at.is_(None),
                    )
                )
                or 0
            )
            product_favorite_count = int(
                await session.scalar(
                    select(func.count(ProductFavorite.id)).where(
                        ProductFavorite.user_id == user.id,
                        ProductFavorite.deleted_at.is_(None),
                    )
                )
                or 0
            )
            store_follow_count = int(
                await session.scalar(
                    select(func.count(StoreFollow.id)).where(
                        StoreFollow.user_id == user.id,
                        StoreFollow.deleted_at.is_(None),
                    )
                )
                or 0
            )
            cart_item_count = int(
                await session.scalar(
                    select(func.count(CartItem.id))
                    .join(Cart, Cart.id == CartItem.cart_id)
                    .where(Cart.user_id == user.id)
                )
                or 0
            )
            user_items.append(
                {
                    "user_id": user.user_no,
                    "username": user.username,
                    "status": user.user_status,
                    "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
                    "registered_at": user.registered_at.isoformat(),
                    "wallet": {
                        "minor_units": wallet.balance_amount if wallet else 0,
                        "currency": "CNY",
                        "display": _money_display(wallet.balance_amount if wallet else 0, "CNY"),
                        "version": wallet.version if wallet else 0,
                    },
                    "visible_order_count": order_count,
                    "active_session_count": active_session_count,
                    "online_status": "online" if active_session_count else "offline",
                    "address_count": address_count,
                    "cart_item_count": cart_item_count,
                    "product_favorite_count": product_favorite_count,
                    "store_follow_count": store_follow_count,
                    "version": int(user.version),
                }
            )
        selected_user_detail: dict[str, object] | None = None
        if len(named_users) == 1:
            selected_user = named_users[0]
            selected_user_item = next(
                (item for item in user_items if item.get("user_id") == selected_user.user_no),
                None,
            )
            address_rows = list(
                (
                    await session.scalars(
                        select(UserAddress)
                        .where(
                            UserAddress.user_id == selected_user.id,
                            UserAddress.deleted_at.is_(None),
                        )
                        .order_by(UserAddress.is_default.desc(), UserAddress.id.desc())
                        .limit(8)
                    )
                ).all()
            )
            selected_user_order_rows = list(
                (
                    await session.execute(
                        select(Order, Store)
                        .join(Store, Store.id == Order.store_id)
                        .where(
                            Order.user_id == selected_user.id,
                            Order.order_status.not_in(("cancelled", "closed")),
                        )
                        .order_by(Order.created_at.desc(), Order.id.desc())
                        .limit(5)
                    )
                ).all()
            )
            selected_user_cart_rows = list(
                (
                    await session.execute(
                        select(CartItem, ProductSku, Product, Store)
                        .join(Cart, Cart.id == CartItem.cart_id)
                        .join(ProductSku, ProductSku.id == CartItem.sku_id)
                        .join(Product, Product.id == ProductSku.product_id)
                        .join(Store, Store.id == Product.store_id)
                        .where(Cart.user_id == selected_user.id)
                        .order_by(CartItem.updated_at.desc(), CartItem.id.desc())
                        .limit(12)
                    )
                ).all()
            )
            selected_user_favorite_rows = list(
                (
                    await session.execute(
                        select(ProductFavorite, Product, Store)
                        .join(Product, Product.id == ProductFavorite.product_id)
                        .join(Store, Store.id == Product.store_id)
                        .where(
                            ProductFavorite.user_id == selected_user.id,
                            ProductFavorite.deleted_at.is_(None),
                        )
                        .order_by(ProductFavorite.favorited_at.desc(), ProductFavorite.id.desc())
                        .limit(12)
                    )
                ).all()
            )
            selected_user_follow_rows = list(
                (
                    await session.execute(
                        select(StoreFollow, Store)
                        .join(Store, Store.id == StoreFollow.store_id)
                        .where(
                            StoreFollow.user_id == selected_user.id,
                            StoreFollow.deleted_at.is_(None),
                        )
                        .order_by(StoreFollow.followed_at.desc(), StoreFollow.id.desc())
                        .limit(12)
                    )
                ).all()
            )
            selected_user_wallet = await session.scalar(
                select(UserWallet).where(
                    UserWallet.user_id == selected_user.id,
                    UserWallet.currency == "CNY",
                )
            )
            selected_user_wallet_transactions = (
                list(
                    (
                        await session.scalars(
                            select(WalletTransaction)
                            .where(WalletTransaction.wallet_id == selected_user_wallet.id)
                            .order_by(
                                WalletTransaction.occurred_at.desc(),
                                WalletTransaction.id.desc(),
                            )
                            .limit(12)
                        )
                    ).all()
                )
                if selected_user_wallet is not None
                else []
            )
            selected_user_detail = {
                **(selected_user_item or {}),
                "addresses": [
                    {
                        "address_id": address.address_no,
                        "region_codes": [
                            address.province_code,
                            address.city_code,
                            address.district_code,
                        ],
                        "phone_masked": f"*** **** {address.phone_last4}",
                        "is_default": address.is_default,
                    }
                    for address in address_rows
                ],
                "recent_orders": [
                    {
                        "order_id": order.order_no,
                        "store_id": store.store_no,
                        "store_name": store.store_name,
                        "status": order.order_status,
                        "payment_status": order.payment_status,
                        "fulfillment_status": order.fulfillment_status,
                        "after_sale_status": order.after_sale_status,
                        "amount": {
                            "minor_units": order.paid_amount,
                            "currency": order.currency,
                            "display": _money_display(order.paid_amount, order.currency),
                        },
                        "created_at": order.created_at.isoformat(),
                    }
                    for order, store in selected_user_order_rows
                ],
                "cart_items": [
                    {
                        "cart_item_id": item.cart_item_no,
                        "product_id": product.product_no,
                        "product_name": product.product_name,
                        "sku_id": sku.sku_no,
                        "sku_name": sku.sku_name,
                        "store_id": store.store_no,
                        "store_name": store.store_name,
                        "quantity": int(item.quantity),
                        "selected": bool(item.is_selected),
                        "current_price": {
                            "minor_units": sku.sale_price_amount,
                            "currency": sku.currency,
                            "display": _money_display(sku.sale_price_amount, sku.currency),
                        },
                        "version": int(item.version),
                    }
                    for item, sku, product, store in selected_user_cart_rows
                ],
                "favorite_products": [
                    {
                        "product_id": product.product_no,
                        "product_name": product.product_name,
                        "store_id": store.store_no,
                        "store_name": store.store_name,
                        "status": product.product_status,
                        "favorited_at": favorite.favorited_at.isoformat(),
                    }
                    for favorite, product, store in selected_user_favorite_rows
                ],
                "followed_stores": [
                    {
                        "store_id": store.store_no,
                        "store_name": store.store_name,
                        "status": store.store_status,
                        "followed_at": follow.followed_at.isoformat(),
                    }
                    for follow, store in selected_user_follow_rows
                ],
                "wallet_transactions": [
                    {
                        "transaction_id": transaction.transaction_no,
                        "type": transaction.transaction_type,
                        "direction": transaction.direction,
                        "amount": {
                            "minor_units": transaction.amount,
                            "currency": transaction.currency,
                            "display": _money_display(transaction.amount, transaction.currency),
                        },
                        "balance_after": {
                            "minor_units": transaction.balance_after,
                            "currency": transaction.currency,
                            "display": _money_display(
                                transaction.balance_after, transaction.currency
                            ),
                        },
                        "description": transaction.description,
                        "occurred_at": transaction.occurred_at.isoformat(),
                    }
                    for transaction in selected_user_wallet_transactions
                ],
            }
        return {
            "user_status_counts": user_counts,
            "recent_users": user_items,
            "selected_user": selected_user_detail,
            "search_matched": bool(named_users),
            "search_result_count": len(named_users) if named_users else len(users),
            "requested_user_asset": {
                "governance.users.addresses.list": "addresses",
                "governance.users.cart.list": "cart",
                "governance.users.favorites.list": "favorites",
                "governance.users.orders.list": "orders",
                "governance.users.wallet.get": "wallet",
            }.get(tool_code),
        }
    if tool_code == "governance.stores.service_profile":
        stores = list(
            (
                await session.scalars(
                    select(Store).order_by(Store.sales_count.desc(), Store.id.desc()).limit(200)
                )
            ).all()
        )
        named_stores = [
            store
            for store in stores
            if _query_mentions_identifier(query_text, store.store_name, store.store_no)
        ]
        if len(named_stores) != 1:
            return {
                "service_profile": None,
                "search_matched": bool(named_stores),
                "search_result_count": len(named_stores),
                "selection_required": True,
                "matching_stores": [
                    {"store_id": store.store_no, "store_name": store.store_name}
                    for store in named_stores[:8]
                ],
            }

        selected_store = named_stores[0]
        now = utc_now()
        policy_rows = list(
            (
                await session.scalars(
                    select(StoreServicePolicy)
                    .where(
                        StoreServicePolicy.store_id == selected_store.id,
                        StoreServicePolicy.policy_status == "published",
                        or_(
                            StoreServicePolicy.effective_at.is_(None),
                            StoreServicePolicy.effective_at <= now,
                        ),
                        or_(
                            StoreServicePolicy.expires_at.is_(None),
                            StoreServicePolicy.expires_at > now,
                        ),
                    )
                    .order_by(
                        StoreServicePolicy.policy_type,
                        StoreServicePolicy.policy_version.desc(),
                    )
                )
            ).all()
        )
        latest_policies: dict[str, StoreServicePolicy] = {}
        for policy in policy_rows:
            latest_policies.setdefault(policy.policy_type, policy)

        templates = list(
            (
                await session.scalars(
                    select(ShippingTemplate)
                    .where(
                        ShippingTemplate.store_id == selected_store.id,
                        ShippingTemplate.template_status == "effective",
                    )
                    .order_by(ShippingTemplate.policy_version.desc(), ShippingTemplate.id.desc())
                )
            ).all()
        )
        fulfillment_rows = list(
            (
                await session.execute(
                    select(Product, ProductFulfillmentProfile)
                    .outerjoin(
                        ProductFulfillmentProfile,
                        ProductFulfillmentProfile.product_id == Product.id,
                    )
                    .where(
                        Product.store_id == selected_store.id,
                        Product.deleted_at.is_(None),
                    )
                    .order_by(Product.id)
                )
            ).all()
        )
        fulfillment_profiles = [
            profile for _product, profile in fulfillment_rows if profile is not None
        ]
        origin_region_codes = sorted(
            {
                profile.origin_region_code
                for profile in fulfillment_profiles
                if profile.origin_region_code
            }
        )
        dispatch_min_hours = (
            min(profile.dispatch_min_hours for profile in fulfillment_profiles)
            if fulfillment_profiles
            else None
        )
        dispatch_max_hours = (
            max(profile.dispatch_max_hours for profile in fulfillment_profiles)
            if fulfillment_profiles
            else None
        )
        recent_carrier_rows = list(
            (
                await session.execute(
                    select(Shipment.carrier_code, Shipment.carrier_name, func.count(Shipment.id))
                    .where(Shipment.store_id == selected_store.id)
                    .group_by(Shipment.carrier_code, Shipment.carrier_name)
                    .order_by(func.count(Shipment.id).desc(), Shipment.carrier_name)
                    .limit(8)
                )
            ).all()
        )
        published_policy_entries = [
            {
                "policy_type": policy.policy_type,
                "title": policy.title,
                "content": policy.content,
                "version": int(policy.policy_version),
                "effective_at": policy.effective_at.isoformat() if policy.effective_at else None,
                "expires_at": policy.expires_at.isoformat() if policy.expires_at else None,
            }
            for policy in latest_policies.values()
        ]
        after_sale_policies = [
            policy
            for policy in published_policy_entries
            if any(
                marker in f"{policy.get('policy_type', '')}{policy.get('title', '')}".casefold()
                for marker in ("after_sale", "refund", "return", "售后", "退款", "退换")
            )
        ]
        missing_items: list[str] = []
        if not (selected_store.description or "").strip():
            missing_items.append("店铺简介")
        if not origin_region_codes:
            missing_items.append("商品发货地")
        if dispatch_min_hours is None or dispatch_max_hours is None:
            missing_items.append("商品发货时效")
        if not templates:
            missing_items.append("有效配送模板")
        if not after_sale_policies:
            missing_items.append("已发布售后政策")

        return {
            "service_profile": {
                "store_id": selected_store.store_no,
                "store_name": selected_store.store_name,
                "status": selected_store.store_status,
                "version": int(selected_store.version),
                "description": selected_store.description,
                "product_count": len(fulfillment_rows),
                "fulfillment_configured_count": len(fulfillment_profiles),
                "origin_region_codes": origin_region_codes,
                "dispatch_min_hours": dispatch_min_hours,
                "dispatch_max_hours": dispatch_max_hours,
                "shipping_templates": [
                    {
                        "template_id": template.template_no,
                        "template_name": template.template_name,
                        "delivery_type": template.delivery_type,
                        "charge_mode": template.charge_mode,
                        "dispatch_min_hours": template.dispatch_min_hours,
                        "dispatch_max_hours": template.dispatch_max_hours,
                        "version": int(template.policy_version),
                    }
                    for template in templates
                ],
                "default_carrier": {
                    "configured": False,
                    "value": None,
                    "reason": "当前系统不设置店铺级默认快递，承运商在创建实际包裹时确定",
                    "recent_carriers": [
                        {"carrier_code": code, "carrier_name": name, "shipment_count": int(count)}
                        for code, name, count in recent_carrier_rows
                    ],
                },
                "published_policies": published_policy_entries,
                "after_sale_policies": after_sale_policies,
                "missing_items": missing_items,
                "is_complete": not missing_items,
            },
            "search_matched": True,
            "search_result_count": 1,
            "selection_required": False,
        }
    if tool_code in {"governance.store_summary", "governance.stores.search"}:
        stores = list(
            (
                await session.scalars(
                    select(Store).order_by(Store.sales_count.desc(), Store.id.desc()).limit(200)
                )
            ).all()
        )
        named_stores = [
            store
            for store in stores
            if _query_mentions_identifier(query_text, store.store_name, store.store_no)
        ]
        visible_stores = (named_stores if named_stores else stores)[:8]
        store_items: list[dict[str, object]] = []
        for store in visible_stores:
            product_count = int(
                await session.scalar(
                    select(func.count(Product.id)).where(
                        Product.store_id == store.id,
                        Product.deleted_at.is_(None),
                    )
                )
                or 0
            )
            completed_revenue = int(
                await session.scalar(
                    select(
                        func.coalesce(func.sum(Order.paid_amount - Order.refunded_amount), 0)
                    ).where(
                        Order.store_id == store.id,
                        Order.order_status == "completed",
                    )
                )
                or 0
            )
            owner = await session.scalar(select(User).where(User.id == store.owner_user_id))
            store_order_counts = await _counts(
                session,
                Order.order_status,
                Order.store_id == store.id,
                Order.order_status.not_in(("cancelled", "closed")),
            )
            store_product_counts = await _counts(
                session,
                Product.product_status,
                Product.store_id == store.id,
                Product.deleted_at.is_(None),
            )
            pending_after_sale_count = int(
                await session.scalar(
                    select(func.count(RefundApplication.id)).where(
                        RefundApplication.store_id == store.id,
                        RefundApplication.refund_status.in_(
                            (
                                "submitted",
                                "merchant_review",
                                "approved",
                                "waiting_return",
                                "returning",
                                "received",
                                "refunding",
                            )
                        ),
                    )
                )
                or 0
            )
            store_items.append(
                {
                    "store_id": store.store_no,
                    "store_name": store.store_name,
                    "status": store.store_status,
                    "sales_count": store.sales_count,
                    "rating": str(store.rating_score),
                    "product_count": product_count,
                    "owner_username": owner.username if owner else None,
                    "order_status_counts": store_order_counts,
                    "product_status_counts": store_product_counts,
                    "pending_after_sale_count": pending_after_sale_count,
                    "completed_revenue": {
                        "minor_units": completed_revenue,
                        "currency": "CNY",
                        "display": _money_display(completed_revenue, "CNY"),
                    },
                    "version": int(store.version),
                }
            )
        selected_store_detail: dict[str, object] | None = None
        if len(named_stores) == 1:
            selected_store = named_stores[0]
            selected_store_item = next(
                (item for item in store_items if item.get("store_id") == selected_store.store_no),
                None,
            )
            selected_products = list(
                (
                    await session.scalars(
                        select(Product)
                        .where(
                            Product.store_id == selected_store.id,
                            Product.deleted_at.is_(None),
                        )
                        .order_by(Product.sales_count.desc(), Product.id.desc())
                        .limit(8)
                    )
                ).all()
            )
            selected_orders = list(
                (
                    await session.execute(
                        select(Order, User)
                        .join(User, User.id == Order.user_id)
                        .where(
                            Order.store_id == selected_store.id,
                            Order.order_status.not_in(("cancelled", "closed")),
                        )
                        .order_by(Order.created_at.desc(), Order.id.desc())
                        .limit(5)
                    )
                ).all()
            )
            selected_store_detail = {
                **(selected_store_item or {}),
                "description": selected_store.description,
                "rating_count": selected_store.rating_count,
                "follower_count": selected_store.follower_count,
                "products": [
                    {
                        "product_id": product.product_no,
                        "product_name": product.product_name,
                        "status": product.product_status,
                        "sales_count": product.sales_count,
                        "version": int(product.version),
                    }
                    for product in selected_products
                ],
                "recent_orders": [
                    {
                        "order_id": order.order_no,
                        "customer_name": customer.username,
                        "status": order.order_status,
                        "payment_status": order.payment_status,
                        "fulfillment_status": order.fulfillment_status,
                        "after_sale_status": order.after_sale_status,
                        "amount": {
                            "minor_units": order.paid_amount,
                            "currency": order.currency,
                            "display": _money_display(order.paid_amount, order.currency),
                        },
                        "created_at": order.created_at.isoformat(),
                    }
                    for order, customer in selected_orders
                ],
            }
        return {
            "store_status_counts": store_counts,
            "product_status_counts": product_counts,
            "stores": store_items,
            "selected_store": selected_store_detail,
            "search_matched": bool(named_stores),
            "search_result_count": len(named_stores) if named_stores else len(stores),
        }
    if tool_code == "governance.catalog.search":
        admin_product_rows = (
            await session.execute(
                select(Product, Store, ProductSku)
                .join(Store, Store.id == Product.store_id)
                .outerjoin(
                    ProductSku,
                    (ProductSku.product_id == Product.id) & (ProductSku.sku_status == "active"),
                )
                .where(Product.deleted_at.is_(None))
                .order_by(Product.sales_count.desc(), Product.id.desc(), ProductSku.id)
                .limit(500)
            )
        ).all()
        matched_product_ids = {
            product.id
            for product, store, _sku in admin_product_rows
            if _query_mentions_catalog_product(query_text, product.product_name, product.product_no)
            or _query_mentions_identifier(query_text, store.store_name, store.store_no)
        }
        admin_products: dict[int, dict[str, object]] = {}
        selected_admin_product: Product | None = None
        selected_admin_store: Store | None = None
        for product, store, sku in admin_product_rows:
            if matched_product_ids and product.id not in matched_product_ids:
                continue
            if len(matched_product_ids) == 1 and product.id in matched_product_ids:
                selected_admin_product = product
                selected_admin_store = store
            item = admin_products.setdefault(
                product.id,
                {
                    "product_id": product.product_no,
                    "product_name": product.product_name,
                    "status": product.product_status,
                    "sales_count": product.sales_count,
                    "store_id": store.store_no,
                    "store_name": store.store_name,
                    "minimum_price": None,
                    "version": int(product.version),
                },
            )
            if sku is not None:
                price = item.get("minimum_price")
                if not isinstance(price, int) or sku.sale_price_amount < price:
                    item["minimum_price"] = sku.sale_price_amount
            if len(admin_products) >= 8 and not matched_product_ids:
                break
        selected_product_detail = (
            await _load_product_edit_snapshot(session, selected_admin_product, selected_admin_store)
            if selected_admin_product is not None and selected_admin_store is not None
            else None
        )
        return {
            "product_status_counts": product_counts,
            "products": list(admin_products.values())[:8],
            "selected_product": selected_product_detail,
            "search_matched": bool(matched_product_ids),
            "search_result_count": (
                len(matched_product_ids) if matched_product_ids else len(admin_products)
            ),
        }
    if tool_code == "governance.trade.payment_timeline":
        compact_query = re.sub(r"\s+", "", query_text).casefold()
        explicit_payment_no = next(
            iter(re.findall(r"pay_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
        )
        explicit_trade_no = next(
            iter(re.findall(r"trd_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
        )
        explicit_order_no = next(
            iter(re.findall(r"ord_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
        )
        requested_payment_statuses: set[str] = set()
        for status, markers in {
            "created": ("已创建", "待发起"),
            "pending": ("支付中", "处理中", "待支付确认"),
            "succeeded": ("支付成功", "付款成功", "已支付"),
            "failed": ("支付失败", "付款失败"),
            "closed": ("支付关闭", "已关闭"),
            "partially_refunded": ("部分退款",),
            "refunded": ("全额退款", "已退款"),
        }.items():
            if any(marker in compact_query for marker in markers):
                requested_payment_statuses.add(status)

        payment_customer_rows = list(
            (
                await session.execute(
                    select(User.id, User.username, User.user_no)
                    .join(Payment, Payment.user_id == User.id)
                    .distinct()
                    .limit(500)
                )
            ).all()
        )
        payment_customer = next(
            (
                (customer_id, username, user_no)
                for customer_id, username, user_no in payment_customer_rows
                if _query_mentions_identifier(query_text, username, user_no)
            ),
            None,
        )
        payment_store_rows = list(
            (
                await session.execute(
                    select(Store.id, Store.store_name, Store.store_no)
                    .join(Order, Order.store_id == Store.id)
                    .join(TradeOrder, TradeOrder.id == Order.trade_order_id)
                    .join(Payment, Payment.trade_order_id == TradeOrder.id)
                    .distinct()
                    .limit(500)
                )
            ).all()
        )
        payment_store = next(
            (
                (store_id, store_name, store_no)
                for store_id, store_name, store_no in payment_store_rows
                if _query_mentions_identifier(query_text, store_name, store_no)
            ),
            None,
        )
        qualified_customer = _qualified_scope_name(query_text, ("用户", "顾客", "客户", "账号"))
        qualified_store = _qualified_scope_name(query_text, ("店铺", "商家"))
        if (qualified_customer and payment_customer is None) or (
            qualified_store and payment_store is None
        ):
            return {
                "query_mode": "payment_list",
                "applied_filters": {
                    "customer_name": qualified_customer,
                    "store_name": qualified_store,
                },
                "payments": [],
                "selected_payment": None,
                "result_count": 0,
                "exact_target_matched": False,
            }

        payment_conditions: list[Any] = []
        if explicit_payment_no:
            payment_conditions.append(Payment.payment_no == explicit_payment_no)
        if explicit_trade_no:
            payment_conditions.append(TradeOrder.trade_no == explicit_trade_no)
        if explicit_order_no:
            payment_conditions.append(Order.trade_order_id == Payment.trade_order_id)
            payment_conditions.append(Order.order_no == explicit_order_no)
        if payment_customer:
            payment_conditions.append(Payment.user_id == payment_customer[0])
        if payment_store:
            payment_conditions.append(Order.store_id == payment_store[0])
        if requested_payment_statuses:
            payment_conditions.append(Payment.payment_status.in_(requested_payment_statuses))

        payment_statement = (
            select(Payment, TradeOrder, User)
            .join(TradeOrder, TradeOrder.id == Payment.trade_order_id)
            .join(User, User.id == Payment.user_id)
        )
        if explicit_order_no or payment_store:
            payment_statement = payment_statement.join(
                Order, Order.trade_order_id == Payment.trade_order_id
            )
        payment_rows = list(
            (
                await session.execute(
                    payment_statement.where(*payment_conditions)
                    .distinct()
                    .order_by(Payment.created_at.desc(), Payment.id.desc())
                    .limit(12)
                )
            ).all()
        )
        payment_ids = [payment.id for payment, _trade, _user in payment_rows]
        trade_ids = [trade.id for _payment, trade, _user in payment_rows]
        event_rows = (
            list(
                (
                    await session.scalars(
                        select(PaymentEvent)
                        .where(PaymentEvent.payment_id.in_(payment_ids))
                        .order_by(
                            PaymentEvent.payment_id,
                            PaymentEvent.created_at.desc(),
                            PaymentEvent.id.desc(),
                        )
                    )
                ).all()
            )
            if payment_ids
            else []
        )
        callback_rows = (
            list(
                (
                    await session.scalars(
                        select(PaymentCallback)
                        .where(PaymentCallback.payment_id.in_(payment_ids))
                        .order_by(
                            PaymentCallback.payment_id,
                            PaymentCallback.created_at.desc(),
                            PaymentCallback.id.desc(),
                        )
                    )
                ).all()
            )
            if payment_ids
            else []
        )
        order_store_rows = (
            list(
                (
                    await session.execute(
                        select(
                            Order.trade_order_id,
                            Order.order_no,
                            Store.store_no,
                            Store.store_name,
                        )
                        .join(Store, Store.id == Order.store_id)
                        .where(Order.trade_order_id.in_(trade_ids))
                        .order_by(Order.trade_order_id, Order.id)
                    )
                ).all()
            )
            if trade_ids
            else []
        )
        events_by_payment: dict[int, list[PaymentEvent]] = {}
        for event in event_rows:
            events_by_payment.setdefault(event.payment_id, []).append(event)
        callbacks_by_payment: dict[int, list[PaymentCallback]] = {}
        for callback in callback_rows:
            if callback.payment_id is not None:
                callbacks_by_payment.setdefault(callback.payment_id, []).append(callback)
        orders_by_trade: dict[int, list[dict[str, str]]] = {}
        for trade_id, order_no, store_no, store_name in order_store_rows:
            orders_by_trade.setdefault(trade_id, []).append(
                {
                    "order_id": order_no,
                    "store_id": store_no,
                    "store_name": store_name,
                }
            )

        admin_payment_values: list[dict[str, object]] = []
        for payment, trade, customer in payment_rows:
            events = events_by_payment.get(payment.id, [])
            callbacks = callbacks_by_payment.get(payment.id, [])
            admin_payment_values.append(
                {
                    "payment_id": payment.payment_no,
                    "trade_order_id": trade.trade_no,
                    "customer_id": customer.user_no,
                    "customer_name": customer.username,
                    "orders": orders_by_trade.get(trade.id, []),
                    "provider": payment.provider,
                    "payment_method": payment.payment_method,
                    "provider_trade_no_masked": _mask_business_reference(payment.provider_trade_no),
                    "status": payment.payment_status,
                    "trade_status": trade.trade_status,
                    "requested_amount": {
                        "minor_units": payment.requested_amount,
                        "currency": payment.currency,
                        "display": _money_display(payment.requested_amount, payment.currency),
                    },
                    "paid_amount": {
                        "minor_units": payment.paid_amount,
                        "currency": payment.currency,
                        "display": _money_display(payment.paid_amount, payment.currency),
                    },
                    "refunded_amount": {
                        "minor_units": payment.refunded_amount,
                        "currency": payment.currency,
                        "display": _money_display(payment.refunded_amount, payment.currency),
                    },
                    "created_at": payment.created_at.isoformat(),
                    "expires_at": payment.expires_at.isoformat(),
                    "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
                    "closed_at": payment.closed_at.isoformat() if payment.closed_at else None,
                    "failure_code": payment.failure_code,
                    "failure_message": payment.failure_message,
                    "version": int(payment.version),
                    "events": [
                        {
                            "event_id": event.event_no,
                            "event_type": event.event_type,
                            "from_status": event.from_status,
                            "to_status": event.to_status,
                            "amount": {
                                "minor_units": event.amount,
                                "currency": event.currency,
                                "display": _money_display(event.amount, event.currency),
                            },
                            "source_type": event.source_type,
                            "occurred_at": event.created_at.isoformat(),
                            "provider_occurred_at": (
                                event.provider_occurred_at.isoformat()
                                if event.provider_occurred_at
                                else None
                            ),
                        }
                        for event in events[:20]
                    ],
                    "callbacks": [
                        {
                            "callback_id": callback.callback_no,
                            "provider": callback.provider,
                            "provider_event_id_masked": _mask_business_reference(
                                callback.provider_event_id
                            ),
                            "signature_status": callback.signature_status,
                            "process_status": callback.process_status,
                            "attempt_count": int(callback.attempt_count),
                            "error_code": callback.error_code,
                            "received_at": callback.created_at.isoformat(),
                            "processed_at": (
                                callback.processed_at.isoformat() if callback.processed_at else None
                            ),
                        }
                        for callback in callbacks[:12]
                    ],
                }
            )
        exact_target = bool(explicit_payment_no or explicit_trade_no or explicit_order_no)
        selected_payment = admin_payment_values[0] if len(admin_payment_values) == 1 else None
        return {
            "query_mode": "payment_detail" if selected_payment is not None else "payment_list",
            "applied_filters": {
                "payment_id": explicit_payment_no,
                "trade_order_id": explicit_trade_no,
                "order_id": explicit_order_no,
                "customer_name": payment_customer[1] if payment_customer else None,
                "store_name": payment_store[1] if payment_store else None,
                "statuses": sorted(requested_payment_statuses),
            },
            "payments": admin_payment_values,
            "selected_payment": selected_payment,
            "result_count": len(admin_payment_values),
            "exact_target_matched": exact_target and selected_payment is not None,
        }
    if tool_code == "governance.trade.shipments.get":
        compact_query = re.sub(r"\s+", "", query_text).casefold()
        explicit_shipment_no = next(
            iter(re.findall(r"shp_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
        )
        explicit_order_no = next(
            iter(re.findall(r"ord_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
        )
        shipment_conditions: list[Any] = [Shipment.shipment_status != "voided"]
        if explicit_shipment_no:
            shipment_conditions.append(Shipment.shipment_no == explicit_shipment_no)
        if explicit_order_no:
            shipment_conditions.append(Order.order_no == explicit_order_no)
        requested_shipment_statuses: set[str] = set()
        for status, markers in {
            "created": ("待揽收",),
            "picked_up": ("已揽收",),
            "in_transit": ("运输中", "在途", "派送中"),
            "delivered": ("已签收", "签收完成"),
            "exception": ("物流异常", "运输异常"),
            "returned": ("已退回", "退回包裹"),
        }.items():
            if any(marker in compact_query for marker in markers):
                requested_shipment_statuses.add(status)
        if requested_shipment_statuses:
            shipment_conditions.append(Shipment.shipment_status.in_(requested_shipment_statuses))

        shipment_rows_all = list(
            (
                await session.execute(
                    select(Shipment, Order, Store, User)
                    .join(Order, Order.id == Shipment.order_id)
                    .join(Store, Store.id == Shipment.store_id)
                    .join(User, User.id == Order.user_id)
                    .where(*shipment_conditions)
                    .order_by(Shipment.created_at.desc(), Shipment.id.desc())
                    .limit(100)
                )
            ).all()
        )
        named_rows = [
            row
            for row in shipment_rows_all
            if _query_mentions_identifier(query_text, row[2].store_name, row[2].store_no)
            or _query_mentions_identifier(query_text, row[3].username, row[3].user_no)
        ]
        named_scope_required = bool(
            _qualified_scope_name(query_text, ("用户", "顾客", "客户", "账号"))
            or _qualified_scope_name(query_text, ("店铺", "商家"))
        )
        shipment_rows = (
            named_rows
            if named_rows
            else []
            if named_scope_required
            else shipment_rows_all
            if explicit_shipment_no or explicit_order_no or not shipment_rows_all
            else shipment_rows_all[:12]
        )
        shipment_rows = shipment_rows[:12]
        shipment_ids = [shipment.id for shipment, _order, _store, _user in shipment_rows]
        shipment_item_rows = (
            list(
                (
                    await session.execute(
                        select(ShipmentItem, OrderItem)
                        .join(OrderItem, OrderItem.id == ShipmentItem.order_item_id)
                        .where(ShipmentItem.shipment_id.in_(shipment_ids))
                        .order_by(ShipmentItem.shipment_id, ShipmentItem.id)
                    )
                ).all()
            )
            if shipment_ids
            else []
        )
        shipment_items: dict[int, list[dict[str, object]]] = {}
        for shipment_item, order_item in shipment_item_rows:
            shipment_items.setdefault(shipment_item.shipment_id, []).append(
                {
                    "order_item_id": order_item.order_item_no,
                    "product_id": order_item.product_no,
                    "product_name": order_item.product_name,
                    "sku_name": order_item.sku_name,
                    "quantity": shipment_item.quantity,
                }
            )
        track_rows = (
            list(
                (
                    await session.scalars(
                        select(ShipmentTrack)
                        .where(ShipmentTrack.shipment_id.in_(shipment_ids))
                        .order_by(
                            ShipmentTrack.shipment_id,
                            ShipmentTrack.occurred_at.desc(),
                            ShipmentTrack.id.desc(),
                        )
                    )
                ).all()
            )
            if shipment_ids
            else []
        )
        shipment_tracks: dict[int, list[dict[str, object]]] = {}
        for track in track_rows:
            values = shipment_tracks.setdefault(track.shipment_id, [])
            if len(values) < 12:
                values.append(
                    {
                        "status": track.track_status,
                        "provider_status": track.provider_status,
                        "description": track.description,
                        "location": track.location_text,
                        "occurred_at": track.occurred_at.isoformat(),
                    }
                )
        shipment_address_rows = (
            {
                item.order_id: item
                for item in (
                    await session.scalars(
                        select(OrderAddress).where(
                            OrderAddress.order_id.in_(
                                [order.id for _shipment, order, _store, _user in shipment_rows]
                            )
                        )
                    )
                ).all()
            }
            if shipment_rows
            else {}
        )
        security = SecurityService(get_settings())
        shipment_values: list[dict[str, object]] = []
        for shipment, order, store, customer in shipment_rows:
            address = shipment_address_rows.get(order.id)
            destination: dict[str, object] | None = None
            if address is not None:
                destination = {
                    "province_code": address.province_code,
                    "city_code": address.city_code,
                    "district_code": address.district_code,
                    "address": security.decrypt("address-detail", address.address_ciphertext),
                }
            tracks = shipment_tracks.get(shipment.id, [])
            shipment_values.append(
                {
                    "shipment_id": shipment.shipment_no,
                    "order_id": order.order_no,
                    "store_id": store.store_no,
                    "store_name": store.store_name,
                    "customer_id": customer.user_no,
                    "customer_name": customer.username,
                    "carrier_code": shipment.carrier_code,
                    "carrier_name": shipment.carrier_name,
                    "tracking_no_masked": shipment.tracking_no_masked,
                    "status": shipment.shipment_status,
                    "provider_status": shipment.provider_status,
                    "is_simulated": shipment.carrier_code == "fake_express",
                    "items": shipment_items.get(shipment.id, []),
                    "latest_track": tracks[0] if tracks else None,
                    "tracks": tracks,
                    "destination": destination,
                    "estimated_delivery_min_at": (
                        shipment.estimated_delivery_min_at.isoformat()
                        if shipment.estimated_delivery_min_at
                        else None
                    ),
                    "estimated_delivery_max_at": (
                        shipment.estimated_delivery_max_at.isoformat()
                        if shipment.estimated_delivery_max_at
                        else None
                    ),
                    "last_track_at": (
                        shipment.last_track_at.isoformat() if shipment.last_track_at else None
                    ),
                    "version": int(shipment.version),
                }
            )
        exact_target = bool(explicit_shipment_no or explicit_order_no)
        selected = shipment_values[0] if len(shipment_values) == 1 else None
        return {
            "query_mode": "shipment_detail" if selected is not None else "shipment_list",
            "applied_filters": {
                "shipment_id": explicit_shipment_no,
                "order_id": explicit_order_no,
                "statuses": sorted(requested_shipment_statuses),
                "named_scope_matched": bool(named_rows),
            },
            "shipments": shipment_values,
            "selected_shipment": selected,
            "result_count": len(shipment_values),
            "exact_target_matched": exact_target and selected is not None,
        }
    if tool_code == "governance.order_summary":
        compact_query = re.sub(r"\s+", "", query_text).casefold()
        admin_requested_statuses: set[str] = set()
        admin_status_markers = {
            "pending_payment": ("待付款", "未付款"),
            "paid": ("已付款",),
            "pending_shipment": ("待发货",),
            "shipped": ("运输中", "在途", "物流中"),
            "completed": ("已完成", "完成订单", "待评价"),
        }
        for status, markers in admin_status_markers.items():
            if any(marker in compact_query for marker in markers):
                admin_requested_statuses.add(status)
        admin_explicit_order_no = next(
            iter(re.findall(r"ord_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
        )
        admin_customer_rows = list(
            (
                await session.execute(
                    select(User.id, User.username, User.user_no)
                    .join(Order, Order.user_id == User.id)
                    .distinct()
                    .limit(500)
                )
            ).all()
        )
        admin_requested_customer = next(
            (
                (customer_id, username, user_no)
                for customer_id, username, user_no in admin_customer_rows
                if _query_mentions_identifier(query_text, username, user_no)
            ),
            None,
        )
        admin_store_rows = list(
            (
                await session.execute(
                    select(Store.id, Store.store_name, Store.store_no)
                    .join(Order, Order.store_id == Store.id)
                    .distinct()
                    .limit(500)
                )
            ).all()
        )
        admin_requested_store = next(
            (
                (store_id, store_name, store_no)
                for store_id, store_name, store_no in admin_store_rows
                if _query_mentions_identifier(query_text, store_name, store_no)
            ),
            None,
        )
        admin_qualified_customer = _qualified_scope_name(
            query_text, ("用户", "顾客", "客户", "账号")
        )
        admin_qualified_store = _qualified_scope_name(query_text, ("店铺", "商家"))
        admin_now = utc_now()
        admin_today_start = admin_now.replace(hour=0, minute=0, second=0, microsecond=0)
        admin_created_after: datetime | None = None
        admin_created_before: datetime | None = None
        if "今天" in compact_query or "今日" in compact_query:
            admin_created_after = admin_today_start
        elif "昨天" in compact_query or "昨日" in compact_query:
            admin_created_after = admin_today_start - timedelta(days=1)
            admin_created_before = admin_today_start
        elif any(marker in compact_query for marker in ("近30日", "近三十日", "最近30天")):
            admin_created_after = admin_today_start - timedelta(days=29)
        elif any(marker in compact_query for marker in ("近7日", "最近7天", "本周")):
            admin_created_after = admin_today_start - timedelta(days=6)

        admin_order_conditions: list[Any] = [Order.order_status.not_in(("cancelled", "closed"))]
        if admin_requested_statuses:
            admin_order_conditions.append(Order.order_status.in_(admin_requested_statuses))
        if admin_explicit_order_no:
            admin_order_conditions.append(Order.order_no == admin_explicit_order_no)
        if admin_requested_customer:
            admin_order_conditions.append(Order.user_id == admin_requested_customer[0])
        if admin_requested_store:
            admin_order_conditions.append(Order.store_id == admin_requested_store[0])
        if admin_created_after is not None:
            admin_order_conditions.append(Order.created_at >= admin_created_after)
        if admin_created_before is not None:
            admin_order_conditions.append(Order.created_at < admin_created_before)

        admin_order_rows = (
            []
            if (admin_qualified_customer and admin_requested_customer is None)
            or (admin_qualified_store and admin_requested_store is None)
            else (
                await session.execute(
                    select(Order, Store, User)
                    .join(Store, Store.id == Order.store_id)
                    .join(User, User.id == Order.user_id)
                    .where(*admin_order_conditions)
                    .order_by(Order.created_at.desc(), Order.id.desc())
                    .limit(12)
                )
            ).all()
        )
        admin_order_ids = [order.id for order, _store, _customer in admin_order_rows]
        admin_order_item_rows = (
            list(
                (
                    await session.scalars(
                        select(OrderItem)
                        .where(OrderItem.order_id.in_(admin_order_ids))
                        .order_by(OrderItem.order_id, OrderItem.id)
                    )
                ).all()
            )
            if admin_order_ids
            else []
        )
        admin_items_by_order: dict[int, list[OrderItem]] = {}
        for order_item in admin_order_item_rows:
            admin_items_by_order.setdefault(order_item.order_id, []).append(order_item)
        admin_image_keys = {
            item.image_object_key
            for item in admin_order_item_rows
            if item.image_object_key is not None
        }
        admin_image_keys.update(
            store.logo_object_key
            for _order, store, _customer in admin_order_rows
            if store.logo_object_key is not None
        )
        admin_image_files = (
            {
                file.object_key: file
                for file in (
                    await session.scalars(
                        select(FileObject).where(
                            FileObject.object_key.in_(admin_image_keys),
                            FileObject.file_status == "active",
                            FileObject.scan_status == "safe",
                        )
                    )
                ).all()
            }
            if admin_image_keys
            else {}
        )
        admin_recent_orders: list[dict[str, object]] = []
        for order, store, customer in admin_order_rows:
            admin_order_items = admin_items_by_order.get(order.id, [])
            admin_recent_orders.append(
                {
                    "order_id": order.order_no,
                    "display_order_id": f"{order.order_no[:8]}…{order.order_no[-4:]}",
                    "store": {
                        "store_id": store.store_no,
                        "store_name": store.store_name,
                        "logo_url": (
                            f"/api/v1/files/{admin_image_files[store.logo_object_key].file_no}"
                            if store.logo_object_key in admin_image_files
                            else None
                        ),
                    },
                    "customer": {
                        "user_id": customer.user_no,
                        "username": customer.username,
                    },
                    "store_id": store.store_no,
                    "store_name": store.store_name,
                    "customer_name": customer.username,
                    "status": order.order_status,
                    "payment_status": order.payment_status,
                    "fulfillment_status": order.fulfillment_status,
                    "after_sale_status": order.after_sale_status,
                    "has_pending_review": any(
                        item.review_status == "pending" for item in admin_order_items
                    ),
                    "items": [
                        {
                            "product_id": item.product_no,
                            "sku_id": item.sku_no,
                            "product_name": item.product_name,
                            "sku_name": item.sku_name,
                            "quantity": item.quantity,
                            "image_url": (
                                f"/api/v1/files/{admin_image_files[item.image_object_key].file_no}"
                                "?variant=thumbnail"
                                if item.image_object_key in admin_image_files
                                else None
                            ),
                        }
                        for item in admin_order_items
                    ],
                    "item_count": len(admin_order_items),
                    "total_quantity": sum(item.quantity for item in admin_order_items),
                    "amount": {
                        "minor_units": order.paid_amount,
                        "currency": order.currency,
                        "display": _money_display(order.paid_amount, order.currency),
                    },
                    "payable_amount": {
                        "minor_units": str(order.payable_amount),
                        "currency": order.currency,
                    },
                    "refunded_amount": {
                        "minor_units": order.refunded_amount,
                        "currency": order.currency,
                        "display": _money_display(order.refunded_amount, order.currency),
                    },
                    "version": int(order.version),
                    "created_at": order.created_at.isoformat(),
                }
            )
        return {
            "query_mode": (
                "detail" if admin_explicit_order_no and len(admin_recent_orders) <= 1 else "list"
            ),
            "applied_filters": {
                "statuses": sorted(admin_requested_statuses),
                "customer_name": (
                    admin_requested_customer[1] if admin_requested_customer else None
                ),
                "store_name": admin_requested_store[1] if admin_requested_store else None,
                "order_id": admin_explicit_order_no,
                "created_after": (admin_created_after.isoformat() if admin_created_after else None),
                "created_before": (
                    admin_created_before.isoformat() if admin_created_before else None
                ),
            },
            "order_status_counts": order_counts,
            "recent_orders": admin_recent_orders,
            "result_count": len(admin_recent_orders),
        }
    if tool_code == "governance.after_sale.timeline":
        compact_query = re.sub(r"\s+", "", query_text).casefold()
        explicit_refund_no = next(
            iter(re.findall(r"(?:ref|rfd)_[0-9a-z]+", compact_query, flags=re.IGNORECASE)),
            None,
        )
        explicit_appeal_no = next(
            iter(re.findall(r"rap_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
        )
        explicit_refund_payment_no = next(
            iter(re.findall(r"rfp_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
        )
        explicit_order_no = next(
            iter(re.findall(r"ord_[0-9a-z]+", compact_query, flags=re.IGNORECASE)), None
        )
        requested_refund_statuses: set[str] = set()
        for status, markers in {
            "submitted": ("待受理", "已提交"),
            "merchant_review": ("商家审核", "待商家处理"),
            "approved": ("已同意", "已批准"),
            "waiting_return": ("待退货",),
            "returning": ("退货中",),
            "received": ("已收到退货", "商家收货"),
            "refunding": ("退款中",),
            "succeeded": ("退款成功",),
            "rejected": ("已拒绝", "退款拒绝"),
            "cancelled": ("已取消",),
            "closed": ("已关闭",),
        }.items():
            if any(marker in compact_query for marker in markers):
                requested_refund_statuses.add(status)

        admin_refund_customer_rows = list(
            (
                await session.execute(
                    select(User.id, User.username, User.user_no)
                    .join(RefundApplication, RefundApplication.user_id == User.id)
                    .distinct()
                    .limit(500)
                )
            ).all()
        )
        admin_refund_customer = next(
            (
                (customer_id, username, user_no)
                for customer_id, username, user_no in admin_refund_customer_rows
                if _query_mentions_identifier(query_text, username, user_no)
            ),
            None,
        )
        admin_refund_store_rows = list(
            (
                await session.execute(
                    select(Store.id, Store.store_name, Store.store_no)
                    .join(RefundApplication, RefundApplication.store_id == Store.id)
                    .distinct()
                    .limit(500)
                )
            ).all()
        )
        admin_refund_store = next(
            (
                (store_id, store_name, store_no)
                for store_id, store_name, store_no in admin_refund_store_rows
                if _query_mentions_identifier(query_text, store_name, store_no)
            ),
            None,
        )
        qualified_customer = _qualified_scope_name(query_text, ("用户", "顾客", "客户", "账号"))
        qualified_store = _qualified_scope_name(query_text, ("店铺", "商家"))
        if (qualified_customer and admin_refund_customer is None) or (
            qualified_store and admin_refund_store is None
        ):
            return {
                "query_mode": "after_sale_list",
                "applied_filters": {
                    "customer_name": qualified_customer,
                    "store_name": qualified_store,
                },
                "refunds": [],
                "selected_refund": None,
                "result_count": 0,
                "exact_target_matched": False,
            }

        admin_refund_conditions: list[Any] = []
        if explicit_refund_no:
            admin_refund_conditions.append(RefundApplication.refund_no == explicit_refund_no)
        if explicit_order_no:
            admin_refund_conditions.append(Order.order_no == explicit_order_no)
        if admin_refund_customer:
            admin_refund_conditions.append(RefundApplication.user_id == admin_refund_customer[0])
        if admin_refund_store:
            admin_refund_conditions.append(RefundApplication.store_id == admin_refund_store[0])
        if requested_refund_statuses:
            admin_refund_conditions.append(
                RefundApplication.refund_status.in_(requested_refund_statuses)
            )
        admin_refund_statement = (
            select(RefundApplication, Order, Store, User)
            .join(Order, Order.id == RefundApplication.order_id)
            .join(Store, Store.id == RefundApplication.store_id)
            .join(User, User.id == RefundApplication.user_id)
        )
        if explicit_appeal_no:
            admin_refund_statement = admin_refund_statement.join(
                RefundAppeal, RefundAppeal.refund_id == RefundApplication.id
            )
            admin_refund_conditions.append(RefundAppeal.appeal_no == explicit_appeal_no)
        if explicit_refund_payment_no:
            admin_refund_statement = admin_refund_statement.join(
                RefundPaymentRecord,
                RefundPaymentRecord.refund_id == RefundApplication.id,
            )
            admin_refund_conditions.append(
                RefundPaymentRecord.refund_payment_no == explicit_refund_payment_no
            )
        admin_refund_rows = list(
            (
                await session.execute(
                    admin_refund_statement.where(*admin_refund_conditions)
                    .distinct()
                    .order_by(RefundApplication.submitted_at.desc(), RefundApplication.id.desc())
                    .limit(8)
                )
            ).all()
        )
        admin_refund_ids = [refund.id for refund, _order, _store, _user in admin_refund_rows]
        admin_refund_item_rows = (
            list(
                (
                    await session.execute(
                        select(RefundItem, OrderItem)
                        .join(OrderItem, OrderItem.id == RefundItem.order_item_id)
                        .where(RefundItem.refund_id.in_(admin_refund_ids))
                        .order_by(RefundItem.refund_id, RefundItem.id)
                    )
                ).all()
            )
            if admin_refund_ids
            else []
        )
        refund_event_rows = (
            list(
                (
                    await session.scalars(
                        select(RefundEvent)
                        .where(RefundEvent.refund_id.in_(admin_refund_ids))
                        .order_by(
                            RefundEvent.refund_id,
                            RefundEvent.created_at.desc(),
                            RefundEvent.id.desc(),
                        )
                    )
                ).all()
            )
            if admin_refund_ids
            else []
        )
        appeal_rows = (
            list(
                (
                    await session.scalars(
                        select(RefundAppeal)
                        .where(RefundAppeal.refund_id.in_(admin_refund_ids))
                        .order_by(RefundAppeal.created_at.desc(), RefundAppeal.id.desc())
                    )
                ).all()
            )
            if admin_refund_ids
            else []
        )
        appeal_ids = [appeal.id for appeal in appeal_rows]
        appeal_event_rows = (
            list(
                (
                    await session.scalars(
                        select(RefundAppealEvent)
                        .where(RefundAppealEvent.appeal_id.in_(appeal_ids))
                        .order_by(
                            RefundAppealEvent.appeal_id,
                            RefundAppealEvent.created_at.desc(),
                            RefundAppealEvent.id.desc(),
                        )
                    )
                ).all()
            )
            if appeal_ids
            else []
        )
        refund_payment_rows = (
            list(
                (
                    await session.scalars(
                        select(RefundPaymentRecord)
                        .where(RefundPaymentRecord.refund_id.in_(admin_refund_ids))
                        .order_by(RefundPaymentRecord.id.desc())
                    )
                ).all()
            )
            if admin_refund_ids
            else []
        )
        refund_payment_ids = [record.id for record in refund_payment_rows]
        refund_payment_event_rows = (
            list(
                (
                    await session.scalars(
                        select(RefundPaymentEvent)
                        .where(RefundPaymentEvent.refund_payment_id.in_(refund_payment_ids))
                        .order_by(
                            RefundPaymentEvent.refund_payment_id,
                            RefundPaymentEvent.created_at.desc(),
                            RefundPaymentEvent.id.desc(),
                        )
                    )
                ).all()
            )
            if refund_payment_ids
            else []
        )
        return_shipments = (
            list(
                (
                    await session.scalars(
                        select(RefundShipment).where(RefundShipment.refund_id.in_(admin_refund_ids))
                    )
                ).all()
            )
            if admin_refund_ids
            else []
        )

        admin_items_by_refund: dict[int, list[tuple[RefundItem, OrderItem]]] = {}
        admin_refund_image_keys: set[str] = set()
        for refund_item, order_item in admin_refund_item_rows:
            admin_items_by_refund.setdefault(refund_item.refund_id, []).append(
                (refund_item, order_item)
            )
            if order_item.image_object_key:
                admin_refund_image_keys.add(order_item.image_object_key)
        admin_refund_image_files = (
            {
                file.object_key: file
                for file in (
                    await session.scalars(
                        select(FileObject).where(
                            FileObject.object_key.in_(admin_refund_image_keys),
                            FileObject.file_status == "active",
                            FileObject.scan_status == "safe",
                        )
                    )
                ).all()
            }
            if admin_refund_image_keys
            else {}
        )
        events_by_refund: dict[int, list[RefundEvent]] = {}
        for refund_event in refund_event_rows:
            events_by_refund.setdefault(refund_event.refund_id, []).append(refund_event)
        appeals_by_refund: dict[int, list[RefundAppeal]] = {}
        for appeal in appeal_rows:
            appeals_by_refund.setdefault(appeal.refund_id, []).append(appeal)
        appeal_events_by_appeal: dict[int, list[RefundAppealEvent]] = {}
        for appeal_event in appeal_event_rows:
            appeal_events_by_appeal.setdefault(appeal_event.appeal_id, []).append(appeal_event)
        payments_by_refund: dict[int, list[RefundPaymentRecord]] = {}
        for record in refund_payment_rows:
            payments_by_refund.setdefault(record.refund_id, []).append(record)
        payment_events_by_record: dict[int, list[RefundPaymentEvent]] = {}
        for refund_payment_event in refund_payment_event_rows:
            payment_events_by_record.setdefault(refund_payment_event.refund_payment_id, []).append(
                refund_payment_event
            )
        shipments_by_refund = {shipment.refund_id: shipment for shipment in return_shipments}

        admin_refund_values: list[dict[str, object]] = []
        for refund, order, store, customer in admin_refund_rows:
            return_shipment = shipments_by_refund.get(refund.id)
            admin_refund_values.append(
                {
                    "refund_id": refund.refund_no,
                    "order_id": order.order_no,
                    "store_id": store.store_no,
                    "store_name": store.store_name,
                    "customer_id": customer.user_no,
                    "customer_name": customer.username,
                    "status": refund.refund_status,
                    "refund_type": refund.refund_type,
                    "reason_code": refund.reason_code,
                    "reason_detail": refund.reason_detail,
                    "requested_amount": {
                        "minor_units": refund.requested_amount,
                        "currency": refund.currency,
                        "display": _money_display(refund.requested_amount, refund.currency),
                    },
                    "approved_amount": {
                        "minor_units": refund.approved_amount,
                        "currency": refund.currency,
                        "display": _money_display(refund.approved_amount, refund.currency),
                    },
                    "submitted_at": refund.submitted_at.isoformat(),
                    "decided_at": refund.decided_at.isoformat() if refund.decided_at else None,
                    "version": int(refund.version),
                    "items": [
                        {
                            "order_item_id": order_item.order_item_no,
                            "product_id": order_item.product_no,
                            "product_name": order_item.product_name,
                            "sku_name": order_item.sku_name,
                            "quantity": refund_item.quantity,
                            "requested_amount": {
                                "minor_units": refund_item.requested_amount,
                                "currency": refund.currency,
                                "display": _money_display(
                                    refund_item.requested_amount, refund.currency
                                ),
                            },
                            "succeeded_amount": {
                                "minor_units": refund_item.succeeded_amount,
                                "currency": refund.currency,
                                "display": _money_display(
                                    refund_item.succeeded_amount, refund.currency
                                ),
                            },
                            "image_url": (
                                f"/api/v1/files/{admin_refund_image_files[order_item.image_object_key].file_no}"
                                "?variant=thumbnail"
                                if order_item.image_object_key in admin_refund_image_files
                                else None
                            ),
                        }
                        for refund_item, order_item in admin_items_by_refund.get(refund.id, [])
                    ],
                    "events": [
                        {
                            "event_id": event.event_no,
                            "from_status": event.from_status,
                            "to_status": event.to_status,
                            "event_code": event.event_code,
                            "actor_type": event.actor_type,
                            "reason": event.reason,
                            "occurred_at": event.created_at.isoformat(),
                        }
                        for event in events_by_refund.get(refund.id, [])[:20]
                    ],
                    "return_shipment": (
                        {
                            "carrier_code": return_shipment.carrier_code,
                            "carrier_name": return_shipment.carrier_name,
                            "tracking_no_masked": return_shipment.tracking_no_masked,
                            "status": return_shipment.shipment_status,
                            "shipped_at": (
                                return_shipment.shipped_at.isoformat()
                                if return_shipment.shipped_at
                                else None
                            ),
                            "delivered_at": (
                                return_shipment.delivered_at.isoformat()
                                if return_shipment.delivered_at
                                else None
                            ),
                            "received_at": (
                                return_shipment.received_at.isoformat()
                                if return_shipment.received_at
                                else None
                            ),
                            "version": int(return_shipment.version),
                        }
                        if return_shipment
                        else None
                    ),
                    "refund_payments": [
                        {
                            "refund_payment_id": record.refund_payment_no,
                            "provider": record.provider,
                            "provider_refund_no_masked": _mask_business_reference(
                                record.provider_refund_no
                            ),
                            "status": record.payment_status,
                            "amount": {
                                "minor_units": record.amount,
                                "currency": record.currency,
                                "display": _money_display(record.amount, record.currency),
                            },
                            "completed_at": (
                                record.completed_at.isoformat() if record.completed_at else None
                            ),
                            "version": int(record.version),
                            "events": [
                                {
                                    "event_id": event.event_no,
                                    "provider_status": event.provider_status,
                                    "amount": {
                                        "minor_units": event.amount,
                                        "currency": event.currency,
                                        "display": _money_display(event.amount, event.currency),
                                    },
                                    "signature_valid": bool(event.signature_valid),
                                    "occurred_at": event.created_at.isoformat(),
                                }
                                for event in payment_events_by_record.get(record.id, [])[:12]
                            ],
                        }
                        for record in payments_by_refund.get(refund.id, [])
                    ],
                    "appeals": [
                        {
                            "appeal_id": appeal.appeal_no,
                            "status": appeal.appeal_status,
                            "reason": appeal.reason,
                            "reason_code": appeal.reason_code,
                            "resolution_code": appeal.resolution_code,
                            "resolution_detail": appeal.resolution_detail,
                            "submitted_at": appeal.created_at.isoformat(),
                            "decided_at": (
                                appeal.decided_at.isoformat() if appeal.decided_at else None
                            ),
                            "version": int(appeal.version),
                            "events": [
                                {
                                    "event_id": event.event_no,
                                    "event_type": event.event_type,
                                    "from_status": event.from_status,
                                    "to_status": event.to_status,
                                    "actor_type": event.actor_type,
                                    "reason_code": event.reason_code,
                                    "remark": event.remark,
                                    "occurred_at": event.created_at.isoformat(),
                                }
                                for event in appeal_events_by_appeal.get(appeal.id, [])[:12]
                            ],
                        }
                        for appeal in appeals_by_refund.get(refund.id, [])
                    ],
                }
            )
        exact_target = bool(
            explicit_refund_no
            or explicit_appeal_no
            or explicit_refund_payment_no
            or explicit_order_no
        )
        selected_refund = admin_refund_values[0] if len(admin_refund_values) == 1 else None
        return {
            "query_mode": "after_sale_detail" if selected_refund else "after_sale_list",
            "applied_filters": {
                "refund_id": explicit_refund_no,
                "appeal_id": explicit_appeal_no,
                "refund_payment_id": explicit_refund_payment_no,
                "order_id": explicit_order_no,
                "customer_name": (admin_refund_customer[1] if admin_refund_customer else None),
                "store_name": admin_refund_store[1] if admin_refund_store else None,
                "statuses": sorted(requested_refund_statuses),
            },
            "refunds": admin_refund_values,
            "selected_refund": selected_refund,
            "result_count": len(admin_refund_values),
            "exact_target_matched": exact_target and selected_refund is not None,
        }
    if tool_code == "governance.after_sale_summary":
        summary_refund_rows = (
            await session.execute(
                select(RefundApplication, Store, User, Order)
                .join(Store, Store.id == RefundApplication.store_id)
                .join(User, User.id == RefundApplication.user_id)
                .join(Order, Order.id == RefundApplication.order_id)
                .order_by(RefundApplication.submitted_at.desc(), RefundApplication.id.desc())
                .limit(8)
            )
        ).all()
        return {
            "refund_status_counts": await _counts(session, RefundApplication.refund_status),
            "recent_refunds": [
                {
                    "refund_id": refund.refund_no,
                    "order_id": order.order_no,
                    "store_name": store.store_name,
                    "customer_name": customer.username,
                    "status": refund.refund_status,
                    "refund_type": refund.refund_type,
                    "reason_code": refund.reason_code,
                    "reason_detail": refund.reason_detail,
                    "amount": {
                        "minor_units": refund.requested_amount,
                        "currency": refund.currency,
                        "display": _money_display(refund.requested_amount, refund.currency),
                    },
                    "submitted_at": refund.submitted_at.isoformat(),
                    "version": int(refund.version),
                }
                for refund, store, customer, order in summary_refund_rows
            ],
        }
    if tool_code == "governance.support_summary":
        ticket_rows = (
            await session.execute(
                select(HumanServiceTicket, User, Store, Conversation)
                .join(User, User.id == HumanServiceTicket.user_id)
                .outerjoin(Store, Store.id == HumanServiceTicket.store_id)
                .join(Conversation, Conversation.id == HumanServiceTicket.conversation_id)
                .where(
                    HumanServiceTicket.ticket_status.in_(
                        ("queued", "assigned", "active", "waiting_user")
                    )
                )
                .order_by(
                    case(
                        (HumanServiceTicket.priority == "urgent", 0),
                        (HumanServiceTicket.priority == "high", 1),
                        (HumanServiceTicket.priority == "normal", 2),
                        else_=3,
                    ),
                    HumanServiceTicket.created_at,
                    HumanServiceTicket.id,
                )
                .limit(12)
            )
        ).all()
        active_tickets = [
            {
                "ticket_id": ticket.ticket_no,
                "conversation_id": conversation.conversation_no,
                "conversation_type": conversation.conversation_type,
                "customer_id": customer.user_no,
                "customer_name": customer.username,
                "store_id": store.store_no if store else None,
                "store_name": store.store_name if store else None,
                "queue_type": ticket.queue_type,
                "queue_code": ticket.queue_code,
                "ticket_type": ticket.ticket_type,
                "priority": ticket.priority,
                "status": ticket.ticket_status,
                "summary": ticket.handoff_summary,
                "sla_due_at": ticket.sla_due_at.isoformat() if ticket.sla_due_at else None,
                "waiting_since": (
                    ticket.waiting_started_at.isoformat() if ticket.waiting_started_at else None
                ),
                "version": int(ticket.version),
            }
            for ticket, customer, store, conversation in ticket_rows
        ]
        selected_ticket: dict[str, object] | None = None
        for (ticket, customer, store, conversation), ticket_data in zip(
            ticket_rows, active_tickets, strict=True
        ):
            store_match = bool(
                store and _query_mentions_identifier(query_text, store.store_name, store.store_no)
            )
            if not (
                _query_mentions_identifier(query_text, customer.username, customer.user_no)
                or _query_mentions_identifier(query_text, ticket.ticket_no, ticket.ticket_no)
                or _query_mentions_identifier(
                    query_text, conversation.conversation_no, conversation.conversation_no
                )
                or store_match
            ):
                continue
            message_rows = list(
                (
                    await session.scalars(
                        select(Message)
                        .where(
                            Message.conversation_id == conversation.id,
                            Message.message_status == "sent",
                        )
                        .order_by(Message.sequence_no.desc())
                        .limit(30)
                    )
                ).all()
            )
            context_rows = list(
                (
                    await session.scalars(
                        select(ConversationContext)
                        .where(
                            ConversationContext.conversation_id == conversation.id,
                            ConversationContext.context_status == "active",
                        )
                        .order_by(ConversationContext.created_at.desc())
                        .limit(12)
                    )
                ).all()
            )
            selected_ticket = {
                **ticket_data,
                "recent_messages": [
                    {
                        "message_id": message.message_no,
                        "sequence": int(message.sequence_no),
                        "sender": message.sender_type,
                        "type": message.message_type,
                        "text": (message.text_content or "")[:800] or None,
                        "card": message.content_payload,
                        "sent_at": message.sent_at.isoformat(),
                    }
                    for message in reversed(message_rows)
                ],
                "active_contexts": [
                    {
                        "type": item.context_type,
                        "resource_id": item.resource_no,
                        "resource_version": item.resource_version,
                        "display": item.display_snapshot,
                    }
                    for item in context_rows
                ],
            }
            break
        return {
            "ticket_status_counts": await _counts(session, HumanServiceTicket.ticket_status),
            "ticket_queue_counts": await _counts(session, HumanServiceTicket.queue_type),
            "active_tickets": active_tickets,
            "selected_ticket": selected_ticket,
            "query_mode": "ticket_detail" if selected_ticket is not None else "ticket_queue",
        }
    if tool_code == "governance.ai.evaluations.list":
        compact_query = re.sub(r"\s+", "", query_text).casefold()
        evaluation_match = re.search(r"evr_[0-9a-z]+", query_text, flags=re.IGNORECASE)
        evaluation_no = evaluation_match.group(0) if evaluation_match is not None else None
        status_filter = next(
            (
                code
                for code, markers in (
                    ("failed", ("失败",)),
                    ("running", ("运行中", "采集中")),
                    ("queued", ("排队", "待运行")),
                    ("completed", ("已完成", "完成的")),
                )
                if any(marker in compact_query for marker in markers)
            ),
            None,
        )
        statement = select(AiEvaluationRun).order_by(
            AiEvaluationRun.created_at.desc(), AiEvaluationRun.id.desc()
        )
        if evaluation_no is not None:
            statement = statement.where(AiEvaluationRun.evaluation_run_no == evaluation_no)
        if status_filter is not None:
            statement = statement.where(AiEvaluationRun.run_status == status_filter)
        rows = list((await session.scalars(statement.limit(30))).all())
        evaluations: list[dict[str, object]] = []
        for row in rows:
            report = row.report if isinstance(row.report, Mapping) else {}
            raw_metrics = report.get("metrics")
            metrics: Mapping[str, object] = raw_metrics if isinstance(raw_metrics, Mapping) else {}
            raw_reasons = report.get("reasons")
            reasons = (
                [str(item) for item in raw_reasons[:12]] if isinstance(raw_reasons, list) else []
            )
            evaluations.append(
                {
                    "evaluation_id": row.evaluation_run_no,
                    "dataset_id": row.dataset_id,
                    "dataset_version": row.dataset_version,
                    "dataset_sha256": row.dataset_hash.hex(),
                    "baseline": {
                        "type": row.baseline_type,
                        "version": row.baseline_version,
                    },
                    "candidate": {
                        "type": row.candidate_type,
                        "version": row.candidate_version,
                    },
                    "require_significant_gain": bool(row.require_significant_gain),
                    "status": row.run_status,
                    "release_gate": row.release_gate,
                    "metrics": dict(metrics),
                    "reasons": reasons,
                    "trace_id": row.trace_id,
                    "error_code": row.error_code,
                    "created_at": row.created_at.isoformat(),
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                }
            )
        return {
            "requested_governance_asset": "evaluations",
            "query_mode": "evaluation_detail" if evaluation_no is not None else "evaluation_list",
            "applied_filters": {"evaluation_id": evaluation_no, "status": status_filter},
            "result_count": len(evaluations),
            "exact_target_matched": bool(evaluation_no is not None and evaluations),
            "evaluation_status_counts": await _counts(session, AiEvaluationRun.run_status),
            "release_gate_counts": await _counts(session, AiEvaluationRun.release_gate),
            "active_dataset": {
                "dataset_id": DATASET_MANIFEST.dataset_id,
                "dataset_version": DATASET_VERSION,
                "dataset_sha256": DATASET_MANIFEST.sha256,
                "case_count": DATASET_CASE_COUNT,
            },
            "registered_comparison": {
                "baseline_type": BASELINE_TYPE,
                "baseline_version": BASELINE_VERSION,
                "candidate_type": CANDIDATE_TYPE,
                "candidate_version": CANDIDATE_VERSION,
            },
            "evaluations": evaluations,
        }
    if tool_code in {
        "governance.knowledge.documents.list",
    }:
        compact_query = re.sub(r"\s+", "", query_text).casefold()
        explicit_document_match = re.search(r"kdoc_[0-9a-z]+", query_text, flags=re.IGNORECASE)
        explicit_document_no = (
            explicit_document_match.group(0) if explicit_document_match is not None else None
        )
        status_filter = next(
            (
                code
                for code, markers in (
                    ("withdrawn", ("已撤回", "撤回的")),
                    ("published", ("已发布", "发布的")),
                    ("draft", ("草稿", "未发布")),
                )
                if any(marker in compact_query for marker in markers)
            ),
            None,
        )
        document_statement = select(KnowledgeDocument).order_by(
            KnowledgeDocument.created_at.desc(), KnowledgeDocument.id.desc()
        )
        if explicit_document_no is not None:
            document_statement = document_statement.where(
                KnowledgeDocument.document_no == explicit_document_no
            )
        if status_filter is not None:
            document_statement = document_statement.where(
                KnowledgeDocument.document_status == status_filter
            )
        candidate_documents = list((await session.scalars(document_statement.limit(100))).all())
        title_matches = [
            item
            for item in candidate_documents
            if _query_mentions_identifier(query_text, item.title, item.document_no)
        ]
        selected_documents = (
            title_matches
            if title_matches
            else candidate_documents
            if explicit_document_no is None
            else []
        )
        selected_documents = selected_documents[:20]
        store_nos = {item.scope_no for item in selected_documents if item.scope_type == "store"}
        store_names = (
            {
                store.store_no: store.store_name
                for store in list(
                    (
                        await session.scalars(select(Store).where(Store.store_no.in_(store_nos)))
                    ).all()
                )
            }
            if store_nos
            else {}
        )
        job_rows = list(
            (
                await session.scalars(
                    select(AdminBatchJob)
                    .where(AdminBatchJob.job_type == "knowledge_index")
                    .order_by(AdminBatchJob.created_at.desc(), AdminBatchJob.id.desc())
                    .limit(300)
                )
            ).all()
        )
        jobs_by_document: dict[str, list[dict[str, object]]] = {}
        for job in job_rows:
            request_config = job.request_config if isinstance(job.request_config, Mapping) else {}
            document_no = str(request_config.get("document_no") or "")
            if not document_no:
                continue
            jobs_by_document.setdefault(document_no, []).append(
                {
                    "job_id": job.job_no,
                    "execution_job_id": job.execution_job_no,
                    "status": job.job_status,
                    "total_count": int(job.total_count),
                    "success_count": int(job.success_count),
                    "failure_count": int(job.failure_count),
                    "error_code": job.error_code,
                    "error_summary": job.error_summary,
                    "embedding_model": request_config.get("embedding_model_code"),
                    "content_version": request_config.get("content_version"),
                    "trace_id": job.trace_id,
                    "created_at": job.created_at.isoformat(),
                    "started_at": job.started_at.isoformat() if job.started_at else None,
                    "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                }
            )
        documents = [
            {
                "document_id": item.document_no,
                "title": item.title,
                "scope_type": item.scope_type,
                "scope_id": item.scope_no,
                "scope_name": (
                    "平台"
                    if item.scope_type == "platform"
                    else store_names.get(item.scope_no, "店铺已删除或不可用")
                ),
                "status": item.document_status,
                "content_version": item.content_version,
                "character_count": len(item.safe_text or ""),
                "resource_version": int(item.version),
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
                "latest_index_job": (
                    jobs_by_document[item.document_no][0]
                    if jobs_by_document.get(item.document_no)
                    else None
                ),
                "index_jobs": (jobs_by_document.get(item.document_no) or [])[:10],
            }
            for item in selected_documents
        ]
        return {
            "requested_governance_asset": "knowledge_documents",
            "query_mode": (
                "knowledge_document_detail"
                if explicit_document_no is not None or len(title_matches) == 1
                else "knowledge_document_list"
            ),
            "applied_filters": {
                "document_id": explicit_document_no,
                "status": status_filter,
                "title_match": title_matches[0].title if len(title_matches) == 1 else None,
            },
            "result_count": len(documents),
            "exact_target_matched": bool(explicit_document_no is not None and selected_documents),
            "document_status_counts": await _counts(session, KnowledgeDocument.document_status),
            "index_job_status_counts": await _counts(
                session, AdminBatchJob.job_status, AdminBatchJob.job_type == "knowledge_index"
            ),
            "documents": documents,
        }
    if tool_code in {
        "governance.ai_summary",
        "governance.ai.agents.list",
        "governance.ai.skills.list",
        "governance.ai.tools.list",
    }:
        agent_rows = (
            await session.execute(
                select(AgentDefinition, AgentVersion)
                .outerjoin(
                    AgentVersion,
                    (AgentVersion.agent_id == AgentDefinition.id)
                    & (AgentVersion.version_status == "published"),
                )
                .order_by(AgentDefinition.display_name, AgentVersion.version_no.desc())
            )
        ).all()
        latest_agents: dict[int, dict[str, object]] = {}
        for definition, version in agent_rows:
            if definition.id in latest_agents:
                continue
            latest_agents[definition.id] = {
                "agent_id": definition.agent_no,
                "agent_code": definition.agent_code,
                "display_name": definition.display_name,
                "agent_type": definition.agent_type,
                "scope_type": definition.scope_type,
                "status": definition.agent_status,
                "published_version": version.version_no if version else None,
                "model_profile": version.model_profile if version else None,
                "tool_count": len(version.tool_allowlist or []) if version else 0,
                "version_status": version.version_status if version else "unpublished",
            }
        recent_runs = list(
            (
                await session.execute(
                    select(AgentRun, AgentDefinition)
                    .join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)
                    .join(AgentDefinition, AgentDefinition.id == AgentVersion.agent_id)
                    .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                    .limit(10)
                )
            ).all()
        )
        agent_versions = [
            version.id
            for definition, version in agent_rows
            if version is not None and definition.id in latest_agents
        ]
        skill_binding_rows = (
            list(
                (
                    await session.execute(
                        select(AgentSkillBinding, SkillVersion, SkillDefinition)
                        .join(SkillVersion, SkillVersion.id == AgentSkillBinding.skill_version_id)
                        .join(SkillDefinition, SkillDefinition.id == SkillVersion.skill_id)
                        .where(
                            AgentSkillBinding.agent_version_id.in_(agent_versions),
                            AgentSkillBinding.binding_status == "active",
                        )
                    )
                ).all()
            )
            if agent_versions
            else []
        )
        skills_by_agent_version: dict[int, list[dict[str, object]]] = {}
        for binding, version, definition in skill_binding_rows:
            skills_by_agent_version.setdefault(binding.agent_version_id, []).append(
                {
                    "skill_code": definition.skill_code,
                    "display_name": definition.display_name,
                    "version": version.version_no,
                    "status": version.version_status,
                }
            )
        for definition, version in agent_rows:
            latest_agent_item = latest_agents.get(definition.id)
            if latest_agent_item is not None and version is not None:
                latest_agent_item["skills"] = skills_by_agent_version.get(version.id, [])

        skill_rows = list(
            (
                await session.execute(
                    select(SkillDefinition, SkillVersion)
                    .outerjoin(
                        SkillVersion,
                        (SkillVersion.skill_id == SkillDefinition.id)
                        & (SkillVersion.version_status == "published"),
                    )
                    .order_by(SkillDefinition.display_name, SkillVersion.version_no.desc())
                )
            ).all()
        )
        latest_skills: dict[int, dict[str, object]] = {}
        for definition, version in skill_rows:
            if definition.id in latest_skills:
                continue
            latest_skills[definition.id] = {
                "skill_code": definition.skill_code,
                "display_name": definition.display_name,
                "status": definition.skill_status,
                "published_version": version.version_no if version else None,
                "version_status": version.version_status if version else "unpublished",
                "evaluation_report": version.evaluation_report if version else {},
                "tool_bindings": [],
            }
        published_skill_versions = {
            version.id: definition.id
            for definition, version in skill_rows
            if version is not None and definition.id in latest_skills
        }
        if published_skill_versions:
            tool_bindings = list(
                (
                    await session.execute(
                        select(SkillToolBinding, ToolVersion, ToolDefinition)
                        .join(ToolVersion, ToolVersion.id == SkillToolBinding.tool_version_id)
                        .join(ToolDefinition, ToolDefinition.id == ToolVersion.tool_id)
                        .where(
                            SkillToolBinding.skill_version_id.in_(published_skill_versions),
                            SkillToolBinding.permission_effect == "allow",
                        )
                        .order_by(ToolDefinition.tool_code)
                    )
                ).all()
            )
            for binding, version, definition in tool_bindings:
                skill_id = published_skill_versions[binding.skill_version_id]
                bindings = latest_skills[skill_id]["tool_bindings"]
                assert isinstance(bindings, list)
                bindings.append(
                    {
                        "tool_code": definition.tool_code,
                        "tool_version": version.version_no,
                        "confirmation_policy": binding.confirmation_policy,
                        "call_budget": binding.call_budget,
                        "timeout_ms": binding.timeout_ms,
                    }
                )

        tool_rows = list(
            (
                await session.execute(
                    select(ToolDefinition, ToolVersion)
                    .outerjoin(
                        ToolVersion,
                        (ToolVersion.tool_id == ToolDefinition.id)
                        & (ToolVersion.version_status == "published"),
                    )
                    .order_by(ToolDefinition.tool_code, ToolVersion.version_no.desc())
                )
            ).all()
        )
        latest_tools: dict[int, dict[str, object]] = {}
        for definition, version in tool_rows:
            if definition.id in latest_tools:
                continue
            latest_tools[definition.id] = {
                "tool_code": definition.tool_code,
                "server_code": definition.server_code,
                "risk_level": definition.risk_level,
                "status": definition.tool_status,
                "published_version": version.version_no if version else None,
                "version_status": version.version_status if version else "unpublished",
                "input_schema_fields": (
                    sorted(str(key) for key in version.input_schema.get("properties", {}))
                    if version and isinstance(version.input_schema, Mapping)
                    else []
                ),
                "evaluation_report": version.evaluation_report if version else {},
            }

        result: dict[str, object] = {
            "agent_status_counts": await _counts(session, AgentDefinition.agent_status),
            "agent_run_status_counts": await _counts(session, AgentRun.run_status),
            "skill_status_counts": await _counts(session, SkillDefinition.skill_status),
            "skill_version_status_counts": await _counts(session, SkillVersion.version_status),
            "tool_status_counts": await _counts(session, ToolDefinition.tool_status),
            "tool_version_status_counts": await _counts(session, ToolVersion.version_status),
            "knowledge_document_status_counts": await _counts(
                session, KnowledgeDocument.document_status
            ),
            "agents": list(latest_agents.values()),
            "recent_runs": [
                {
                    "run_id": run.run_no,
                    "agent_name": definition.display_name,
                    "status": run.run_status,
                    "phase": run.current_phase,
                    "error_code": run.error_code,
                    "degraded_reason": run.degraded_reason,
                    "trace_id": run.trace_id,
                    "created_at": run.created_at.isoformat(),
                }
                for run, definition in recent_runs
            ],
        }
        if tool_code == "governance.ai.agents.list":
            result["requested_governance_asset"] = "agents"
        elif tool_code == "governance.ai.skills.list":
            result["requested_governance_asset"] = "skills"
            result["skills"] = list(latest_skills.values())[:30]
        elif tool_code == "governance.ai.tools.list":
            result["requested_governance_asset"] = "tools"
            result["tools"] = list(latest_tools.values())[:50]
        else:
            result["skills"] = list(latest_skills.values())[:12]
            result["tools"] = list(latest_tools.values())[:20]
        return result
    if tool_code == "governance.metrics.query":
        days = _requested_metrics_days(query_text)
        window_start = utc_now() - timedelta(days=days)
        paid_order_amount = int(
            await session.scalar(
                select(func.coalesce(func.sum(Order.paid_amount), 0)).where(
                    Order.created_at >= window_start,
                    Order.payment_status == "paid",
                )
            )
            or 0
        )
        net_completed_amount = int(
            await session.scalar(
                select(func.coalesce(func.sum(Order.paid_amount - Order.refunded_amount), 0)).where(
                    Order.created_at >= window_start,
                    Order.order_status == "completed",
                )
            )
            or 0
        )
        refunded_amount = int(
            await session.scalar(
                select(func.coalesce(func.sum(Order.refunded_amount), 0)).where(
                    Order.created_at >= window_start
                )
            )
            or 0
        )
        return {
            "metric_window": {
                "days": days,
                "started_at": window_start.isoformat(),
                "ended_at": utc_now().isoformat(),
                "timezone": "Asia/Shanghai",
            },
            "business_metrics": {
                "created_order_count": int(
                    await session.scalar(
                        select(func.count(Order.id)).where(Order.created_at >= window_start)
                    )
                    or 0
                ),
                "paid_order_amount": {
                    "minor_units": paid_order_amount,
                    "currency": "CNY",
                    "display": _money_display(paid_order_amount, "CNY"),
                    "basis": "窗口内创建且已支付订单的实付金额",
                },
                "completed_net_amount": {
                    "minor_units": net_completed_amount,
                    "currency": "CNY",
                    "display": _money_display(net_completed_amount, "CNY"),
                    "basis": "窗口内创建且已完成订单的实付减退款",
                },
                "refunded_amount": {
                    "minor_units": refunded_amount,
                    "currency": "CNY",
                    "display": _money_display(refunded_amount, "CNY"),
                },
                "new_user_count": int(
                    await session.scalar(
                        select(func.count(User.id)).where(User.registered_at >= window_start)
                    )
                    or 0
                ),
                "new_store_count": int(
                    await session.scalar(
                        select(func.count(Store.id)).where(Store.opened_at >= window_start)
                    )
                    or 0
                ),
                "new_product_count": int(
                    await session.scalar(
                        select(func.count(Product.id)).where(
                            Product.published_at >= window_start,
                            Product.deleted_at.is_(None),
                        )
                    )
                    or 0
                ),
            },
        }
    if tool_code in {"observability.traces.search", "observability.traces.get"}:
        identifier_match = re.search(r"(?:run|trc)_[0-9a-z]+", query_text, flags=re.IGNORECASE)
        identifier = identifier_match.group(0) if identifier_match is not None else None
        run_statement = (
            select(AgentRun, AgentDefinition)
            .join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)
            .join(AgentDefinition, AgentDefinition.id == AgentVersion.agent_id)
        )
        if identifier is not None:
            run_statement = run_statement.where(
                (AgentRun.run_no == identifier) | (AgentRun.trace_id == identifier)
            )
        failed_only = any(
            marker in re.sub(r"\s+", "", query_text).casefold()
            for marker in (
                "失败的agent运行",
                "失败agent运行",
                "失败运行",
                "最近失败",
            )
        )
        if failed_only and identifier is None:
            run_statement = run_statement.where(AgentRun.run_status == "failed")
        run_rows = list(
            (
                await session.execute(
                    run_statement.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(12)
                )
            ).all()
        )
        run_ids = [run.id for run, _definition in run_rows]
        delegation_rows = (
            list(
                (
                    await session.scalars(
                        select(AgentDelegation)
                        .where(AgentDelegation.run_id.in_(run_ids))
                        .order_by(AgentDelegation.run_id, AgentDelegation.id)
                    )
                ).all()
            )
            if run_ids
            else []
        )
        audit_rows = (
            list(
                (
                    await session.scalars(
                        select(AgentToolAudit)
                        .where(AgentToolAudit.run_id.in_(run_ids))
                        .order_by(AgentToolAudit.run_id, AgentToolAudit.id)
                    )
                ).all()
            )
            if run_ids
            else []
        )
        delegations_by_run: dict[int, list[AgentDelegation]] = {}
        for delegation in delegation_rows:
            delegations_by_run.setdefault(delegation.run_id, []).append(delegation)
        audits_by_run: dict[int, list[AgentToolAudit]] = {}
        for audit in audit_rows:
            audits_by_run.setdefault(audit.run_id, []).append(audit)
        return {
            "query_identifier": identifier,
            "runs": [
                {
                    "run_id": run.run_no,
                    "trace_id": run.trace_id,
                    "agent_name": definition.display_name,
                    "status": run.run_status,
                    "phase": run.current_phase,
                    "error_code": run.error_code,
                    "degraded_reason": run.degraded_reason,
                    "created_at": run.created_at.isoformat(),
                    "delegations": [
                        {
                            "delegation_id": item.delegation_no,
                            "specialist": item.specialist_code,
                            "status": item.delegation_status,
                            "tool_calls": item.tool_calls,
                            "model_calls": item.model_calls,
                            "tokens_used": item.tokens_used,
                            "error_code": item.error_code,
                        }
                        for item in delegations_by_run.get(run.id, [])
                    ],
                    "tool_calls": [
                        {
                            "tool_code": item.tool_code,
                            "outcome": item.outcome,
                            "latency_ms": item.latency_ms,
                            "error_code": item.error_code,
                        }
                        for item in audits_by_run.get(run.id, [])
                    ],
                }
                for run, definition in run_rows
            ],
            "matched_count": len(run_rows),
            "status_filter": "failed" if failed_only else None,
        }
    if tool_code == "observability.cost_metrics":
        days = _requested_metrics_days(query_text)
        window_start = utc_now() - timedelta(days=days)
        run_count = int(
            await session.scalar(
                select(func.count(AgentRun.id)).where(AgentRun.created_at >= window_start)
            )
            or 0
        )
        completed_count = int(
            await session.scalar(
                select(func.count(AgentRun.id)).where(
                    AgentRun.created_at >= window_start,
                    AgentRun.run_status == "completed",
                )
            )
            or 0
        )
        audit_count = int(
            await session.scalar(
                select(func.count(AgentToolAudit.id))
                .join(AgentRun, AgentRun.id == AgentToolAudit.run_id)
                .where(AgentRun.created_at >= window_start)
            )
            or 0
        )
        successful_audits = int(
            await session.scalar(
                select(func.count(AgentToolAudit.id))
                .join(AgentRun, AgentRun.id == AgentToolAudit.run_id)
                .where(
                    AgentRun.created_at >= window_start,
                    AgentToolAudit.outcome == "succeeded",
                )
            )
            or 0
        )
        average_latency = int(
            await session.scalar(
                select(func.coalesce(func.avg(AgentToolAudit.latency_ms), 0))
                .join(AgentRun, AgentRun.id == AgentToolAudit.run_id)
                .where(AgentRun.created_at >= window_start)
            )
            or 0
        )
        recorded_tokens = int(
            await session.scalar(
                select(func.coalesce(func.sum(AgentDelegation.tokens_used), 0))
                .join(AgentRun, AgentRun.id == AgentDelegation.run_id)
                .where(AgentRun.created_at >= window_start)
            )
            or 0
        )
        return {
            "metric_window": {"days": days, "started_at": window_start.isoformat()},
            "agent_run_count": run_count,
            "completed_run_count": completed_count,
            "agent_success_rate": round(completed_count / run_count, 4) if run_count else None,
            "tool_call_count": audit_count,
            "tool_success_rate": (
                round(successful_audits / audit_count, 4) if audit_count else None
            ),
            "average_tool_latency_ms": average_latency,
            "recorded_delegation_tokens": recorded_tokens,
            "token_cost_coverage": "partial",
            "cost_amount": None,
            "cost_status": "provider_usage_and_pricing_not_fully_recorded",
        }
    pending_outbox = int(
        await session.scalar(
            select(func.count(OutboxEvent.id)).where(OutboxEvent.event_status == "pending")
        )
        or 0
    )
    stale_pending_outbox = int(
        await session.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.event_status == "pending",
                OutboxEvent.created_at <= utc_now() - timedelta(minutes=5),
            )
        )
        or 0
    )
    failure_window_started_at = utc_now() - timedelta(hours=24)
    failed_runs = int(
        await session.scalar(
            select(func.count(AgentRun.id)).where(
                AgentRun.run_status == "failed",
                AgentRun.created_at >= failure_window_started_at,
            )
        )
        or 0
    )
    latest_failure_at = await session.scalar(
        select(func.max(AgentRun.created_at)).where(
            AgentRun.run_status == "failed",
            AgentRun.created_at >= failure_window_started_at,
        )
    )
    successful_runs_after_latest_failure = 0
    if latest_failure_at is not None:
        successful_runs_after_latest_failure = int(
            await session.scalar(
                select(func.count(AgentRun.id)).where(
                    AgentRun.run_status == "completed",
                    AgentRun.created_at > latest_failure_at,
                )
            )
            or 0
        )
    unrecovered_failures = int(
        latest_failure_at is not None and successful_runs_after_latest_failure == 0
    )
    runtime_health: dict[str, object] = {
        "pending_outbox_events": pending_outbox,
        "stale_pending_outbox_events": stale_pending_outbox,
        "failed_agent_runs_24h": failed_runs,
        "successful_runs_after_latest_failure": successful_runs_after_latest_failure,
        "unrecovered_agent_failures": unrecovered_failures,
    }
    if tool_code == "observability.dead_letters.list":
        open_only = any(
            marker in re.sub(r"\s+", "", query_text).casefold()
            for marker in ("待处理", "未处理", "open")
        )
        dead_letter_statement = select(DeadLetterEvent)
        if open_only:
            dead_letter_statement = dead_letter_statement.where(
                DeadLetterEvent.dead_status == "open"
            )
        dead_letters = list(
            (
                await session.scalars(
                    dead_letter_statement.order_by(
                        case((DeadLetterEvent.dead_status == "open", 0), else_=1),
                        DeadLetterEvent.last_failed_at.desc(),
                        DeadLetterEvent.id.desc(),
                    ).limit(20)
                )
            ).all()
        )
        return {
            "dead_letters": [
                {
                    "dead_letter_id": item.dead_letter_no,
                    "event_type": item.event_type,
                    "source_type": item.source_type,
                    "source_id": item.source_no,
                    "scope_type": item.scope_type,
                    "scope_id": item.scope_id,
                    "status": item.dead_status,
                    "failure_count": item.failure_count,
                    "last_error_code": item.last_error_code,
                    "last_error": item.last_error[:300],
                    "last_failed_at": item.last_failed_at.isoformat(),
                    "replay_count": item.replay_count,
                    "payload_hash": item.payload_hash.hex(),
                    "version": item.version,
                    "available_actions": (
                        ["preview_replay", "ignore"] if item.dead_status == "open" else []
                    ),
                }
                for item in dead_letters
            ],
            "open_count": sum(item.dead_status == "open" for item in dead_letters),
            "result_limit": 20,
            "query_mode": "dead_letter_list",
            "status_filter": "open" if open_only else None,
        }
    if tool_code == "observability.runtime_health":
        return runtime_health
    completed_gmv = int(
        await session.scalar(
            select(func.coalesce(func.sum(Order.paid_amount - Order.refunded_amount), 0)).where(
                Order.order_status == "completed"
            )
        )
        or 0
    )
    today_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_orders = int(
        await session.scalar(select(func.count(Order.id)).where(Order.created_at >= today_start))
        or 0
    )
    pending_refunds = int(
        await session.scalar(
            select(func.count(RefundApplication.id)).where(
                RefundApplication.refund_status.in_(
                    (
                        "submitted",
                        "merchant_review",
                        "approved",
                        "waiting_return",
                        "returning",
                        "received",
                        "refunding",
                    )
                )
            )
        )
        or 0
    )
    return {
        "user_status_counts": user_counts,
        "store_status_counts": store_counts,
        "product_status_counts": product_counts,
        "order_status_counts": order_counts,
        "business_metrics": {
            "completed_gmv": {
                "minor_units": completed_gmv,
                "currency": "CNY",
                "display": _money_display(completed_gmv, "CNY"),
                "basis": "已完成订单实付金额减已退款金额",
            },
            "today_order_count": today_orders,
            "pending_after_sale_count": pending_refunds,
            "as_of": utc_now().isoformat(),
        },
        **runtime_health,
    }


async def _counts(session: AsyncSession, field: Any, *conditions: Any) -> dict[str, int]:
    rows = (
        await session.execute(select(field, func.count()).where(*conditions).group_by(field))
    ).all()
    return {str(key): int(value) for key, value in rows}


async def _load_product_edit_snapshot(
    session: AsyncSession, product: Product, store: Store
) -> dict[str, object]:
    """Load one bounded, editable product dossier from the business primary store."""

    sku_rows = list(
        (
            await session.execute(
                select(ProductSku, Inventory)
                .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
                .where(ProductSku.product_id == product.id)
                .order_by(ProductSku.id)
            )
        ).all()
    )
    image_rows = list(
        (
            await session.execute(
                select(ProductImage, FileObject)
                .join(FileObject, FileObject.id == ProductImage.file_id)
                .where(ProductImage.product_id == product.id)
                .order_by(ProductImage.sku_id, ProductImage.sort_order, ProductImage.id)
            )
        ).all()
    )
    images_by_sku: dict[int, list[dict[str, object]]] = {}
    for image, file_object in image_rows:
        images_by_sku.setdefault(image.sku_id, []).append(
            {
                "file_id": file_object.file_no,
                "image_id": image.id,
                "image_type": image.image_type,
                "sort_order": image.sort_order,
                "status": image.image_status,
                "scan_status": file_object.scan_status,
                "ocr_status": file_object.ocr_status,
                "image_url": (
                    f"/api/v1/files/{file_object.file_no}?variant=thumbnail"
                    if file_object.file_status == "active" and file_object.scan_status == "safe"
                    else None
                ),
            }
        )
    attributes = list(
        (
            await session.scalars(
                select(ProductAttribute)
                .where(ProductAttribute.product_id == product.id)
                .order_by(ProductAttribute.sort_order, ProductAttribute.id)
            )
        ).all()
    )
    content = (
        await session.scalar(
            select(ProductContentVersion).where(
                ProductContentVersion.id == product.current_detail_content_version_id,
                ProductContentVersion.product_id == product.id,
            )
        )
        if product.current_detail_content_version_id is not None
        else None
    )
    content_files = (
        list(
            (
                await session.scalars(
                    select(FileObject)
                    .join(
                        ProductContentVersionFile,
                        ProductContentVersionFile.file_id == FileObject.id,
                    )
                    .where(ProductContentVersionFile.content_version_id == content.id)
                    .order_by(ProductContentVersionFile.id)
                )
            ).all()
        )
        if content is not None
        else []
    )
    faq_rows = list(
        (
            await session.execute(
                select(ProductFaq, ProductFaqVersion)
                .outerjoin(
                    ProductFaqVersion,
                    ProductFaqVersion.id == ProductFaq.current_content_version_id,
                )
                .where(ProductFaq.product_id == product.id)
                .order_by(ProductFaq.sort_order, ProductFaq.id)
            )
        ).all()
    )
    fulfillment = await session.scalar(
        select(ProductFulfillmentProfile).where(ProductFulfillmentProfile.product_id == product.id)
    )
    return {
        "product_id": product.product_no,
        "name": product.product_name,
        "status": product.product_status,
        "store": {
            "store_id": store.store_no,
            "store_name": store.store_name,
            "status": store.store_status,
            "version": int(store.version),
        },
        "subtitle": product.subtitle,
        "description": product.description,
        "sales_count": int(product.sales_count),
        "review_count": int(product.review_count),
        "rating_score": float(product.rating_score or 0),
        "version": int(product.version),
        "skus": [
            {
                "sku_id": sku.sku_no,
                "name": sku.sku_name,
                "status": sku.sku_status,
                "spec_values": sku.spec_values,
                "price": {
                    "minor_units": sku.sale_price_amount,
                    "currency": sku.currency,
                    "display": _money_display(sku.sale_price_amount, sku.currency),
                },
                "inventory": {
                    "on_hand": inventory.on_hand_quantity if inventory else 0,
                    "reserved": inventory.reserved_quantity if inventory else 0,
                    "available": (
                        inventory.on_hand_quantity - inventory.reserved_quantity if inventory else 0
                    ),
                    "safety_stock": inventory.safety_stock_quantity if inventory else 0,
                    "version": int(inventory.version) if inventory else None,
                },
                "images": images_by_sku.get(sku.id, []),
                "version": int(sku.version),
            }
            for sku, inventory in sku_rows
        ],
        "attributes": [
            {
                "code": item.attribute_code,
                "name": item.attribute_name,
                "value": item.value_text,
                "unit": item.unit,
            }
            for item in attributes
        ],
        "content": (
            {
                "content_version_id": content.content_version_no,
                "content_version": content.content_version,
                "status": content.version_status,
                "scan_status": content.security_scan_status,
                "format": content.public_content_format,
                "blocks": content.safe_blocks,
                "safe_text": content.safe_text[:6000],
            }
            if content is not None
            else None
        ),
        "ocr_results": [
            {
                "file_id": file.file_no,
                "status": file.ocr_status,
                "text": (file.ocr_text or "")[:1500] or None,
                "engine": file.ocr_engine,
                "processed_at": file.ocr_processed_at.isoformat()
                if file.ocr_processed_at
                else None,
            }
            for file in content_files
        ],
        "faqs": [
            {
                "faq_id": faq.faq_no,
                "question": faq.question,
                "answer": faq_version.safe_text if faq_version is not None else None,
                "status": faq.faq_status,
                "version": faq_version.content_version if faq_version is not None else None,
            }
            for faq, faq_version in faq_rows
        ],
        "fulfillment": (
            {
                "origin_region_code": fulfillment.origin_region_code,
                "dispatch_min_hours": fulfillment.dispatch_min_hours,
                "dispatch_max_hours": fulfillment.dispatch_max_hours,
                "purchase_notice": fulfillment.purchase_notice,
                "version": fulfillment.profile_version,
            }
            if fulfillment is not None
            else None
        ),
    }


def _query_mentions_identifier(query: str, name: str, public_id: str) -> bool:
    """Match a concrete business object without treating generic nouns as filters."""

    compact_query = re.sub(r"\s+", "", query).casefold()
    if not compact_query:
        return False
    compact_name = re.sub(r"\s+", "", name).casefold()
    if compact_name and len(compact_name) >= 2 and compact_name in compact_query:
        return True
    return bool(public_id and public_id.casefold() in query.casefold())


def _query_mentions_catalog_product(query: str, name: str, public_id: str) -> bool:
    """Resolve an explicitly named product even when the operator uses a stable short name."""

    if _query_mentions_identifier(query, name, public_id):
        return True
    compact_name = re.sub(r"\s+", "", name).casefold()
    compact_query = re.sub(r"\s+", "", query).casefold()
    match = re.search(
        r"(?:查询|查看|搜索)(.+?)(?:的(?:销售状态|状态|所属店铺|销量|最低售价|售价|价格)|[，,]|$)",
        compact_query,
    )
    if match is None:
        return False
    candidate = match.group(1).strip("\uff1a:《》【】[]")
    return len(candidate) >= 4 and candidate in compact_name


def _qualified_scope_name(query: str, qualifiers: tuple[str, ...]) -> str | None:
    """Return an explicitly labelled human-readable scope, if one was supplied.

    A missing named user/store must produce an empty result instead of silently
    broadening the query to recent platform records.  Requiring a separator keeps
    ordinary phrases such as ``用户订单`` from being mistaken for a username.
    """

    marker = "|".join(re.escape(item) for item in qualifiers)
    match = re.search(
        rf"(?:{marker})\s*(?:[:\uff1a]|\s)\s*([0-9A-Za-z_\-\u4e00-\u9fff]{{1,64}})",
        query,
    )
    if match is None:
        return None
    return match.group(1).strip()


def _mask_business_reference(value: str | None) -> str | None:
    """Keep a reference recognizable without placing the full provider identifier in traces."""

    if value is None:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * min(12, len(value) - 8)}{value[-4:]}"


def _deterministic_intent(text: str, audience: str) -> str:
    compact = re.sub(r"\s+", "", text).casefold()
    if is_explicit_handoff_request(text):
        return "human_handoff"
    if any(
        term in compact
        for term in (
            "运行",
            "告警",
            "积压",
            "故障",
            "死信",
            "重放",
            "worker",
            "agent运行",
            "ai运行",
            "trace",
            "run_",
        )
    ):
        return "runtime" if audience == "admin" else "overview"
    if any(term in compact for term in ("库存", "缺货", "低库存")):
        return "inventory"
    if audience == "merchant" and any(
        term in compact
        for term in ("店铺资料", "店铺简介", "店铺名称", "店名", "logo", "发货地", "营业状态")
    ):
        return "profile"
    if audience == "merchant" and any(term in compact for term in ("评价", "评分", "差评")):
        return "reviews"
    if audience == "merchant" and any(
        term in compact
        for term in (
            "顾客咨询",
            "顾客会话",
            "会话列表",
            "客服",
            "工单",
            "接待",
            "未读消息",
            "未读状态",
        )
    ):
        return "service"
    if audience == "merchant" and any(
        term in compact for term in ("政策", "规则", "退换", "包邮", "发货承诺")
    ):
        return "policy"
    if audience == "merchant" and any(
        term in compact for term in ("售后", "退款申请", "退货", "退款进度")
    ):
        return "after_sale"
    if audience == "admin" and any(
        term in compact for term in ("退款", "售后", "退货", "申诉", "ref_", "rfd_", "rap_", "rfp_")
    ):
        return "after_sale"
    if audience == "admin" and any(term in compact for term in ("客服", "工单", "人工队列")):
        return "support"
    if audience == "admin" and any(
        term in compact
        for term in (
            "知识库",
            "rag",
            "skill",
            "mcp",
            "模型配置",
            "模型质量",
            "评估",
            "系统提示词",
            "提示词",
            "prompt",
            "agent配置",
            "agent版本",
        )
    ):
        return "ai_governance"
    if any(
        term in compact
        for term in (
            "订单",
            "支付",
            "付款",
            "营业额",
            "收入",
            "履约",
            "物流",
            "包裹",
            "快递",
            "运单",
        )
    ):
        return "orders"
    if any(term in compact for term in ("商品", "上架", "下架", "审核")):
        return "catalog"
    if audience == "admin" and any(term in compact for term in ("用户", "账号")):
        return "users"
    if audience == "admin" and any(term in compact for term in ("店铺", "商家")):
        return "stores"
    return "overview"


def _operations_small_talk_reply(text: str, audience: str) -> str | None:
    compact = re.sub(r"[\s\u3002\uff01!\uff1f?]+", "", text).casefold()
    if any(term in compact for term in ("人工客服", "平台客服", "人工服务")):
        if audience == "merchant":
            return (
                "平台人工客服暂未配置公开的固定服务时段。需要人工协助时，直接告诉我"
                "“请帮我转人工客服”即可。转接后我会暂停回复，人工服务结束后再继续协助你。"
            )
        return (
            "你当前已在超级管理端。AI 管家不会把管理员会话转交给普通客服。"
            "如需人工协作，请联系系统运维负责人。"
        )
    if compact not in {
        "你好",
        "您好",
        "hello",
        "hi",
        "在吗",
        "谢谢",
        "你是谁",
        "你能做什么",
        "有什么功能",
    }:
        return None
    if audience == "merchant":
        return (
            "你好，我是 AI 经营助理。我可以生成经营简报，分析本店商品、库存、订单、"
            "履约和营业额，并把风险与下一步整理成可操作卡片，需要平台处理时也能发起转接。"
        )
    return (
        "你好，我是超级管理员 AI 管家。我可以在管理员权限范围内协助分析用户、店铺、"
        "商品、交易与系统运行情况。所有业务查询默认只读并保留审计记录。"
    )


def _operations_subtask_trace(
    tasks: tuple[OperationsSupervisorSubtask, ...], audience: str, user_text: str = ""
) -> list[dict[str, object]]:
    merchant_names = {
        "overview": "经营总览 Agent",
        "profile": "店铺与商品运营 Agent",
        "catalog": "商品运营 Agent",
        "inventory": "库存风险 Agent",
        "orders": "订单履约 Agent",
        "reviews": "评价运营 Agent",
        "service": "顾客服务 Agent",
        "policy": "店铺政策 Agent",
        "after_sale": "售后评价 Agent",
    }
    admin_names = {
        "overview": "平台总览 Agent",
        "users": "用户治理 Agent",
        "stores": "店铺治理 Agent",
        "catalog": "商品治理 Agent",
        "inventory": "商品治理 Agent",
        "orders": "订单治理 Agent",
        "after_sale": "售后治理 Agent",
        "support": "客服治理 Agent",
        "ai_governance": "AI 治理 Agent",
        "runtime": "运行诊断 Agent",
    }
    names = merchant_names if audience == "merchant" else admin_names
    return [
        {
            "subtask_key": task.subtask_key,
            "specialist": names.get(task.intent, "受限领域 Agent"),
            "intent": task.intent,
            "objective": task.objective,
            "allowed_tools": [_tool_for_query(task.intent, audience, task.objective)],
            "depth": 0,
        }
        for task in tasks
    ]


def _tool_for_intent(intent: str, audience: str) -> str:
    if audience == "merchant":
        return {
            "profile": "store_ops.profile.get",
            "catalog": "store_ops.catalog_summary",
            "orders": "store_ops.order_summary",
            "inventory": "store_ops.inventory_risks",
            "reviews": "store_ops.review_summary",
            "service": "store_ops.service_summary",
            "policy": "store_ops.policy_summary",
            "after_sale": "store_ops.after_sale.list",
        }.get(intent, "store_ops.overview")
    return {
        "users": "governance.users.search",
        "stores": "governance.stores.search",
        "orders": "governance.order_summary",
        "catalog": "governance.catalog.search",
        "inventory": "governance.catalog.search",
        "runtime": "observability.runtime_health",
        "after_sale": "governance.after_sale_summary",
        "support": "governance.support_summary",
        "ai_governance": "governance.ai_summary",
    }.get(intent, "governance.platform_overview")


def _tool_for_query(intent: str, audience: str, user_text: str) -> str:
    """Select the narrowest registered read tool without changing Agent domains.

    The Supervisor still delegates by business domain.  This selector prevents a
    single-domain request such as "查看店铺资料" from being answered by an overly
    broad dashboard snapshot, while compound requests continue to use the domain
    summary tools through the multi-Agent path.
    """

    compact = re.sub(r"\s+", "", user_text).casefold()
    if audience != "merchant":
        if intent == "stores" and _is_admin_store_service_profile_query(compact):
            return "governance.stores.service_profile"
        if intent == "users":
            if "地址" in compact:
                return "governance.users.addresses.list"
            if "购物车" in compact:
                return "governance.users.cart.list"
            if any(term in compact for term in ("收藏", "关注店铺")):
                return "governance.users.favorites.list"
            if any(term in compact for term in ("资金流水", "钱包流水", "余额")):
                return "governance.users.wallet.get"
            if any(term in compact for term in ("购买订单", "订单列表", "订单详情")):
                return "governance.users.orders.list"
        if intent == "runtime":
            if any(term in compact for term in ("死信", "重放", "失败事件")):
                return "observability.dead_letters.list"
            if re.search(r"(?:run|trc)_[0-9a-z]+", compact, flags=re.IGNORECASE):
                return "observability.traces.get"
            if any(
                term in compact
                for term in (
                    "调用链路",
                    "执行链路",
                    "trace",
                    "run详情",
                    "失败的agent运行",
                    "失败agent运行",
                    "失败运行",
                    "最近失败",
                )
            ):
                return "observability.traces.search"
            if any(term in compact for term in ("token", "成本", "耗时", "延迟", "成功率")):
                return "observability.cost_metrics"
        if intent == "ai_governance":
            if any(
                term in compact
                for term in (
                    "评估",
                    "质量门槛",
                    "发布门禁",
                    "golden",
                    "测试集",
                    "evr_",
                )
            ):
                return "governance.ai.evaluations.list"
            if any(
                term in compact
                for term in (
                    "知识库",
                    "知识文档",
                    "索引任务",
                    "索引状态",
                    "rag",
                    "kdoc_",
                )
            ):
                return "governance.knowledge.documents.list"
            if any(term in compact for term in ("skill", "技能", "能力绑定")):
                return "governance.ai.skills.list"
            if any(term in compact for term in ("mcp", "tool", "工具", "权限策略")):
                return "governance.ai.tools.list"
            if any(term in compact for term in ("agent", "智能体", "模型配置", "模型版本")):
                return "governance.ai.agents.list"
        if intent == "after_sale" and any(
            term in compact
            for term in (
                "售后详情",
                "退款详情",
                "售后进度",
                "退款进度",
                "售后时间线",
                "退款时间线",
                "退货物流",
                "退款去向",
                "退款支付",
                "申诉",
                "ref_",
                "rfd_",
                "rap_",
            )
        ):
            return "governance.after_sale.timeline"
        if intent == "orders" and any(
            term in compact for term in ("物流", "包裹", "快递", "运单", "轨迹", "签收")
        ):
            return "governance.trade.shipments.get"
        if intent == "orders" and any(
            term in compact
            for term in (
                "支付单",
                "支付流水",
                "支付事件",
                "支付回调",
                "付款记录",
                "渠道回调",
                "对账事件",
                "支付时间线",
            )
        ):
            return "governance.trade.payment_timeline"
        if any(
            term in compact
            for term in (
                "平台指标",
                "经营指标",
                "gmv",
                "成交额",
                "订单量",
                "新增用户",
                "新增店铺",
            )
        ):
            return "governance.metrics.query"
        return _tool_for_intent(intent, audience)
    if intent == "overview" and any(
        term in compact
        for term in ("店铺资料", "店铺简介", "店铺名称", "店名", "logo", "发货地", "营业状态")
    ):
        return "store_ops.profile.get"
    if intent == "orders":
        if any(term in compact for term in ("售后", "退款申请", "退货", "退款进度")):
            return "store_ops.after_sale.list"
        revenue_requested = any(
            term in compact for term in ("营业额", "收入", "收益", "营收", "待确认金额")
        )
        order_records_requested = any(
            term in compact
            for term in (
                "待发货",
                "待履约",
                "运输中",
                "在途",
                "订单列表",
                "订单明细",
                "哪些订单",
                "哪几笔订单",
            )
        )
        if (
            revenue_requested
            and not order_records_requested
            and not any(term in compact for term in ("订单详情", "哪笔订单", "订单号"))
        ):
            return "store_ops.revenue_metrics"
        if revenue_requested and order_records_requested:
            return "store_ops.order_summary"
        if any(term in compact for term in ("订单详情", "哪笔订单", "订单号")):
            return "store_ops.orders.get"
        return "store_ops.orders.list"
    if intent in {"catalog", "inventory"}:
        inventory_detail_requested = any(
            term in compact
            for term in (
                "实时可售",
                "可售为0",
                "可售0",
                "库存为0",
                "库存0",
                "零库存",
                "现有库存",
                "已预占",
                "库存数量",
                "全部款式库存",
                "各款式库存",
            )
        )
        if inventory_detail_requested:
            return "store_ops.inventory.get_skus"
        # A multi-intent request such as "概览商品和库存" gives both the
        # catalog and inventory subtasks the original user text.  Generic
        # "库存" therefore must not move the catalog subtask onto an inventory
        # tool: that both drops the catalog evidence and violates the catalog
        # specialist's tool policy.  Only explicit risk language narrows a
        # catalog task to the inventory-risk specialist; the independent
        # inventory subtask remains responsible for ordinary stock facts.
        if intent == "catalog" and any(
            term in compact for term in ("缺货", "低库存", "安全库存", "补货", "库存风险")
        ):
            return "store_ops.inventory_risks"
    if intent == "inventory" and not any(
        term in compact for term in ("缺货", "低库存", "风险", "不足", "安全库存")
    ):
        return "store_ops.inventory.get_skus"
    if intent == "catalog" and any(
        term in compact
        for term in (
            "商品详情",
            "完整信息",
            "编辑内容",
            "详情块",
            "ocr",
            "常见问题",
            "这个商品",
            "该商品",
            "资料完整",
            "能否提交",
            "可以提交",
            "上架检查",
            "提交审核前",
        )
    ):
        return "store_ops.catalog.get_product"
    if intent == "reviews":
        return "store_ops.reviews.list"
    if intent == "service":
        return "store_ops.conversations.list"
    return _tool_for_intent(intent, audience)


def _render(
    context: TrustedOperationsContext,
    intent: str,
    data: Mapping[str, Any],
    *,
    user_text: str = "",
) -> str:
    if context.audience == "merchant" and intent == "catalog":
        product_detail = data.get("product_detail")
        if isinstance(product_detail, Mapping):
            sku_values = product_detail.get("skus")
            sku_count = len(sku_values) if isinstance(sku_values, list) else 0
            content = product_detail.get("content")
            faq_values = product_detail.get("faqs")
            return (
                f"已读取《{product_detail.get('name') or '该商品'}》的当前可编辑版本，"
                f"包括 {sku_count} 个款式、"
                f"{'商品详情' if isinstance(content, Mapping) else '尚未填写的商品详情'}和 "
                f"{len(faq_values) if isinstance(faq_values, list) else 0} 组常见问题。"
                "状态、OCR、履约资料和编辑入口已分区整理在卡片中。"
            )
        candidates = data.get("candidate_products")
        if data.get("query_mode") == "product_selection" and isinstance(candidates, list):
            if candidates:
                return "找到多个可能的本店商品，请从卡片中选择一个后再查看完整编辑内容。"
            return "本店当前没有可管理的商品。"
        products = data.get("on_sale_products")
        if not isinstance(products, list) or not products:
            return "本店当前没有可售商品。本次只读取了本店授权范围内的数据。"
        low_stock_names: list[str] = []
        for product in products:
            if not isinstance(product, dict):
                continue
            skus = product.get("skus")
            for sku in skus if isinstance(skus, list) else []:
                if not isinstance(sku, dict):
                    continue
                inventory = sku.get("inventory")
                available = inventory.get("available") if isinstance(inventory, dict) else 0
                if isinstance(available, int) and available <= 5:
                    low_stock_names.append(f"{product.get('name')}/{sku.get('name')}")
        if low_stock_names:
            return (
                f"已核对 {len(products)} 件在售商品。建议先处理这些低库存款式: "
                + "、".join(low_stock_names[:3])
                + "。详细价格和库存已整理在下方卡片中。"
            )
        return (
            f"已核对 {len(products)} 件在售商品，当前没有款式触发低库存提醒。明细已整理在卡片中。"
        )
    if context.audience == "merchant":
        store_data = data.get("store")
        store_name = (
            str(store_data.get("name"))
            if isinstance(store_data, Mapping) and store_data.get("name")
            else "本店"
        )
        if intent == "overview":
            order_counts = data.get("order_status_counts")
            counts = order_counts if isinstance(order_counts, Mapping) else {}
            pending = sum(
                int(counts.get(key, 0)) for key in ("paid", "pending_shipment", "shipped")
            )
            low_stock = int(data.get("low_stock_sku_count", 0))
            if low_stock:
                priority = f"最优先处理 {low_stock} 个低库存或缺货款式"
            elif pending:
                priority = f"最优先跟进 {pending} 单待履约或运输中订单"
            else:
                priority = "当前没有紧急库存或履约积压，可优先优化在售商品信息"
            return f"已生成{store_name}经营快照。{priority}，营业额和订单明细已整理在卡片中。"
        if intent == "profile":
            return "已核对当前店铺公开资料、营业状态和资料版本，编辑入口已整理在卡片中。"
        if intent == "inventory":
            inventory_skus = data.get("inventory_skus")
            if isinstance(inventory_skus, list):
                return (
                    f"已读取本店 {len(inventory_skus)} 个可管理款式的实时库存、预占和可售数量。"
                    "请从卡片进入对应商品处理。"
                )
            low_stock = int(data.get("low_stock_sku_count", 0))
            if low_stock == 0:
                return "本店当前没有低库存或缺货款式，可继续保持日常库存巡检。"
            if any(marker in user_text for marker in ("什么影响", "有何影响", "后果")):
                return (
                    f"刚才的 {low_stock} 个风险款式若今天不处理，已缺货款式将无法产生新的"
                    "有效成交，顾客选择时也可能无法结算。低库存款式则更容易在后续下单时"
                    "售罄。是否影响已有订单仍需到订单页核对库存预占与履约状态，我不会在"
                    "没有订单证据时猜测。请从下方卡片直接进入该商品处理。"
                )
            return (
                f"本店有 {low_stock} 个款式达到低库存或缺货阈值。"
                "请先打开下方卡片核对实时可售数量和安全库存线。"
            )
        if intent == "orders" and isinstance(data.get("today_revenue"), Mapping):
            return (
                "已按统一结算口径核对今日、昨日、近 30 日、累计营业额及已支付待确认金额。"
                "各时间范围和完成订单数已整理在卡片中。"
            )
        if intent == "orders" and data.get("query_mode") in {"list", "detail"}:
            orders = data.get("recent_orders")
            matched = len(orders) if isinstance(orders, list) else 0
            if data.get("query_mode") == "detail":
                if matched:
                    return (
                        "已找到该订单，商品、成交金额、支付、履约、售后和收货快照"
                        "已整理在订单卡片中。"
                    )
                return "没有找到属于本店且符合当前条件的订单，请核对订单号后重试。"
            if matched:
                return f"已找到 {matched} 笔符合条件的本店订单，可从卡片直接查看和处理。"
            return "当前没有符合筛选条件的本店订单。"
        if intent == "after_sale" and data.get("query_mode") in {
            "after_sale_list",
            "after_sale_detail",
        }:
            refunds = data.get("recent_refunds")
            matched = len(refunds) if isinstance(refunds, list) else 0
            if matched == 0:
                return "当前没有符合售后单、订单、顾客或状态条件的本店售后记录。"
            if data.get("query_mode") == "after_sale_detail":
                return (
                    "已从本店业务数据读取该售后单的商品、处理事件、退货物流、退款支付和"
                    "申诉链路。各类事实已分卡展示，可直接进入售后工作台处理。"
                )
            return f"已找到 {matched} 条符合条件的本店售后记录，请从卡片选择目标继续处理。"
        if intent == "policy":
            sources = data.get("knowledge_sources")
            source_values = (
                [item for item in sources if isinstance(item, Mapping)]
                if isinstance(sources, list)
                else []
            )
            if source_values:
                first = source_values[0]
                excerpt = str(first.get("excerpt") or "")
                points = [
                    line.removeprefix("- ").strip().rstrip("。")
                    for line in excerpt.splitlines()
                    if line.strip().startswith("- ")
                ][:3]
                answer = f"已从平台发布的《{first.get('title') or '商家规则'}》中找到对应规则。"
                if points:
                    answer += "\n\n" + "\n".join(f"- {point}。" for point in points)
                return answer + "\n\n完整条款和来源已整理在卡片中。"
            return "平台规则知识库暂未返回可靠结果，本次不会用店铺草稿或猜测代替平台规则。"
        pagination = data.get("pagination")
        if intent == "reviews" and isinstance(pagination, Mapping):
            returned = int(pagination.get("returned_count") or 0)
            continued = bool(pagination.get("continued"))
            has_more = bool(pagination.get("has_more"))
            if continued and returned == 0:
                return "评价已经全部加载完了，没有下一批记录。"
            prefix = "已继续加载" if continued else "已读取"
            suffix = "回复“下一页”可继续查看。" if has_more else "这已经是最后一批。"
            return (
                f"{prefix} {returned} 条本店评价，待回复优先；评价与回复已整理成独立卡片。{suffix}"
            )
        if (
            intent == "service"
            and data.get("query_mode") == "conversation_list"
            and isinstance(pagination, Mapping)
        ):
            returned = int(pagination.get("returned_count") or 0)
            continued = bool(pagination.get("continued"))
            has_more = bool(pagination.get("has_more"))
            if continued and returned == 0:
                return "顾客会话已经全部加载完了，没有下一批记录。"
            prefix = "已继续加载" if continued else "已读取"
            suffix = "回复“下一页”可继续查看。" if has_more else "这已经是最后一批。"
            return (
                f"{prefix} {returned} 个本店顾客会话，未读数与 AI/人工接待状态已整理成卡片。"
                f"{suffix}"
            )
        return {
            "orders": (
                "已完成本店订单、履约与营业额核对。先看卡片中的待处理数量，再进入订单页处理。"
            ),
            "reviews": "已核对本店评价、平均评分和待回复数量，处理入口已整理在卡片中。",
            "service": "已核对本店顾客咨询与人工服务队列，待接待数量已整理在卡片中。",
            "policy": "已核对本店已发布服务政策，草稿政策不会作为对外承诺。",
            "after_sale": "已核对本店售后申请、处理状态和近期待办，明细已整理在卡片中。",
        }.get(intent, f"已生成{store_name}经营快照，明细已整理为可操作卡片。")
    metric_window = data.get("metric_window")
    if isinstance(metric_window, Mapping) and isinstance(data.get("business_metrics"), Mapping):
        return (
            f"已按近 {metric_window.get('days') or 7} 天统一口径核对平台订单量、成交金额、"
            "已完成净额、退款额及新增用户、店铺和商品，明细已整理在指标卡片中。"
        )
    if isinstance(data.get("runs"), list):
        matched = int(data.get("matched_count") or 0)
        if matched:
            return (
                f"已找到 {matched} 条真实 Agent 运行链路，委派、工具结果和错误状态已整理在卡片中。"
            )
        return "没有找到匹配的 Agent Run 或 Trace，请核对编号后重试。"
    if "agent_run_count" in data and "tool_call_count" in data:
        return (
            "已核对指定窗口内的 Agent 运行数、完成率、工具成功率和平均工具延迟。"
            "当前 Token 与成本采集覆盖度也已明确标注，不会用估算冒充实测。"
        )
    if intent == "ai_governance":
        requested_asset = data.get("requested_governance_asset")
        if requested_asset == "evaluations":
            evaluations = data.get("evaluations")
            evaluation_values = (
                [item for item in evaluations if isinstance(item, Mapping)]
                if isinstance(evaluations, list)
                else []
            )
            matched = len(evaluation_values)
            if matched == 0:
                return (
                    "当前没有符合条件的 AI 评估运行; 已核对当前固定测试集和已登记的基线/候选版本，"
                    "可从卡片进入评估中心发起真实评估。"
                )
            blocked = sum(
                1
                for item in evaluation_values
                if item.get("release_gate") in {"fail", "insufficient_evidence"}
            )
            return (
                f"已读取最近 {matched} 次真实 AI 评估，其中 {blocked} 次未达到发布准入; "
                "测试集版本、质量指标、阻断原因和 Trace 入口已整理在卡片中。"
            )
        if requested_asset == "knowledge_documents":
            documents = data.get("documents")
            matched = len(documents) if isinstance(documents, list) else 0
            if matched == 0:
                return "没有找到符合文档编号、标题或发布状态条件的知识文档。"
            if data.get("query_mode") == "knowledge_document_detail":
                return (
                    "已核对该知识文档的权威状态、内容版本和历次索引任务，正文不会复制到聊天，"
                    "可从卡片进入治理页继续检查或操作。"
                )
            return (
                f"已从业务主库读取 {matched} 份知识文档，并把发布状态、作用范围、内容版本和"
                "最近索引结果整理为治理卡片。"
            )
        if requested_asset == "skills":
            skills = data.get("skills")
            return (
                f"已读取 {len(skills) if isinstance(skills, list) else 0} 个 Skill 的发布版本、"
                "准入证据和 Tool 绑定策略; 调用预算与确认方式已整理在治理卡片中。"
            )
        if requested_asset == "tools":
            tools = data.get("tools")
            return (
                f"已读取 {len(tools) if isinstance(tools, list) else 0} 个 Tool 的服务归属、"
                "风险级别、发布版本和输入字段; 本次只读，没有启停或修改工具。"
            )
        if requested_asset == "agents":
            agents = data.get("agents")
            return (
                f"已读取 {len(agents) if isinstance(agents, list) else 0} 个 Agent 的作用域、"
                "模型配置、发布版本、Skill 和 Tool 授权; 本次只读，没有发布新版本。"
            )
    if intent == "users" and isinstance(data.get("selected_user"), Mapping):
        selected_user = data["selected_user"]
        requested_asset = data.get("requested_user_asset")
        asset_names = {
            "addresses": "收货地址",
            "cart": "购物车",
            "favorites": "收藏商品与关注店铺",
            "orders": "非取消订单",
            "wallet": "账户余额与资金流水",
        }
        if requested_asset in asset_names:
            return (
                f"已按实时主库核对用户 {selected_user.get('username') or ''} 的"
                f"{asset_names[str(requested_asset)]}，明细已整理在卡片中。"
            )
        return (
            f"已找到用户 {selected_user.get('username') or ''}，账号状态、在线会话、余额、"
            "地址、购物车、收藏和近期订单已按实时数据整理在卡片中。"
        )
    if intent == "stores" and isinstance(data.get("service_profile"), Mapping):
        profile = data["service_profile"]
        missing_items = profile.get("missing_items")
        missing_values = missing_items if isinstance(missing_items, list) else []
        if missing_values:
            missing_summary = "、".join(str(item) for item in missing_values)
            return (
                f"已按业务主库核对店铺《{profile.get('store_name') or ''}》的服务资料，"
                f"当前缺少 {len(missing_values)} 项：{missing_summary}。"
                "店铺简介、商品履约资料、配送模板和已发布政策已整理在一张整改卡中。"
            )
        return (
            f"已按业务主库核对店铺《{profile.get('store_name') or ''}》的服务资料，"
            "店铺简介、发货资料、配送模板与已发布售后政策均有可用记录。"
        )
    if intent == "stores" and isinstance(data.get("selected_store"), Mapping):
        selected_store = data["selected_store"]
        return (
            f"已找到店铺《{selected_store.get('store_name') or ''}》，店主、经营状态、"
            "商品、订单、营业额和售后待办已整理在卡片中。"
        )
    if intent == "catalog" and isinstance(data.get("selected_product"), Mapping):
        selected_product = data["selected_product"]
        return (
            f"已找到商品《{selected_product.get('name') or ''}》，款式、价格、库存、图片扫描、"
            "参数、详情、OCR、常见问题、发货资料和资源版本已按主库最新数据整理。"
        )
    if intent == "orders" and data.get("query_mode") in {
        "payment_list",
        "payment_detail",
    }:
        payments = data.get("payments")
        matched = len(payments) if isinstance(payments, list) else 0
        if matched == 0:
            return "当前没有符合支付单、交易单、订单、顾客、店铺或支付状态条件的支付记录。"
        if data.get("query_mode") == "payment_detail":
            return (
                "已从业务主库读取该支付单、交易单、关联店铺订单、不可变支付事件和渠道回调状态。"
                "原始回调正文、签名和支付凭据不会进入 Agent 上下文。"
            )
        return f"已找到 {matched} 笔符合条件的支付记录，请从卡片选择目标后查看完整时间线。"
    if intent == "orders" and data.get("query_mode") in {
        "shipment_list",
        "shipment_detail",
    }:
        shipments = data.get("shipments")
        matched = len(shipments) if isinstance(shipments, list) else 0
        if matched == 0:
            return "当前没有符合包裹号、订单号、顾客、店铺或物流状态条件的包裹。"
        if data.get("query_mode") == "shipment_detail":
            return (
                "已从业务主库读取该包裹的当前节点和不可变物流轨迹。"
                "模拟物流与真实承运商数据已在卡片中明确区分。"
            )
        return f"已找到 {matched} 个符合条件的包裹，当前节点和最近轨迹已整理在卡片中。"
    if intent == "orders" and data.get("query_mode") in {"list", "detail"}:
        orders = data.get("recent_orders")
        matched = len(orders) if isinstance(orders, list) else 0
        if not matched:
            return "当前没有符合用户、店铺、状态、时间或订单号筛选条件的平台订单。"
        if data.get("query_mode") == "detail":
            return "已找到该订单，顾客、店铺、商品、金额与全维度状态已整理在订单卡片中。"
        return f"已找到 {matched} 笔符合条件的平台订单，完整商品与状态已整理在卡片中。"
    if intent == "after_sale" and data.get("query_mode") in {
        "after_sale_list",
        "after_sale_detail",
    }:
        refunds = data.get("refunds")
        matched = len(refunds) if isinstance(refunds, list) else 0
        if matched == 0:
            return "当前没有符合售后单、订单、申诉、顾客、店铺或状态条件的售后记录。"
        if data.get("query_mode") == "after_sale_detail":
            return (
                "已从业务主库读取该售后单的商品、处理事件、退货物流、退款支付和申诉链路。"
                "各类事实按独立卡片展示，不用当前状态反推缺失历史。"
            )
        return f"已找到 {matched} 条符合条件的售后记录，请从卡片选择目标查看完整处理链路。"
    if intent == "runtime" and data.get("query_mode") == "dead_letter_list":
        rows = data.get("dead_letters")
        count = len(rows) if isinstance(rows, list) else 0
        if count == 0:
            return "当前没有死信事件，异步失败队列无需人工处置。"
        return (
            f"已核对最近 {count} 条死信事件，其中 {int(data.get('open_count') or 0)} 条待处理。"
            "失败事实、来源与处置入口已整理在卡片中; 重放必须先生成确认卡，再进入双人审批。"
        )
    return {
        "users": "已完成平台用户状态核对。异常状态和治理入口已整理在卡片中。",
        "stores": "已完成店铺与商品状态核对。建议优先处理暂停店铺和非在售商品。",
        "catalog": "已完成平台商品状态核对。商品治理入口已附在卡片中。",
        "orders": "已完成订单履约状态核对。待付款、待发货、运输中和售后风险已整理在卡片中。",
        "runtime": "已完成 Agent 与异步链路健康核对。故障、积压和恢复状态已整理在卡片中。",
        "after_sale": "已完成平台售后状态核对。待审核、退货中与退款中事项已整理在卡片中。",
        "support": "已完成人工服务队列核对。排队与处理中工单已整理在卡片中。",
        "ai_governance": "已完成 Agent、知识文档和运行状态核对，治理入口已整理在卡片中。",
    }.get(intent, "已生成平台运营快照。用户、店铺、商品、订单和运行风险已整理为卡片。")


def _operations_detail_cards(
    context: TrustedOperationsContext, intent: str, data: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Turn trusted operational evidence into compact, actionable UI cards."""

    merchant_revenue_card: dict[str, Any] | None = None
    failed_subtasks: list[dict[str, Any]] = []
    raw_subtasks = data.get("subtask_results")
    if isinstance(raw_subtasks, list):
        for raw_subtask in raw_subtasks:
            if not isinstance(raw_subtask, Mapping):
                continue
            status = str(raw_subtask.get("status") or "")
            if status in {"succeeded", "reused"}:
                continue
            objective = str(raw_subtask.get("objective") or "该领域任务")
            retry_prompt = str(raw_subtask.get("retry_prompt") or "").strip()
            failed_subtasks.append(
                {
                    "kind": "operations_subtask_failure",
                    "icon": "重",
                    "eyebrow": "可单独重试",
                    "title": objective,
                    "badge": "结果不完整" if status == "partial" else "未完成",
                    "tone": "warning" if status == "partial" else "danger",
                    "summary": "其他已成功结果不会丢失；本次只重新执行这一项。",
                    "rows": [
                        {
                            "label": "领域 Agent",
                            "value": _specialist_label(str(raw_subtask.get("specialist") or "")),
                        },
                        {
                            "label": "工具",
                            "value": str(raw_subtask.get("tool_code") or "未调用"),
                            "meta": str(raw_subtask.get("error_code") or "暂时不可用"),
                        },
                    ],
                    "action": {
                        "label": "只重试此项",
                        "prompt": retry_prompt or f"只重试这项任务：{objective}",
                    },
                }
            )

    operation_result = data.get("operation_result")
    if intent == "operations_action_result" and isinstance(operation_result, Mapping):
        result = operation_result.get("result")
        result_rows: list[dict[str, str]] = []
        if isinstance(result, Mapping):
            action_type = str(operation_result.get("action_type") or "")
            labels = {
                "status": "最新状态",
                "username": "用户",
                "store_name": "店铺",
                "description": "店铺简介",
                "merchant_email_masked": "商家恢复邮箱",
                "email_verification_status": "邮箱状态",
                "image_description": "图片说明",
                "product_name": "商品",
                "sku_name": "款式",
                "image_count": "当前图片",
                "on_hand_quantity": "当前库存",
                "reserved_quantity": "已预占",
                "available_quantity": "当前可售",
                "revoked_session_count": "撤销会话",
                "shipment_id": "包裹",
                "order_id": "订单",
                "order_status": "订单状态",
                "payment_status": "支付状态",
                "carrier_name": "承运商",
                "tracking_no_masked": "运单号",
                "customer_name": "顾客",
                "direction": "调整方向",
                "transaction_id": "资金流水",
                "dead_letter_id": "死信编号",
                "event_type": "事件类型",
                "approval_request_id": "审批申请",
                "required_approval_count": "所需复核",
                "approved_count": "已复核",
                "knowledge_document_id": "知识文档",
                "content_version": "内容版本",
                "index_job_id": "索引任务",
                "index_status": "索引状态",
                "title": "文档标题",
                "ai_entity_type": "治理对象",
                "ai_entity_code": "对象代码",
                "version_no": "候选版本",
                "evaluation_id": "评估任务",
                "dataset_id": "固定测试集",
                "dataset_version": "测试集版本",
                "baseline_version": "生产基线",
                "candidate_version": "候选策略",
                "release_gate": "发布门禁",
                "address_id": "收货地址",
                "recipient_name": "收货人",
                "phone_masked": "联系电话",
                "region": "地区",
                "address": "详细地址",
                "active_address_count": "有效地址数",
                "target_name": "收藏对象",
                "cart_total_quantity": "购物车商品数",
                "removed_item_count": "移除条目",
                "favorite_product_count": "商品收藏数",
                "followed_store_count": "店铺收藏数",
                "origin_region_code": "发货地区代码",
                "credential_status": "密码凭证",
                "merchant_username": "商家用户名",
                "merchant_user_id": "商家账号",
            }
            for key, label in labels.items():
                if key == "description" and "product" in action_type:
                    label = "商品描述"
                value = result.get(key)
                if value is None:
                    continue
                if key == "status":
                    status_labels = {
                        "active": (
                            "正常"
                            if action_type.startswith("admin_user_")
                            else "营业中"
                            if action_type.startswith("admin_store_")
                            else "正常"
                        ),
                        "suspended": (
                            "已冻结"
                            if action_type.startswith("admin_user_")
                            else "暂停营业"
                            if action_type.startswith("admin_store_")
                            else "已暂停"
                        ),
                        "on_sale": "销售中",
                        "off_shelf": "已下架",
                        "created": "待揽收",
                        "picked_up": "已揽收",
                        "in_transit": "运输中",
                        "delivered": "已签收",
                        "exception": "物流异常",
                        "returned": "已退回",
                        "published": "已发布",
                        "withdrawn": "已撤回",
                        "draft": "草稿",
                        "queued": "排队中",
                        "empty": "已清空",
                        "disabled": "已停用",
                    }
                    value = status_labels.get(str(value), value)
                elif key == "order_status":
                    value = {
                        "pending_payment": "待付款",
                        "cancelled": "已取消",
                        "closed": "已关闭",
                    }.get(str(value), value)
                elif key == "payment_status":
                    value = {
                        "unpaid": "未支付",
                        "cancelled": "已取消",
                    }.get(str(value), value)
                elif key == "direction":
                    value = {"credit": "增加", "debit": "扣减"}.get(str(value), value)
                suffix = (
                    " 个"
                    if key == "revoked_session_count"
                    else " 件"
                    if key.endswith("quantity")
                    else " 张"
                    if key == "image_count"
                    else ""
                )
                result_rows.append({"label": label, "value": f"{value}{suffix}"})
            price_minor = result.get("price_minor")
            if isinstance(price_minor, int):
                result_rows.append(
                    {"label": "最新售价", "value": _money_display(price_minor, "CNY")}
                )
            amount_minor = result.get("amount_minor")
            balance_minor = result.get("balance_minor")
            if isinstance(amount_minor, int):
                result_rows.append(
                    {"label": "调整金额", "value": _money_display(amount_minor, "CNY")}
                )
            if isinstance(balance_minor, int):
                result_rows.append(
                    {"label": "最新余额", "value": _money_display(balance_minor, "CNY")}
                )
        succeeded = operation_result.get("status") == "succeeded"
        action_type = str(operation_result.get("action_type") or "")
        path = (
            "/admin/approval-requests/" + str(result.get("approval_request_id"))
            if action_type
            in {
                "admin_dead_letter_replay_request",
                "admin_ai_agent_publish_request",
                "admin_ai_skill_publish_request",
                "admin_ai_tool_publish_request",
            }
            and isinstance(result, Mapping)
            and result.get("approval_request_id")
            else "/admin/shipments/" + str(result.get("shipment_id"))
            if action_type == "admin_shipment_progress"
            and isinstance(result, Mapping)
            and result.get("shipment_id")
            else "/admin/orders/" + str(result.get("order_id"))
            if action_type == "admin_order_cancel"
            and isinstance(result, Mapping)
            and result.get("order_id")
            else "/merchant/orders"
            if context.audience == "merchant" and "shipment" in action_type
            else "/merchant/products/" + str(result.get("product_id"))
            if action_type
            in {
                "merchant_product_draft_create",
                "merchant_product_profile",
                "merchant_product_sku_create",
                "merchant_product_sku_disable",
                "merchant_product_sku_image_replace",
                "merchant_product_faq_upsert",
                "merchant_product_faq_delete",
                "merchant_product_detail_section_upsert",
                "merchant_product_detail_section_delete",
            }
            and isinstance(result, Mapping)
            and result.get("product_id")
            else "/merchant/products"
            if context.audience == "merchant" and "review" in action_type
            else "/admin/stores/" + str(result.get("store_id"))
            if action_type.startswith("admin_product_")
            and isinstance(result, Mapping)
            and result.get("store_id")
            else "/admin/stores/" + str(result.get("store_id"))
            if action_type.startswith("admin_store_")
            and isinstance(result, Mapping)
            and result.get("store_id")
            else "/merchant/products"
            if context.audience == "merchant"
            and any(marker in action_type for marker in ("inventory", "price", "product"))
            else "/merchant/store"
            if context.audience == "merchant"
            else "/admin/knowledge/documents/" + str(result.get("knowledge_document_id"))
            if action_type.startswith("admin_knowledge_document_")
            and isinstance(result, Mapping)
            and result.get("knowledge_document_id")
            else "/admin/ai/evaluations"
            if action_type == "admin_ai_evaluation_run"
            else "/admin/users/" + str(result.get("user_id"))
            if "user" in action_type and isinstance(result, Mapping) and result.get("user_id")
            else "/admin/stores"
        )
        return [
            {
                "kind": "operations_action_result",
                "icon": "✓" if succeeded else "!",
                "eyebrow": "受控操作回读",
                "title": str(operation_result.get("target_label") or "操作结果")[:100],
                "badge": "执行成功" if succeeded else "未执行",
                "tone": "" if succeeded else "warning",
                "summary": "结果来自提交后的业务主库回读。",
                "image_url": result.get("image_url") if isinstance(result, Mapping) else None,
                "rows": result_rows[:8],
                "action": {"label": "打开对应管理页面", "path": path},
            }
        ]

    guide = data.get("operation_guide")
    if intent == "operation_guide" and isinstance(guide, Mapping):
        steps = guide.get("steps")
        return [
            {
                "kind": "operation_guide",
                "icon": "导",
                "eyebrow": "操作指引",
                "title": str(guide.get("title") or "操作步骤")[:100],
                "badge": "只说明，未执行",
                "summary": "请在提交前再次核对目标、当前状态和变更内容。",
                "rows": [
                    {"label": f"步骤 {index}", "value": str(item)[:100], "meta": ""}
                    for index, item in enumerate(steps if isinstance(steps, list) else [], 1)
                ],
                "action": {
                    "label": str(guide.get("label") or "打开管理页面")[:40],
                    "path": str(guide.get("path") or "/admin"),
                },
            }
        ]

    metric_window = data.get("metric_window")
    business_metrics = data.get("business_metrics")
    if isinstance(metric_window, Mapping) and isinstance(business_metrics, Mapping):
        metric_labels = {
            "created_order_count": "创建订单",
            "paid_order_amount": "已支付成交额",
            "completed_net_amount": "已完成净额",
            "refunded_amount": "已退款金额",
            "new_user_count": "新增用户",
            "new_store_count": "新增店铺",
            "new_product_count": "新增上架商品",
        }
        metric_rows: list[dict[str, str]] = []
        for key, label in metric_labels.items():
            value = business_metrics.get(key)
            if isinstance(value, Mapping):
                display = str(value.get("display") or value.get("minor_units") or 0)
                meta = str(value.get("basis") or "")
            else:
                display = str(value or 0)
                meta = ""
            metric_rows.append({"label": label, "value": display, "meta": meta})
        return [
            {
                "kind": "admin_business_metrics",
                "icon": "数",
                "eyebrow": "平台经营指标",
                "title": f"近 {metric_window.get('days') or 7} 天经营快照",
                "badge": "实时主库口径",
                "rows": metric_rows,
                "action": {"label": "打开管理首页", "path": "/admin"},
            }
        ]

    dead_letters = data.get("dead_letters")
    if intent == "runtime" and isinstance(dead_letters, list):
        status_labels = {
            "open": "待处理",
            "replaying": "重放中",
            "resolved": "已解决",
            "ignored": "已忽略",
        }
        visible_dead_letters = [
            item
            for item in dead_letters
            if isinstance(item, Mapping)
            and (data.get("status_filter") != "open" or str(item.get("status") or "") == "open")
        ]
        return [
            {
                "kind": "admin_dead_letter",
                "icon": "!",
                "eyebrow": "失败事件治理",
                "title": str(item.get("event_type") or "未知事件")[:100],
                "badge": status_labels.get(str(item.get("status")), str(item.get("status"))),
                "tone": "warning" if item.get("status") == "open" else "",
                "summary": str(item.get("last_error") or "暂无安全错误摘要")[:180],
                "rows": [
                    {
                        "label": "死信编号",
                        "value": str(item.get("dead_letter_id") or "—"),
                    },
                    {
                        "label": "错误码",
                        "value": str(item.get("last_error_code") or "—"),
                    },
                    {
                        "label": "失败次数",
                        "value": f"{int(item.get('failure_count') or 0)} 次",
                    },
                    {
                        "label": "来源事件",
                        "value": str(item.get("source_id") or "—"),
                    },
                ],
                "action": {
                    "label": "检查与处置",
                    "path": (
                        "/admin/system/dead-letter-events/" + str(item.get("dead_letter_id") or "")
                    ),
                },
            }
            for item in visible_dead_letters[:8]
        ] or [
            {
                "kind": "admin_dead_letter_empty",
                "icon": "✓",
                "eyebrow": "失败事件治理",
                "title": (
                    "当前没有待处理死信事件"
                    if data.get("status_filter") == "open"
                    else "当前没有死信事件"
                ),
                "badge": "队列正常",
                "rows": [],
                "action": {
                    "label": "打开死信队列",
                    "path": "/admin/system/dead-letter-events",
                },
            }
        ]

    trace_runs = data.get("runs")
    if isinstance(trace_runs, list):
        run_status_labels = {
            "queued": "排队中",
            "running": "运行中",
            "completed": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
        }
        visible_limit = 3 if data.get("status_filter") == "failed" else 8
        return [
            {
                "kind": "admin_agent_trace",
                "icon": "链",
                "eyebrow": "真实执行链路",
                "title": str(item.get("agent_name") or "Agent Run"),
                "badge": run_status_labels.get(
                    str(item.get("status") or ""), str(item.get("status") or "未知")
                ),
                "tone": "warning" if item.get("status") == "failed" else "",
                "summary": f"Run {item.get('run_id')} · Trace {item.get('trace_id')}",
                "rows": [
                    {
                        "label": "领域委派",
                        "value": str(len(item.get("delegations") or [])),
                        "meta": "来自委派账本",
                    },
                    {
                        "label": "工具调用",
                        "value": str(len(item.get("tool_calls") or [])),
                        "meta": "来自工具审计",
                    },
                    {
                        "label": "当前阶段",
                        "value": str(item.get("phase") or "unknown"),
                        "meta": str(item.get("error_code") or item.get("degraded_reason") or ""),
                    },
                ],
                "action": {"label": "打开可观测性", "path": "/admin/observability"},
            }
            for item in trace_runs[:visible_limit]
            if isinstance(item, Mapping)
        ] or [
            {
                "kind": "admin_agent_trace_empty",
                "icon": "链",
                "eyebrow": "真实执行链路",
                "title": (
                    "最近没有失败的 Agent 运行"
                    if data.get("status_filter") == "failed"
                    else "没有匹配的运行记录"
                ),
                "badge": "空结果",
                "rows": [],
                "action": {"label": "打开可观测性", "path": "/admin/observability"},
            }
        ]

    if "agent_run_count" in data and "tool_call_count" in data:
        success_rate = data.get("agent_success_rate")
        tool_rate = data.get("tool_success_rate")
        return [
            {
                "kind": "admin_agent_cost_metrics",
                "icon": "测",
                "eyebrow": "Agent 运行指标",
                "title": "质量、延迟与成本采集",
                "badge": "部分成本覆盖",
                "tone": "warning" if data.get("cost_amount") is None else "",
                "rows": [
                    {"label": "运行次数", "value": str(data.get("agent_run_count") or 0)},
                    {
                        "label": "完成率",
                        "value": (
                            f"{float(success_rate) * 100:.1f}%"
                            if isinstance(success_rate, (int, float))
                            else "暂无样本"
                        ),
                    },
                    {"label": "工具调用", "value": str(data.get("tool_call_count") or 0)},
                    {
                        "label": "工具成功率",
                        "value": (
                            f"{float(tool_rate) * 100:.1f}%"
                            if isinstance(tool_rate, (int, float))
                            else "暂无样本"
                        ),
                    },
                    {
                        "label": "平均工具延迟",
                        "value": f"{int(data.get('average_tool_latency_ms') or 0)} ms",
                    },
                    {
                        "label": "Token 记录",
                        "value": str(data.get("recorded_delegation_tokens") or 0),
                        "meta": "供应商用量未完整落库，暂不计算金额",
                    },
                ],
                "action": {"label": "打开可观测性", "path": "/admin/observability"},
            }
        ]

    specialists = data.get("specialists")
    if isinstance(specialists, Mapping):
        specialist_intents = {
            "merchant_profile": "profile",
            "merchant_catalog": "catalog",
            "merchant_inventory": "inventory",
            "merchant_orders": "orders",
            "merchant_review_service": "reviews",
            "merchant_customer_service": "service",
            "merchant_policy": "policy",
            "merchant_after_sale": "after_sale",
            "governance_users": "users",
            "governance_stores": "stores",
            "governance_catalog": "catalog",
            "governance_orders": "orders",
            "governance_payments": "orders",
            "governance_logistics": "orders",
            "governance_metrics": "overview",
            "observability": "runtime",
            "governance_after_sale": "after_sale",
            "governance_support": "support",
            "governance_ai": "ai_governance",
        }
        specialist_results: dict[str, Mapping[str, Any]] = {}
        specialist_result_entries: list[tuple[str, Mapping[str, Any]]] = []
        for result in specialists.values():
            if not isinstance(result, Mapping):
                continue
            safe_data = result.get("data")
            specialist = str(result.get("specialist"))
            if specialist in specialist_intents and isinstance(safe_data, Mapping):
                specialist_results.setdefault(specialist, safe_data)
                specialist_result_entries.append((specialist, safe_data))
        specialist_cards: list[dict[str, object]] = []
        if context.audience == "merchant":
            trigger = getattr(context, "trigger", None)
            trigger_text = str(getattr(trigger, "text_content", "") or "")
            display_focus = _explicit_operations_display_focus(trigger_text)
            display_specialists = {
                "after_sale": "merchant_after_sale",
                "catalog": "merchant_catalog",
                "orders": "merchant_orders",
            }
            focused_specialist = display_specialists.get(display_focus or "")
            if focused_specialist in specialist_results:
                safe_data = specialist_results[focused_specialist]
                focused_cards = _operations_detail_cards(
                    context,
                    specialist_intents[focused_specialist],
                    safe_data,
                )
                if _requests_single_focused_card(
                    trigger_text,
                    display_focus or "",
                ):
                    focused_detail_cards = [
                        card
                        for card in focused_cards
                        if str(card.get("kind") or "").endswith("_item")
                    ]
                    if focused_detail_cards:
                        focused_cards = focused_detail_cards[:1]
                return (focused_cards + failed_subtasks)[:12]
            commerce_specialists = {
                "merchant_catalog",
                "merchant_inventory",
                "merchant_orders",
            }
            if not (set(specialist_results) & commerce_specialists):
                for specialist in (
                    "merchant_profile",
                    "merchant_after_sale",
                    "merchant_review_service",
                    "merchant_customer_service",
                    "merchant_policy",
                ):
                    safe_data = specialist_results.get(specialist)
                    if safe_data is not None:
                        specialist_cards.extend(
                            _operations_detail_cards(
                                context,
                                specialist_intents[specialist],
                                safe_data,
                            )
                        )
                return (specialist_cards + failed_subtasks)[:12]
            # A cross-domain stock diagnosis must not dump the first five products just because
            # the catalog specialist completed first.  Put risks and orders first; catalog cards
            # are useful only when no more specific operational result is available.
            catalog_data = specialist_results.get("merchant_catalog")
            inventory_data = specialist_results.get("merchant_inventory")
            orders_data = specialist_results.get("merchant_orders")
            products = (
                [
                    item
                    for item in catalog_data.get("on_sale_products", [])
                    if isinstance(item, Mapping)
                ]
                if isinstance(catalog_data, Mapping)
                else []
            )
            low_stock = (
                int(inventory_data.get("low_stock_sku_count", 0))
                if isinstance(inventory_data, Mapping)
                else 0
            )
            order_counts = (
                orders_data.get("order_status_counts") if isinstance(orders_data, Mapping) else {}
            )
            counts = order_counts if isinstance(order_counts, Mapping) else {}
            pending = sum(
                int(counts.get(key, 0)) for key in ("paid", "pending_shipment", "shipped")
            )
            revenue = (
                orders_data.get("completed_order_revenue")
                if isinstance(orders_data, Mapping)
                else None
            )
            revenue_display = (
                str(revenue.get("display", "¥0.00")) if isinstance(revenue, Mapping) else "¥0.00"
            )
            zero_sales = sum(int(product.get("sales_count", 0)) <= 0 for product in products)
            priorities: list[dict[str, str]] = []
            if low_stock:
                priorities.append(
                    {
                        "label": "优先 1 · 库存",
                        "value": f"处理 {low_stock} 个风险款式",
                        "meta": "避免缺货影响成交",
                    }
                )
            if pending:
                priorities.append(
                    {
                        "label": f"优先 {len(priorities) + 1} · 履约",
                        "value": f"跟进 {pending} 笔订单",
                        "meta": "先处理待发货与运输异常",
                    }
                )
            priorities.append(
                {
                    "label": f"优先 {len(priorities) + 1} · 商品",
                    "value": f"复盘 {len(products)} 件在售商品",
                    "meta": (
                        f"{zero_sales} 件暂无销量，检查信息与价格"
                        if zero_sales
                        else "复盘销量并优化在售信息"
                    ),
                }
            )
            priorities.append(
                {
                    "label": f"优先 {len(priorities) + 1} · 订单",
                    "value": f"已确认营业额 {revenue_display}",
                    "meta": "检查订单结构与待处理状态",
                }
            )
            priorities.append(
                {
                    "label": f"优先 {len(priorities) + 1} · 库存",
                    "value": "保持每日巡检" if not low_stock else "复核补货结果",
                    "meta": "当前无风险款式" if not low_stock else f"当前 {low_stock} 个风险款式",
                }
            )
            focus_value = data.get("priority_focus")
            focus = focus_value if isinstance(focus_value, int) and 1 <= focus_value <= 3 else None
            visible_priorities = (
                [priorities[focus - 1]]
                if focus is not None and len(priorities) >= focus
                else priorities[:3]
            )
            focused_label = str(visible_priorities[0].get("label", "")) if focus else ""
            compact_trigger = re.sub(r"\s+", "", trigger_text).casefold()
            priority_requested = (
                not trigger_text
                or focus is not None
                or any(
                    marker in compact_trigger
                    for marker in ("优先级", "先处理", "先做", "经营建议", "经营优先")
                )
            )
            if priority_requested:
                specialist_cards.append(
                    {
                        "kind": "merchant_priorities",
                        "icon": "策",
                        "eyebrow": "经营优先级",
                        "title": (f"当前只看第 {focus} 项" if focus else "今天先处理这三件事"),
                        "badge": "需要处理" if low_stock or pending else "经营建议",
                        "tone": "warning" if low_stock or pending else "",
                        "summary": (
                            "只展示本次追问对应的实时证据与处理入口。"
                            if focus
                            else "按实时商品、库存、订单和已确认营业额排序。"
                        ),
                        "rows": visible_priorities,
                        "action": {"label": "进入经营首页", "path": "/merchant/dashboard"},
                    }
                )
            if focus:
                focused_specialist = (
                    "merchant_inventory"
                    if "库存" in focused_label
                    else "merchant_orders"
                    if "履约" in focused_label or "订单" in focused_label
                    else "merchant_catalog"
                )
                focused_data = specialist_results.get(focused_specialist)
                if focused_data is not None:
                    specialist_cards.extend(
                        _operations_detail_cards(
                            context,
                            specialist_intents[focused_specialist],
                            focused_data,
                        )
                    )
                return (specialist_cards + failed_subtasks)[:12]
            primary_cards: list[dict[str, object]] = []
            merchant_detail_cards: list[dict[str, object]] = []
            for specialist in (
                "merchant_profile",
                "merchant_catalog",
                "merchant_inventory",
                "merchant_orders",
                "merchant_review_service",
                "merchant_customer_service",
                "merchant_policy",
                "merchant_after_sale",
            ):
                safe_data = specialist_results.get(specialist)
                if safe_data is not None:
                    domain_cards = _operations_detail_cards(
                        context, specialist_intents[specialist], safe_data
                    )
                    if domain_cards:
                        primary_cards.append(domain_cards[0])
                        if specialist != "merchant_catalog":
                            merchant_detail_cards.extend(domain_cards[1:])
            specialist_cards.extend(primary_cards)
            specialist_cards.extend(merchant_detail_cards[: max(0, 12 - len(specialist_cards))])
            if catalog_data is not None and len(specialist_cards) == 1:
                specialist_cards.extend(_operations_detail_cards(context, "catalog", catalog_data))
        else:
            admin_specialist_order: tuple[str, ...] = (
                "governance_metrics",
                "governance_payments",
                "governance_logistics",
                "observability",
                "governance_orders",
                "governance_stores",
                "governance_catalog",
                "governance_users",
                "governance_after_sale",
                "governance_support",
                "governance_ai",
            )
            admin_entries = [
                (specialist, safe_data)
                for ordered_specialist in admin_specialist_order
                for specialist, safe_data in specialist_result_entries
                if specialist == ordered_specialist
            ]
            trigger = getattr(context, "trigger", None)
            display_focus = _explicit_operations_display_focus(
                str(getattr(trigger, "text_content", "") or "")
            )
            display_specialists = {
                "after_sale": "governance_after_sale",
                "users": "governance_users",
                "stores": "governance_stores",
                "catalog": "governance_catalog",
                "orders": "governance_orders",
                "runtime": "observability",
            }
            focused_specialist = display_specialists.get(display_focus or "")
            if focused_specialist in specialist_results:
                admin_entries = [entry for entry in admin_entries if entry[0] == focused_specialist]
            focus_value = data.get("priority_focus")
            if isinstance(focus_value, int) and 1 <= focus_value <= len(admin_entries):
                admin_entries = [admin_entries[focus_value - 1]]
            requested_limit = data.get("requested_card_limit")
            if isinstance(requested_limit, int):
                admin_entries = admin_entries[: max(1, min(requested_limit, 4))]
            primary_cards = []
            admin_detail_cards: list[dict[str, object]] = []
            for specialist, safe_data in admin_entries:
                domain_cards = _operations_detail_cards(
                    context, specialist_intents[specialist], safe_data
                )
                if domain_cards:
                    primary_cards.append(domain_cards[0])
                    admin_detail_cards.extend(domain_cards[1:])
            specialist_cards.extend(primary_cards)
            specialist_cards.extend(admin_detail_cards[: max(0, 12 - len(primary_cards))])
        if specialist_cards or failed_subtasks:
            return (specialist_cards + failed_subtasks)[:12]

    def rows_from_counts(
        counts: object, labels: Mapping[str, str], *, maximum: int = 8
    ) -> list[dict[str, str]]:
        if not isinstance(counts, Mapping):
            return []
        return [
            {"label": labels.get(str(key), str(key)), "value": str(value)}
            for key, value in list(counts.items())[:maximum]
        ]

    order_labels = {
        "pending_payment": "待付款",
        "paid": "已付款",
        "pending_shipment": "待发货",
        "shipped": "运输中",
        "completed": "已完成",
        "cancelled": "已取消",
        "closed": "已关闭",
    }
    product_labels = {
        "draft": "草稿",
        "pending_review": "审核中",
        "on_sale": "销售中",
        "off_shelf": "已下架",
        "rejected": "需修改",
    }
    store_labels = {"active": "营业中", "suspended": "已暂停"}
    user_labels = {"active": "正常", "frozen": "冻结", "disabled": "停用", "deleted": "已删除"}

    if context.audience == "merchant":
        store_profile = data.get("store_profile")
        if isinstance(store_profile, Mapping):
            profile_status = str(store_profile.get("status") or "")
            return [
                {
                    "kind": "merchant_store_profile",
                    "icon": "店",
                    "eyebrow": "店铺资料",
                    "title": str(store_profile.get("store_name") or "本店"),
                    "badge": "营业中" if profile_status == "active" else "已暂停",
                    "tone": "" if profile_status == "active" else "warning",
                    "summary": str(store_profile.get("description") or "尚未填写店铺简介"),
                    "rows": [
                        {
                            "label": "店铺 Logo",
                            "value": "已设置" if store_profile.get("logo_configured") else "未设置",
                        },
                        {"label": "店铺评分", "value": str(store_profile.get("rating_score", 0))},
                        {"label": "累计评价", "value": str(store_profile.get("rating_count", 0))},
                        {"label": "累计销量", "value": str(store_profile.get("sales_count", 0))},
                        {"label": "收藏人数", "value": str(store_profile.get("follower_count", 0))},
                        {"label": "资料版本", "value": str(store_profile.get("version", 0))},
                    ],
                    "action": {"label": "编辑店铺资料", "path": "/merchant/store"},
                }
            ]
        if isinstance(data.get("today_revenue"), Mapping):
            today_revenue = data["today_revenue"]
            yesterday_revenue = data.get("yesterday_revenue")
            thirty_day_revenue = data.get("thirty_day_revenue")
            total_revenue = data.get("completed_order_revenue")
            unsettled_revenue = data.get("unsettled_paid_amount")

            def metric_value(value: object) -> str:
                return str(value.get("display", "¥0.00")) if isinstance(value, Mapping) else "¥0.00"

            merchant_revenue_card = {
                "kind": "merchant_revenue_metrics",
                "icon": "营",
                "eyebrow": "经营收入",
                "title": "本店已确认营业额",
                "badge": "实时口径",
                "summary": str(data.get("revenue_basis") or "仅统计已完成订单净收入"),
                "rows": [
                    {
                        "label": "今日收益",
                        "value": metric_value(today_revenue),
                        "meta": f"{today_revenue.get('completed_orders', 0)} 笔已完成订单",
                    },
                    {
                        "label": "昨日收益",
                        "value": metric_value(yesterday_revenue),
                        "meta": (
                            f"{yesterday_revenue.get('completed_orders', 0)} 笔已完成订单"
                            if isinstance(yesterday_revenue, Mapping)
                            else ""
                        ),
                    },
                    {
                        "label": "近 30 日收益",
                        "value": metric_value(thirty_day_revenue),
                        "meta": (
                            f"{thirty_day_revenue.get('completed_orders', 0)} 笔已完成订单"
                            if isinstance(thirty_day_revenue, Mapping)
                            else ""
                        ),
                    },
                    {"label": "累计营业额", "value": metric_value(total_revenue)},
                    {
                        "label": "已支付待确认",
                        "value": metric_value(unsettled_revenue),
                        "meta": "尚未计入营业额",
                    },
                ],
                "action": {"label": "查看店铺订单", "path": "/merchant/orders"},
            }
            if not isinstance(data.get("recent_orders"), list):
                return [merchant_revenue_card]
        recent_refunds = data.get("recent_refunds")
        if isinstance(recent_refunds, list):
            refund_labels = {
                "submitted": "待受理",
                "merchant_review": "待商家处理",
                "approved": "已同意",
                "waiting_return": "待顾客退货",
                "returning": "退货中",
                "received": "已收到退货",
                "refunding": "退款中",
                "succeeded": "退款成功",
                "rejected": "已拒绝",
                "cancelled": "已取消",
                "closed": "已关闭",
            }
            refund_next_steps = {
                "submitted": "先受理申请并核对顾客材料",
                "merchant_review": "核对原因、订单商品与履约情况后处理",
                "approved": "按售后类型等待退货或进入退款",
                "waiting_return": "等待顾客寄回商品",
                "returning": "跟踪退货包裹并等待收货",
                "received": "进入退款处理",
                "refunding": "等待退款结果",
                "succeeded": "退款已完成，无需继续处理",
                "rejected": "如顾客申诉，等待平台复核",
                "cancelled": "申请已取消，无需继续处理",
                "closed": "售后已关闭",
            }
            detail_mode = data.get("query_mode") == "after_sale_detail"
            refund_cards: list[dict[str, object]] = (
                []
                if detail_mode
                else [
                    {
                        "kind": "merchant_after_sale_overview",
                        "icon": "售",
                        "eyebrow": "售后待办",
                        "title": "本店售后申请",
                        "badge": f"共 {len(recent_refunds)} 条近期记录",
                        "rows": rows_from_counts(data.get("refund_status_counts"), refund_labels),
                        "action": {"label": "打开售后列表", "path": "/merchant/after-sales"},
                    }
                ]
            )
            for refund in recent_refunds[:5]:
                if not isinstance(refund, Mapping):
                    continue
                status = str(refund.get("status") or "")
                amount = refund.get("requested_amount")
                refund_cards.append(
                    {
                        "kind": "merchant_after_sale_item",
                        "icon": "售",
                        "eyebrow": "售后申请",
                        "title": str(refund.get("product_name") or "本店订单"),
                        "badge": refund_labels.get(status, status),
                        "tone": "warning" if status in {"submitted", "merchant_review"} else "",
                        "summary": str(
                            refund.get("reason_detail")
                            or refund.get("reason_code")
                            or "顾客申请售后"
                        ),
                        "rows": [
                            {"label": "售后单", "value": str(refund.get("refund_id") or "—")},
                            {"label": "订单", "value": str(refund.get("order_id") or "—")},
                            {"label": "顾客", "value": str(refund.get("customer_name") or "顾客")},
                            {
                                "label": "申请原因",
                                "value": str(
                                    refund.get("reason_detail")
                                    or refund.get("reason_code")
                                    or "顾客申请售后"
                                ),
                            },
                            {
                                "label": "申请金额",
                                "value": str(amount.get("display", "¥0.00"))
                                if isinstance(amount, Mapping)
                                else "¥0.00",
                            },
                            {"label": "状态", "value": refund_labels.get(status, status)},
                            {
                                "label": "下一步",
                                "value": refund_next_steps.get(status, "进入售后详情核对并处理"),
                            },
                        ],
                        "action": {
                            "label": "处理售后",
                            "path": f"/merchant/after-sales/{refund.get('refund_id')}",
                        },
                    }
                )
                if not detail_mode:
                    continue
                refund_id = str(refund.get("refund_id") or "")
                detail_path = f"/merchant/after-sales/{refund_id}"
                events = refund.get("events")
                event_values = (
                    [event for event in events if isinstance(event, Mapping)]
                    if isinstance(events, list)
                    else []
                )
                if event_values:
                    refund_cards.append(
                        {
                            "kind": "merchant_after_sale_timeline",
                            "icon": "流",
                            "eyebrow": "不可变售后事件",
                            "title": f"{refund_id} · 共 {len(event_values)} 个节点",
                            "badge": "最新在前",
                            "rows": [
                                {
                                    "label": refund_labels.get(
                                        str(event.get("to_status") or ""),
                                        str(event.get("to_status") or "售后事件"),
                                    ),
                                    "value": str(
                                        event.get("reason") or event.get("event_code") or "状态变更"
                                    ),
                                    "meta": (
                                        f"{event.get('actor_type') or 'system'} · "
                                        f"{_display_timestamp(event.get('occurred_at'))}"
                                    ),
                                }
                                for event in event_values[:20]
                            ],
                            "action": {"label": "处理售后", "path": detail_path},
                        }
                    )
                return_shipment = refund.get("return_shipment")
                if isinstance(return_shipment, Mapping):
                    refund_cards.append(
                        {
                            "kind": "merchant_return_shipment",
                            "icon": "退",
                            "eyebrow": "顾客退货物流",
                            "title": str(return_shipment.get("carrier_name") or "退货包裹"),
                            "badge": str(return_shipment.get("status") or "—"),
                            "rows": [
                                {
                                    "label": "退货运单",
                                    "value": str(return_shipment.get("tracking_no_masked") or "—"),
                                },
                                {
                                    "label": "寄出时间",
                                    "value": _display_timestamp(return_shipment.get("shipped_at"))
                                    or "尚未寄出",
                                },
                                {
                                    "label": "商家收货",
                                    "value": _display_timestamp(return_shipment.get("received_at"))
                                    or "尚未确认",
                                },
                            ],
                            "action": {"label": "处理售后", "path": detail_path},
                        }
                    )
                raw_refund_payments = refund.get("refund_payments")
                refund_payment_values = (
                    [item for item in raw_refund_payments if isinstance(item, Mapping)]
                    if isinstance(raw_refund_payments, list)
                    else []
                )
                for payment in refund_payment_values[:2]:
                    payment_amount = payment.get("amount")
                    payment_events = payment.get("events")
                    refund_cards.append(
                        {
                            "kind": "merchant_refund_payment",
                            "icon": "款",
                            "eyebrow": "退款支付事实",
                            "title": str(payment.get("refund_payment_id") or "退款支付单"),
                            "badge": str(payment.get("status") or "—"),
                            "rows": [
                                {
                                    "label": "退款金额",
                                    "value": str(
                                        payment_amount.get("display", "¥0.00")
                                        if isinstance(payment_amount, Mapping)
                                        else "¥0.00"
                                    ),
                                },
                                {
                                    "label": "渠道引用",
                                    "value": str(
                                        payment.get("provider_refund_no_masked") or "尚未生成"
                                    ),
                                },
                                {
                                    "label": "支付事件",
                                    "value": (
                                        f"{len(payment_events)} 条"
                                        if isinstance(payment_events, list)
                                        else "0 条"
                                    ),
                                },
                                {
                                    "label": "完成时间",
                                    "value": _display_timestamp(payment.get("completed_at"))
                                    or "尚未完成",
                                },
                            ],
                            "action": {"label": "处理售后", "path": detail_path},
                        }
                    )
                raw_appeals = refund.get("appeals")
                appeal_values = (
                    [item for item in raw_appeals if isinstance(item, Mapping)]
                    if isinstance(raw_appeals, list)
                    else []
                )
                for appeal in appeal_values[:2]:
                    appeal_events = appeal.get("events")
                    refund_cards.append(
                        {
                            "kind": "merchant_refund_appeal",
                            "icon": "申",
                            "eyebrow": "售后申诉",
                            "title": str(appeal.get("appeal_id") or "售后申诉"),
                            "badge": str(appeal.get("status") or "—"),
                            "summary": str(
                                appeal.get("resolution_detail")
                                or appeal.get("reason")
                                or "等待平台复核"
                            ),
                            "rows": [
                                {
                                    "label": "申诉事件",
                                    "value": (
                                        f"{len(appeal_events)} 条"
                                        if isinstance(appeal_events, list)
                                        else "0 条"
                                    ),
                                },
                                {
                                    "label": "提交时间",
                                    "value": _display_timestamp(appeal.get("submitted_at")),
                                },
                                {
                                    "label": "处理时间",
                                    "value": _display_timestamp(appeal.get("decided_at"))
                                    or "尚未处理",
                                },
                            ],
                            "action": {"label": "处理售后", "path": detail_path},
                        }
                    )
            return refund_cards[:10]
        if intent == "reviews":
            review_cards: list[dict[str, object]] = [
                {
                    "kind": "merchant_reviews",
                    "icon": "评",
                    "eyebrow": "评价经营",
                    "title": "本店评价与回复",
                    "badge": f"待回复 {int(data.get('pending_reply_count', 0))}",
                    "tone": "warning" if int(data.get("pending_reply_count", 0)) else "",
                    "rows": [
                        {
                            "label": "已发布评价",
                            "value": str(data.get("published_review_count", 0)),
                        },
                        {"label": "已回复", "value": str(data.get("replied_review_count", 0))},
                        {"label": "平均评分", "value": str(data.get("average_rating", 0))},
                    ],
                    "action": {"label": "处理商品评价", "path": "/merchant/products"},
                }
            ]
            review_values = (
                data.get("recent_reviews")
                if data.get("query_mode") == "history"
                else data.get("pending_reviews")
            )
            for review in review_values if isinstance(review_values, list) else []:
                if not isinstance(review, Mapping):
                    continue
                rating = int(review.get("rating", 0))
                has_reply = bool(review.get("has_reply"))
                review_cards.append(
                    {
                        "kind": "merchant_review_item",
                        "icon": "评",
                        "eyebrow": "历史评价" if has_reply else "待回复评价",
                        "title": str(review.get("product_name") or "商品评价"),
                        "badge": "已回复" if has_reply else f"{rating} 星",
                        "tone": "warning" if rating <= 3 else "",
                        "summary": (
                            f"顾客: {review.get('content') or '未填写文字评价'}; "
                            f"商家回复: {review.get('reply_content')}"
                            if has_reply
                            else str(review.get("content") or "顾客未填写文字评价")
                        ),
                        "rows": [
                            {"label": "顾客", "value": str(review.get("customer_name") or "顾客")},
                            {"label": "评分", "value": "★" * max(0, min(rating, 5))},
                        ],
                        "action": {
                            "label": "查看并回复",
                            "path": f"/merchant/products/{review.get('product_id')}",
                        },
                    }
                )
            return review_cards[:6]
        if intent == "service":
            service_cards: list[dict[str, object]] = [
                {
                    "kind": "merchant_service",
                    "icon": "客",
                    "eyebrow": "顾客服务",
                    "title": "顾客咨询与人工队列",
                    "badge": f"待接待 {int(data.get('waiting_human_count', 0))}",
                    "tone": "warning" if int(data.get("waiting_human_count", 0)) else "",
                    "rows": rows_from_counts(
                        data.get("ticket_status_counts"),
                        {
                            "queued": "排队中",
                            "assigned": "待接入",
                            "active": "人工处理中",
                            "resolved": "已解决",
                            "closed": "已关闭",
                        },
                    ),
                    "action": {"label": "打开顾客消息", "path": "/merchant/messages"},
                }
            ]
            conversation_values = data.get("conversations")
            selected_conversation = data.get("selected_conversation")
            if isinstance(selected_conversation, Mapping):
                recent_messages = selected_conversation.get("recent_messages")
                active_contexts = selected_conversation.get("active_contexts")
                message_values = (
                    [item for item in recent_messages if isinstance(item, Mapping)]
                    if isinstance(recent_messages, list)
                    else []
                )
                service_cards.append(
                    {
                        "kind": "merchant_customer_conversation_context",
                        "icon": "聊",
                        "eyebrow": "顾客会话上下文",
                        "title": str(selected_conversation.get("customer_name") or "顾客"),
                        "badge": (
                            "人工服务"
                            if selected_conversation.get("service_mode") == "human"
                            else "AI 接待"
                        ),
                        "summary": "已按会话顺序读取最近消息，回复前仍会重新核对关联订单或商品。",
                        "rows": [
                            {
                                "label": (
                                    "顾客"
                                    if item.get("sender") == "user"
                                    else "店铺人员"
                                    if item.get("sender") == "human"
                                    else "店铺 AI"
                                    if item.get("sender") == "agent"
                                    else "系统"
                                ),
                                "value": str(
                                    item.get("text")
                                    or (
                                        "[商品卡片]"
                                        if item.get("type") == "product_card"
                                        else "[订单卡片]"
                                    )
                                )[:180],
                                "meta": str(item.get("sent_at") or ""),
                            }
                            for item in message_values[-8:]
                        ],
                        "footer": (
                            f"当前绑定 {len(active_contexts)} 个业务上下文"
                            if isinstance(active_contexts, list)
                            else "当前没有业务上下文"
                        ),
                        "action": {"label": "进入顾客会话", "path": "/merchant/messages"},
                    }
                )
                reply_draft = data.get("reply_draft")
                if isinstance(reply_draft, Mapping) and reply_draft.get("content"):
                    service_cards.append(
                        {
                            "kind": "merchant_customer_reply_draft",
                            "icon": "拟",
                            "eyebrow": "可编辑回复草稿",
                            "title": str(reply_draft.get("customer_name") or "顾客"),
                            "badge": "尚未发送",
                            "tone": "warning",
                            "summary": str(reply_draft.get("content") or "")[:2000],
                            "rows": [
                                {"label": "状态", "value": "仅预览，未发送给顾客"},
                                {"label": "发送规则", "value": "进入会话核对、编辑后再发送"},
                            ],
                            "action": {
                                "label": "进入会话继续编辑",
                                "path": "/merchant/messages",
                            },
                        }
                    )
                return service_cards[:3]
            if data.get("query_mode") == "conversation_list" and isinstance(
                conversation_values, list
            ):
                for conversation in conversation_values[:8]:
                    if not isinstance(conversation, Mapping):
                        continue
                    unread = int(conversation.get("unread_count", 0))
                    mode = str(conversation.get("service_mode") or "ai")
                    service_cards.append(
                        {
                            "kind": "merchant_customer_conversation",
                            "icon": "客",
                            "eyebrow": "顾客会话",
                            "title": str(conversation.get("customer_name") or "顾客"),
                            "badge": (
                                f"{unread} 条未读"
                                if unread
                                else "人工处理中"
                                if mode == "human"
                                else "AI 接待中"
                            ),
                            "tone": "warning" if unread or mode == "human" else "",
                            "summary": str(conversation.get("last_message_preview") or "暂无消息"),
                            "rows": [
                                {
                                    "label": "接待状态",
                                    "value": "人工服务" if mode == "human" else "AI 接待",
                                },
                                {"label": "未读消息", "value": str(unread)},
                            ],
                            "action": {"label": "进入会话", "path": "/merchant/messages"},
                        }
                    )
                return service_cards[:9]
            active_tickets = data.get("active_tickets")
            ticket_labels = {
                "queued": "等待接待",
                "assigned": "待接入",
                "active": "沟通中",
                "waiting_user": "等待顾客",
            }
            for ticket in active_tickets if isinstance(active_tickets, list) else []:
                if not isinstance(ticket, Mapping):
                    continue
                status = str(ticket.get("status") or "queued")
                service_cards.append(
                    {
                        "kind": "merchant_service_ticket",
                        "icon": "客",
                        "eyebrow": "人工服务",
                        "title": str(ticket.get("customer_name") or "顾客咨询"),
                        "badge": ticket_labels.get(status, status),
                        "tone": "warning" if status in {"queued", "assigned"} else "",
                        "summary": str(ticket.get("summary") or "顾客请求人工协助"),
                        "rows": [
                            {"label": "优先级", "value": str(ticket.get("priority") or "普通")},
                            {"label": "状态", "value": ticket_labels.get(status, status)},
                        ],
                        "action": {"label": "进入会话", "path": "/merchant/messages"},
                    }
                )
            return service_cards[:6]
        if intent == "policy":
            policies = data.get("published_policies")
            values = (
                [item for item in policies if isinstance(item, Mapping)]
                if isinstance(policies, list)
                else []
            )
            policy_cards: list[dict[str, object]] = [
                {
                    "kind": "merchant_policy",
                    "icon": "规",
                    "eyebrow": "店铺政策",
                    "title": "当前已发布服务政策",
                    "badge": f"{len(values)} 项",
                    "rows": [
                        {
                            "label": str(item.get("policy_type") or "政策"),
                            "value": str(item.get("title") or "未命名政策"),
                        }
                        for item in values[:8]
                    ],
                    "action": {"label": "打开店铺资料", "path": "/merchant/store"},
                }
            ]
            knowledge_sources = data.get("knowledge_sources")
            for source in knowledge_sources if isinstance(knowledge_sources, list) else []:
                if not isinstance(source, Mapping):
                    continue
                policy_cards.append(
                    {
                        "kind": "merchant_platform_policy_source",
                        "icon": "规",
                        "eyebrow": "平台商家规则",
                        "title": str(source.get("title") or "平台规则"),
                        "badge": f"版本 {source.get('version') or '当前'}",
                        "summary": str(source.get("excerpt") or "已发布平台规则"),
                        "rows": [
                            {
                                "label": "检索方式",
                                "value": str(
                                    (data.get("rag") or {}).get("retrieval_mode", "hybrid")
                                    if isinstance(data.get("rag"), Mapping)
                                    else "hybrid"
                                ),
                            }
                        ],
                        "action": {"label": "查看店铺资料", "path": "/merchant/store"},
                    }
                )
            return policy_cards[:5]
        if intent == "catalog":
            product_detail = data.get("product_detail")
            if isinstance(product_detail, Mapping):
                product_id = str(product_detail.get("product_id") or "")
                sku_values = product_detail.get("skus")
                detail_skus = (
                    [item for item in sku_values if isinstance(item, Mapping)]
                    if isinstance(sku_values, list)
                    else []
                )
                attribute_values = product_detail.get("attributes")
                attributes = (
                    [item for item in attribute_values if isinstance(item, Mapping)]
                    if isinstance(attribute_values, list)
                    else []
                )
                content = product_detail.get("content")
                faq_values = product_detail.get("faqs")
                faqs = (
                    [item for item in faq_values if isinstance(item, Mapping)]
                    if isinstance(faq_values, list)
                    else []
                )
                ocr_values = product_detail.get("ocr_results")
                ocr_results = (
                    [item for item in ocr_values if isinstance(item, Mapping)]
                    if isinstance(ocr_values, list)
                    else []
                )
                completed_ocr_count = sum(item.get("status") == "completed" for item in ocr_results)
                fulfillment = product_detail.get("fulfillment")
                readiness = product_detail.get("submission_readiness")
                cards: list[dict[str, object]] = [
                    {
                        "kind": "merchant_product_editable_overview",
                        "icon": "商",
                        "eyebrow": "商品当前版本",
                        "title": str(product_detail.get("name") or "商品"),
                        "badge": product_labels.get(
                            str(product_detail.get("status") or ""),
                            str(product_detail.get("status") or "状态待确认"),
                        ),
                        "summary": (
                            f"已售 {product_detail.get('sales_count', 0)} · "
                            f"评分 {product_detail.get('rating_score', 0)} · "
                            f"商品版本 {product_detail.get('version', 0)}"
                        ),
                        "rows": [
                            {
                                "label": str(sku.get("name") or "默认款式"),
                                "value": str(
                                    (sku.get("price") or {}).get("display", "价格待核对")
                                    if isinstance(sku.get("price"), Mapping)
                                    else "价格待核对"
                                ),
                                "meta": (
                                    f"可售 {(sku.get('inventory') or {}).get('available', 0)}"
                                    if isinstance(sku.get("inventory"), Mapping)
                                    else "库存待核对"
                                ),
                            }
                            for sku in detail_skus[:8]
                        ],
                        "action": {
                            "label": "打开商品编辑",
                            "path": f"/merchant/products/{product_id}",
                        },
                    },
                    {
                        "kind": "merchant_product_content_status",
                        "icon": "详",
                        "eyebrow": "详情与 OCR",
                        "title": "商品详情资料",
                        "badge": (
                            str(content.get("status") or "当前版本")
                            if isinstance(content, Mapping)
                            else "未填写"
                        ),
                        "summary": (
                            str(content.get("safe_text") or "尚未填写商品详情")[:320]
                            if isinstance(content, Mapping)
                            else "尚未填写商品详情"
                        ),
                        "rows": [
                            {
                                "label": "结构化参数",
                                "value": f"{len(attributes)} 项",
                            },
                            {
                                "label": "详情块",
                                "value": (
                                    f"{len(content.get('blocks') or [])} 项"
                                    if isinstance(content, Mapping)
                                    and isinstance(content.get("blocks"), list)
                                    else "0 项"
                                ),
                            },
                            {
                                "label": "OCR 文件",
                                "value": f"{len(ocr_results)} 个",
                                "meta": f"完成 {completed_ocr_count}",
                            },
                        ],
                        "action": {
                            "label": "编辑详情与图片说明",
                            "path": f"/merchant/products/{product_id}",
                        },
                    },
                    {
                        "kind": "merchant_product_faq_status",
                        "icon": "问",
                        "eyebrow": "常见问题",
                        "title": f"共 {len(faqs)} 组问题与回答",
                        "badge": (
                            f"已发布 {sum(item.get('status') == 'published' for item in faqs)}"
                        ),
                        "rows": [
                            {
                                "label": str(item.get("question") or "问题")[:80],
                                "value": str(item.get("answer") or "尚未填写回答")[:120],
                                "meta": product_labels.get(
                                    str(item.get("status") or ""),
                                    str(item.get("status") or "草稿"),
                                ),
                            }
                            for item in faqs[:6]
                        ],
                        "action": {
                            "label": "编辑常见问题",
                            "path": f"/merchant/products/{product_id}",
                        },
                    },
                ]
                if isinstance(fulfillment, Mapping):
                    cards.append(
                        {
                            "kind": "merchant_product_fulfillment",
                            "icon": "运",
                            "eyebrow": "发货与购买须知",
                            "title": "当前履约资料",
                            "badge": "已配置",
                            "rows": [
                                {
                                    "label": "发货地区",
                                    "value": str(fulfillment.get("origin_region_code") or "未填写"),
                                },
                                {
                                    "label": "预计发货",
                                    "value": (
                                        f"{fulfillment.get('dispatch_min_hours', 0)} 至 "
                                        f"{fulfillment.get('dispatch_max_hours', 0)} 小时"
                                    ),
                                },
                                {
                                    "label": "购买须知",
                                    "value": str(fulfillment.get("purchase_notice") or "未填写")[
                                        :160
                                    ],
                                },
                            ],
                            "action": {
                                "label": "编辑履约资料",
                                "path": f"/merchant/products/{product_id}",
                            },
                        }
                    )
                if isinstance(readiness, Mapping):
                    readiness_labels = {
                        "basic": "商品基础信息",
                        "sku": "有效款式",
                        "sku_images": "每个款式的安全图片",
                        "fulfillment": "发货与购买须知",
                        "detail_content": "通过扫描的商品详情",
                    }
                    checks = readiness.get("checks")
                    cards.append(
                        {
                            "kind": "merchant_product_submission_readiness",
                            "icon": "审",
                            "eyebrow": "提交审核预检",
                            "title": "商品资料可以提交"
                            if readiness.get("ready")
                            else "还有资料需要补齐",
                            "badge": "预检通过" if readiness.get("ready") else "暂不可提交",
                            "tone": "" if readiness.get("ready") else "warning",
                            "summary": str(readiness.get("note") or "确认时会再次读取最新版本。"),
                            "rows": [
                                {
                                    "label": readiness_labels.get(str(key), str(key)),
                                    "value": "已满足" if value else "未满足",
                                }
                                for key, value in (
                                    checks.items() if isinstance(checks, Mapping) else []
                                )
                            ],
                            "action": {
                                "label": "继续完善商品",
                                "path": f"/merchant/products/{product_id}",
                            },
                        }
                    )
                return cards
            candidate_values = data.get("candidate_products")
            if data.get("query_mode") == "product_selection" and isinstance(candidate_values, list):
                return [
                    {
                        "kind": "merchant_product_candidate",
                        "icon": "商",
                        "eyebrow": "请选择商品",
                        "title": str(item.get("name") or "商品"),
                        "badge": product_labels.get(
                            str(item.get("status") or ""),
                            str(item.get("status") or "状态待确认"),
                        ),
                        "summary": f"商品版本 {item.get('version', 0)}",
                        "rows": [],
                        "action": {
                            "label": "查看完整编辑内容",
                            "path": f"/merchant/products/{item.get('product_id')}",
                        },
                    }
                    for item in candidate_values[:8]
                    if isinstance(item, Mapping)
                ]
            product_values = data.get("on_sale_products")
            products = (
                [item for item in product_values if isinstance(item, Mapping)]
                if isinstance(product_values, list)
                else []
            )
            zero_sales = sum(int(item.get("sales_count", 0)) <= 0 for item in products)
            product_cards: list[dict[str, object]] = [
                {
                    "kind": "merchant_catalog_overview",
                    "icon": "商",
                    "eyebrow": "商品经营",
                    "title": "本店商品状态与在售表现",
                    "badge": f"在售 {len(products)} 件",
                    "tone": "warning" if zero_sales else "",
                    "summary": (
                        f"其中 {zero_sales} 件暂无销量，建议核对标题、图片、价格和款式库存。"
                        if zero_sales
                        else "已按实时商品状态与销量完成核对。"
                    ),
                    "rows": rows_from_counts(data.get("product_status_counts"), product_labels),
                    "action": {"label": "进入商品管理", "path": "/merchant/products"},
                }
            ]
            for product in products:
                if not isinstance(product, Mapping):
                    continue
                sku_rows: list[dict[str, str]] = []
                product_skus = product.get("skus")
                for sku in product_skus if isinstance(product_skus, list) else []:
                    if not isinstance(sku, Mapping):
                        continue
                    price = sku.get("price")
                    inventory = sku.get("inventory")
                    available = (
                        inventory.get("available", 0) if isinstance(inventory, Mapping) else 0
                    )
                    sku_rows.append(
                        {
                            "label": str(sku.get("name") or "默认款式"),
                            "value": str(
                                price.get("display") if isinstance(price, Mapping) else "价格待核对"
                            ),
                            "meta": f"可售 {available}",
                        }
                    )
                product_cards.append(
                    {
                        "kind": "merchant_product",
                        "icon": "商",
                        "eyebrow": "在售商品",
                        "title": str(product.get("name") or "商品"),
                        "badge": f"已售 {product.get('sales_count', 0)}",
                        "tone": "",
                        "rows": sku_rows[:6],
                        "action": {
                            "label": "编辑商品",
                            "path": f"/merchant/products/{product.get('product_id')}",
                        },
                    }
                )
            return product_cards[:6]
        if intent == "inventory":
            all_inventory = data.get("inventory_skus")
            if isinstance(all_inventory, list):
                inventory_cards: list[dict[str, object]] = []
                for item in all_inventory[:8]:
                    if not isinstance(item, Mapping):
                        continue
                    available = int(item.get("available_quantity", 0))
                    inventory_cards.append(
                        {
                            "kind": "merchant_inventory_item",
                            "icon": "库",
                            "eyebrow": "款式库存",
                            "title": str(item.get("product_name") or "商品"),
                            "badge": f"可售 {available}",
                            "tone": "warning"
                            if available <= int(item.get("safety_stock_quantity", 0))
                            else "",
                            "summary": str(item.get("sku_name") or "默认款式"),
                            "rows": [
                                {
                                    "label": "现有库存",
                                    "value": str(item.get("on_hand_quantity", 0)),
                                },
                                {"label": "已预占", "value": str(item.get("reserved_quantity", 0))},
                                {"label": "实时可售", "value": str(available)},
                                {
                                    "label": "安全库存线",
                                    "value": str(item.get("safety_stock_quantity", 0)),
                                },
                            ],
                            "action": {
                                "label": "编辑该商品",
                                "path": f"/merchant/products/{item.get('product_id')}",
                            },
                        }
                    )
                return inventory_cards or [
                    {
                        "kind": "merchant_inventory_item",
                        "icon": "库",
                        "eyebrow": "款式库存",
                        "title": "本店暂无可管理款式",
                        "badge": "空数据",
                        "rows": [],
                        "action": {"label": "进入商品管理", "path": "/merchant/products"},
                    }
                ]
            low_count = int(data.get("low_stock_sku_count", 0))
            risks = data.get("low_stock_skus")
            risk_items = (
                [item for item in risks if isinstance(item, Mapping)]
                if isinstance(risks, list)
                else []
            )
            if not risk_items:
                return [
                    {
                        "kind": "inventory_risk",
                        "icon": "库",
                        "eyebrow": "库存守卫",
                        "title": "当前没有低库存或缺货款式",
                        "badge": "状态良好",
                        "tone": "",
                        "summary": "已按每个款式的安全库存线核对实时可售库存。",
                        "rows": [{"label": "风险款式", "value": "0", "meta": "实时核对"}],
                        "action": {"label": "进入商品管理", "path": "/merchant/products"},
                    }
                ]
            risk_cards: list[dict[str, object]] = []
            for item in risk_items[:5]:
                available = int(item.get("available_quantity", 0))
                safety = int(item.get("safety_stock_quantity", 0))
                product_id = str(item.get("product_id") or "")
                risk_cards.append(
                    {
                        "kind": "inventory_risk",
                        "icon": "库",
                        "eyebrow": "缺货款式" if available <= 0 else "低库存款式",
                        "title": str(item.get("product_name") or "商品"),
                        "badge": "已缺货" if available <= 0 else f"仅剩 {available}",
                        "tone": "warning",
                        "summary": str(item.get("sku_name") or "默认款式"),
                        "rows": [
                            {"label": "实时可售", "value": str(available)},
                            {"label": "安全库存线", "value": str(safety)},
                        ],
                        "action": {
                            "label": "编辑该商品",
                            "path": (
                                f"/merchant/products/{product_id}"
                                if product_id
                                else "/merchant/products"
                            ),
                        },
                    }
                )
            if low_count > len(risk_cards):
                risk_cards[-1]["summary"] = (
                    f"另有 {low_count - len(risk_cards)} 个风险款式，请进入商品管理继续查看。"
                )
            return risk_cards
        order_rows = rows_from_counts(data.get("order_status_counts"), order_labels)
        revenue = data.get("completed_order_revenue")
        unsettled = data.get("unsettled_paid_amount")
        if isinstance(revenue, Mapping):
            order_rows.insert(
                0, {"label": "已确认营业额", "value": str(revenue.get("display", "¥0.00"))}
            )
        if isinstance(unsettled, Mapping):
            order_rows.insert(
                1, {"label": "待确认收货金额", "value": str(unsettled.get("display", "¥0.00"))}
            )
        low_stock = int(data.get("low_stock_sku_count", 0))
        if low_stock:
            order_rows.insert(
                2,
                {
                    "label": "低库存或缺货款式",
                    "value": str(low_stock),
                    "meta": "建议优先处理",
                },
            )
        merchant_cards: list[dict[str, object]] = []
        if merchant_revenue_card is not None:
            merchant_cards.append(merchant_revenue_card)
        merchant_cards.append(
            {
                "kind": "merchant_overview",
                "icon": "营",
                "eyebrow": "经营快照",
                "title": context.store.store_name if context.store else "本店经营概况",
                "badge": "需要处理" if low_stock else "实时",
                "tone": "warning" if low_stock else "",
                "rows": order_rows[:8]
                or rows_from_counts(data.get("product_status_counts"), product_labels),
                "action": {
                    "label": "查看本店订单" if intent == "orders" else "进入经营首页",
                    "path": "/merchant/orders" if intent == "orders" else "/merchant/dashboard",
                },
            }
        )
        if intent == "orders":
            recent_orders = data.get("recent_orders")
            trigger = getattr(context, "trigger", None)
            request_text = (getattr(trigger, "text_content", "") or "").replace(" ", "")
            requested_statuses: set[str] = set()
            if "待发货" in request_text:
                requested_statuses.add("pending_shipment")
            if any(marker in request_text for marker in ("运输中", "在途", "物流中")):
                requested_statuses.add("shipped")
            if any(marker in request_text for marker in ("已完成", "完成订单")):
                requested_statuses.add("completed")
            for order in recent_orders if isinstance(recent_orders, list) else []:
                if not isinstance(order, Mapping):
                    continue
                status = str(order.get("status") or "")
                if requested_statuses and status not in requested_statuses:
                    continue
                amount = order.get("amount")
                merchant_cards.append(
                    {
                        "kind": "merchant_order_item",
                        "icon": "单",
                        "eyebrow": "本店订单",
                        "title": str(order.get("product_name") or "本店订单"),
                        "badge": order_labels.get(status, status),
                        "tone": "warning" if status == "pending_shipment" else "",
                        "summary": (
                            f"{order.get('sku_name') or '默认款式'}"
                            + (f"，{order.get('quantity')} 件" if order.get("quantity") else "")
                        ),
                        "rows": [
                            {"label": "顾客", "value": str(order.get("customer_name") or "顾客")},
                            {
                                "label": "实付",
                                "value": str(
                                    amount.get("display", "¥0.00")
                                    if isinstance(amount, Mapping)
                                    else "¥0.00"
                                ),
                            },
                        ],
                        "action": {"label": "处理订单", "path": "/merchant/orders"},
                    }
                )
        return merchant_cards[:7]

    payment_values = data.get("payments")
    if intent == "orders" and isinstance(payment_values, list):
        payment_status_labels = {
            "created": "已创建",
            "pending": "支付确认中",
            "succeeded": "支付成功",
            "failed": "支付失败",
            "closed": "已关闭",
            "partially_refunded": "部分退款",
            "refunded": "已退款",
        }
        callback_status_labels = {
            "received": "已接收",
            "processed": "已处理",
            "duplicate": "重复回调",
            "rejected": "已拒绝",
            "failed": "处理失败",
        }
        signature_status_labels = {
            "valid": "验签通过",
            "invalid": "验签失败",
            "error": "验签异常",
        }
        payment_cards: list[dict[str, object]] = []
        for item in payment_values[:8]:
            if not isinstance(item, Mapping):
                continue
            payment_id = str(item.get("payment_id") or "")
            status = str(item.get("status") or "")
            customer_name = str(item.get("customer_name") or "未知顾客")
            orders = item.get("orders")
            order_values = (
                [value for value in orders if isinstance(value, Mapping)]
                if isinstance(orders, list)
                else []
            )
            store_names = "、".join(
                dict.fromkeys(str(value.get("store_name") or "未知店铺") for value in order_values)
            )
            requested_amount = item.get("requested_amount")
            paid_amount = item.get("paid_amount")
            refunded_amount = item.get("refunded_amount")
            events = item.get("events")
            event_values = (
                [value for value in events if isinstance(value, Mapping)]
                if isinstance(events, list)
                else []
            )
            callbacks = item.get("callbacks")
            callback_values = (
                [value for value in callbacks if isinstance(value, Mapping)]
                if isinstance(callbacks, list)
                else []
            )
            payment_cards.append(
                {
                    "kind": "admin_payment",
                    "icon": "付",
                    "eyebrow": "平台支付事实",
                    "title": f"支付单 {payment_id}",
                    "badge": payment_status_labels.get(status, status),
                    "tone": "warning" if status in {"failed", "pending"} else "",
                    "summary": (
                        f"顾客 {customer_name} · {store_names or '暂无关联店铺'} · "
                        f"{len(order_values)} 笔店铺订单"
                    ),
                    "rows": [
                        {
                            "label": "应付 / 实付",
                            "value": (
                                f"{requested_amount.get('display', '¥0.00')} / "
                                f"{paid_amount.get('display', '¥0.00')}"
                                if isinstance(requested_amount, Mapping)
                                and isinstance(paid_amount, Mapping)
                                else "—"
                            ),
                        },
                        {
                            "label": "已退款",
                            "value": str(
                                refunded_amount.get("display", "¥0.00")
                                if isinstance(refunded_amount, Mapping)
                                else "¥0.00"
                            ),
                        },
                        {
                            "label": "渠道 / 方式",
                            "value": (
                                f"{item.get('provider') or '—'} / "
                                f"{item.get('payment_method') or '—'}"
                            ),
                        },
                        {
                            "label": "渠道引用",
                            "value": str(item.get("provider_trade_no_masked") or "尚未生成"),
                        },
                        {
                            "label": "事件 / 回调",
                            "value": f"{len(event_values)} 条 / {len(callback_values)} 条",
                        },
                        {
                            "label": "创建时间",
                            "value": str(item.get("created_at") or "—")[:19].replace("T", " "),
                        },
                    ],
                    "action": {
                        "label": "打开支付详情",
                        "path": f"/admin/payments/{payment_id}",
                    },
                }
            )
            if data.get("query_mode") != "payment_detail":
                continue
            if event_values:
                payment_cards.append(
                    {
                        "kind": "admin_payment_timeline",
                        "icon": "流",
                        "eyebrow": "不可变支付事件",
                        "title": f"{payment_id} · 共 {len(event_values)} 个节点",
                        "badge": "最新在前",
                        "rows": [
                            {
                                "label": payment_status_labels.get(
                                    str(event.get("to_status") or ""),
                                    str(event.get("to_status") or "支付事件"),
                                ),
                                "value": str(event.get("event_type") or "状态变更"),
                                "meta": (
                                    f"{event.get('source_type') or 'system'} · "
                                    f"{str(event.get('occurred_at') or '')[:19].replace('T', ' ')}"
                                ),
                            }
                            for event in event_values[:20]
                        ],
                        "action": {
                            "label": "打开支付详情",
                            "path": f"/admin/payments/{payment_id}",
                        },
                    }
                )
            if callback_values:
                payment_cards.append(
                    {
                        "kind": "admin_payment_callbacks",
                        "icon": "验",
                        "eyebrow": "渠道回调核验",
                        "title": f"{payment_id} · 共 {len(callback_values)} 次回调",
                        "badge": "仅显示安全字段",
                        "rows": [
                            {
                                "label": callback_status_labels.get(
                                    str(callback.get("process_status") or ""),
                                    str(callback.get("process_status") or "回调"),
                                ),
                                "value": signature_status_labels.get(
                                    str(callback.get("signature_status") or ""),
                                    str(callback.get("signature_status") or "验签未知"),
                                ),
                                "meta": (
                                    f"尝试 {callback.get('attempt_count') or 0} 次 · "
                                    f"{_display_timestamp(callback.get('received_at'))}"
                                ),
                            }
                            for callback in callback_values[:12]
                        ],
                        "action": {
                            "label": "打开支付详情",
                            "path": f"/admin/payments/{payment_id}",
                        },
                    }
                )
        return payment_cards or [
            {
                "kind": "admin_payment_empty",
                "icon": "付",
                "eyebrow": "平台支付",
                "title": "当前没有匹配的支付记录",
                "badge": "空结果",
                "rows": [],
                "action": {"label": "打开支付管理", "path": "/admin/payments"},
            }
        ]

    shipment_values = data.get("shipments")
    if intent == "orders" and isinstance(shipment_values, list):
        shipment_status_labels = {
            "created": "待揽收",
            "picked_up": "已揽收",
            "in_transit": "运输中",
            "delivered": "已签收",
            "exception": "物流异常",
            "returned": "已退回",
            "closed": "已关闭",
        }
        shipment_cards: list[dict[str, object]] = []
        for item in shipment_values[:8]:
            if not isinstance(item, Mapping):
                continue
            status = str(item.get("status") or "")
            latest = item.get("latest_track")
            latest_track = latest if isinstance(latest, Mapping) else {}
            shipment_product_items = item.get("items")
            product_values = (
                [value for value in shipment_product_items if isinstance(value, Mapping)]
                if isinstance(shipment_product_items, list)
                else []
            )
            product_summary = "、".join(
                f"{value.get('product_name') or '商品'} x{value.get('quantity') or 0}"
                for value in product_values[:3]
            )
            if len(product_values) > 3:
                product_summary += f" 等 {len(product_values)} 项"
            destination = item.get("destination")
            destination_value = ""
            if isinstance(destination, Mapping):
                destination_value = " / ".join(
                    str(destination.get(key) or "")
                    for key in ("province_code", "city_code", "district_code")
                    if destination.get(key)
                )
                address_detail = str(destination.get("address") or "")
                if address_detail:
                    destination_value = f"{destination_value} {address_detail}".strip()
            shipment_id = str(item.get("shipment_id") or "")
            rows: list[dict[str, str]] = [
                {"label": "订单", "value": str(item.get("order_id") or "—")},
                {
                    "label": "店铺 / 顾客",
                    "value": (
                        f"{item.get('store_name') or '未知店铺'} / "
                        f"{item.get('customer_name') or '未知顾客'}"
                    ),
                },
                {"label": "承运商", "value": str(item.get("carrier_name") or "—")},
                {"label": "运单号", "value": str(item.get("tracking_no_masked") or "—")},
            ]
            if product_summary:
                rows.append({"label": "包裹内容", "value": product_summary})
            if latest_track:
                rows.append(
                    {
                        "label": "最新轨迹",
                        "value": str(latest_track.get("description") or "暂无轨迹"),
                        "meta": str(latest_track.get("location") or ""),
                    }
                )
            if destination_value:
                rows.append({"label": "目的地", "value": destination_value})
            shipment_cards.append(
                {
                    "kind": "admin_shipment",
                    "icon": "运",
                    "eyebrow": "平台物流 · 商城模拟"
                    if item.get("is_simulated")
                    else "平台物流 · 承运商同步",
                    "title": f"包裹 {shipment_id}",
                    "badge": shipment_status_labels.get(status, status),
                    "tone": "warning" if status == "exception" else "",
                    "summary": (
                        "每个节点均来自物流轨迹表，不按固定时间自动推进。"
                        if item.get("is_simulated")
                        else "轨迹来自承运商同步结果。"
                    ),
                    "rows": rows,
                    "action": {
                        "label": "打开物流详情",
                        "path": f"/admin/shipments/{shipment_id}",
                    },
                }
            )
            tracks = item.get("tracks")
            track_values = (
                [value for value in tracks if isinstance(value, Mapping)]
                if isinstance(tracks, list)
                else []
            )
            if data.get("query_mode") == "shipment_detail" and track_values:
                shipment_cards.append(
                    {
                        "kind": "admin_shipment_timeline",
                        "icon": "轨",
                        "eyebrow": "不可变物流轨迹",
                        "title": f"{shipment_id} · 共 {len(track_values)} 个节点",
                        "badge": "最新在前",
                        "rows": [
                            {
                                "label": shipment_status_labels.get(
                                    str(track.get("status") or ""),
                                    str(track.get("provider_status") or "物流节点"),
                                ),
                                "value": str(track.get("description") or "—"),
                                "meta": (
                                    f"{track.get('location') or '位置未填写'} · "
                                    f"{str(track.get('occurred_at') or '')[:19].replace('T', ' ')}"
                                ),
                            }
                            for track in track_values[:12]
                        ],
                        "action": {
                            "label": "打开物流详情",
                            "path": f"/admin/shipments/{shipment_id}",
                        },
                    }
                )
        return shipment_cards or [
            {
                "kind": "admin_shipment_empty",
                "icon": "运",
                "eyebrow": "平台物流",
                "title": "当前没有匹配的包裹",
                "badge": "空结果",
                "rows": [],
                "action": {"label": "打开订单管理", "path": "/admin/orders"},
            }
        ]

    after_sale_values = data.get("refunds")
    if intent == "after_sale" and isinstance(after_sale_values, list):
        refund_status_labels = {
            "submitted": "待受理",
            "merchant_review": "商家审核中",
            "approved": "已同意",
            "waiting_return": "待顾客退货",
            "returning": "退货中",
            "received": "商家已收货",
            "refunding": "退款中",
            "succeeded": "退款成功",
            "rejected": "已拒绝",
            "cancelled": "已取消",
            "closed": "已关闭",
        }
        appeal_status_labels = {
            "submitted": "待受理",
            "reviewing": "复核中",
            "upheld": "申诉成立",
            "rejected": "申诉驳回",
            "cancelled": "已取消",
            "closed": "已关闭",
        }
        admin_after_sale_cards: list[dict[str, object]] = []
        for refund in after_sale_values[:6]:
            if not isinstance(refund, Mapping):
                continue
            refund_id = str(refund.get("refund_id") or "")
            status = str(refund.get("status") or "")
            requested_amount = refund.get("requested_amount")
            approved_amount = refund.get("approved_amount")
            items = refund.get("items")
            item_values = (
                [item for item in items if isinstance(item, Mapping)]
                if isinstance(items, list)
                else []
            )
            item_summary = "、".join(
                f"{item.get('product_name') or '商品'} x{item.get('quantity') or 0}"
                for item in item_values[:3]
            )
            admin_after_sale_cards.append(
                {
                    "kind": "admin_after_sale_case",
                    "icon": "售",
                    "eyebrow": (
                        f"{refund.get('store_name') or '平台售后'} · "
                        f"{refund.get('customer_name') or '顾客'}"
                    ),
                    "title": item_summary or f"售后单 {refund_id}",
                    "badge": refund_status_labels.get(status, status),
                    "tone": (
                        "warning"
                        if status in {"submitted", "merchant_review", "returning", "refunding"}
                        else ""
                    ),
                    "summary": str(
                        refund.get("reason_detail") or refund.get("reason_code") or "售后申请"
                    ),
                    "image_url": (
                        str(item_values[0].get("image_url"))
                        if item_values and item_values[0].get("image_url")
                        else None
                    ),
                    "rows": [
                        {"label": "售后单", "value": refund_id},
                        {"label": "订单", "value": str(refund.get("order_id") or "—")},
                        {
                            "label": "申请 / 核准金额",
                            "value": (
                                f"{requested_amount.get('display', '¥0.00')} / "
                                f"{approved_amount.get('display', '¥0.00')}"
                                if isinstance(requested_amount, Mapping)
                                and isinstance(approved_amount, Mapping)
                                else "—"
                            ),
                        },
                        {
                            "label": "类型",
                            "value": (
                                "退货退款"
                                if refund.get("refund_type") == "return_and_refund"
                                else "仅退款"
                            ),
                        },
                        {
                            "label": "提交时间",
                            "value": _display_timestamp(refund.get("submitted_at")),
                        },
                    ],
                    "action": {
                        "label": "打开售后详情",
                        "path": f"/admin/refund-applications/{refund_id}",
                    },
                }
            )
            if data.get("query_mode") != "after_sale_detail":
                continue
            events = refund.get("events")
            event_values = (
                [event for event in events if isinstance(event, Mapping)]
                if isinstance(events, list)
                else []
            )
            if event_values:
                admin_after_sale_cards.append(
                    {
                        "kind": "admin_after_sale_timeline",
                        "icon": "流",
                        "eyebrow": "不可变售后事件",
                        "title": f"{refund_id} · 共 {len(event_values)} 个节点",
                        "badge": "最新在前",
                        "rows": [
                            {
                                "label": refund_status_labels.get(
                                    str(event.get("to_status") or ""),
                                    str(event.get("to_status") or "售后事件"),
                                ),
                                "value": str(event.get("reason") or event.get("event_code") or "—"),
                                "meta": (
                                    f"{event.get('actor_type') or 'system'} · "
                                    f"{_display_timestamp(event.get('occurred_at'))}"
                                ),
                            }
                            for event in event_values[:20]
                        ],
                        "action": {
                            "label": "打开售后详情",
                            "path": f"/admin/refund-applications/{refund_id}",
                        },
                    }
                )
            return_shipment = refund.get("return_shipment")
            if isinstance(return_shipment, Mapping):
                admin_after_sale_cards.append(
                    {
                        "kind": "admin_return_shipment",
                        "icon": "退",
                        "eyebrow": "退货物流",
                        "title": str(return_shipment.get("carrier_name") or "退货包裹"),
                        "badge": str(return_shipment.get("status") or "—"),
                        "rows": [
                            {
                                "label": "退货运单",
                                "value": str(return_shipment.get("tracking_no_masked") or "—"),
                            },
                            {
                                "label": "寄出时间",
                                "value": _display_timestamp(return_shipment.get("shipped_at"))
                                or "尚未寄出",
                            },
                            {
                                "label": "商家收货",
                                "value": _display_timestamp(return_shipment.get("received_at"))
                                or "尚未确认",
                            },
                        ],
                        "action": {
                            "label": "打开售后详情",
                            "path": f"/admin/refund-applications/{refund_id}",
                        },
                    }
                )
            refund_payments = refund.get("refund_payments")
            for payment in (refund_payments if isinstance(refund_payments, list) else [])[:2]:
                if not isinstance(payment, Mapping):
                    continue
                amount = payment.get("amount")
                payment_events = payment.get("events")
                payment_event_values = (
                    [event for event in payment_events if isinstance(event, Mapping)]
                    if isinstance(payment_events, list)
                    else []
                )
                admin_after_sale_cards.append(
                    {
                        "kind": "admin_refund_payment",
                        "icon": "款",
                        "eyebrow": "退款支付事实",
                        "title": str(payment.get("refund_payment_id") or "退款支付单"),
                        "badge": str(payment.get("status") or "—"),
                        "rows": [
                            {
                                "label": "退款金额",
                                "value": str(
                                    amount.get("display", "¥0.00")
                                    if isinstance(amount, Mapping)
                                    else "¥0.00"
                                ),
                            },
                            {
                                "label": "渠道引用",
                                "value": str(
                                    payment.get("provider_refund_no_masked") or "尚未生成"
                                ),
                            },
                            {
                                "label": "支付事件",
                                "value": f"{len(payment_event_values)} 条",
                                "meta": (
                                    "最近事件验签通过"
                                    if payment_event_values
                                    and payment_event_values[0].get("signature_valid")
                                    else "暂无已验签成功事件"
                                ),
                            },
                            {
                                "label": "完成时间",
                                "value": _display_timestamp(payment.get("completed_at"))
                                or "尚未完成",
                            },
                        ],
                        "action": {
                            "label": "打开售后详情",
                            "path": f"/admin/refund-applications/{refund_id}",
                        },
                    }
                )
            appeals = refund.get("appeals")
            for appeal in (appeals if isinstance(appeals, list) else [])[:2]:
                if not isinstance(appeal, Mapping):
                    continue
                appeal_id = str(appeal.get("appeal_id") or "")
                appeal_events = appeal.get("events")
                appeal_event_values = (
                    [event for event in appeal_events if isinstance(event, Mapping)]
                    if isinstance(appeal_events, list)
                    else []
                )
                admin_after_sale_cards.append(
                    {
                        "kind": "admin_refund_appeal",
                        "icon": "诉",
                        "eyebrow": "售后申诉",
                        "title": appeal_id or "售后申诉",
                        "badge": appeal_status_labels.get(
                            str(appeal.get("status") or ""),
                            str(appeal.get("status") or "—"),
                        ),
                        "summary": str(appeal.get("reason") or "用户申请平台复核"),
                        "rows": [
                            {
                                "label": "处理结果",
                                "value": str(
                                    appeal.get("resolution_detail")
                                    or appeal.get("resolution_code")
                                    or "尚未处理"
                                ),
                            },
                            {
                                "label": "申诉事件",
                                "value": f"{len(appeal_event_values)} 条",
                            },
                            {
                                "label": "提交时间",
                                "value": _display_timestamp(appeal.get("submitted_at")),
                            },
                        ],
                        "action": {
                            "label": "打开申诉详情",
                            "path": f"/admin/refund-appeals/{appeal_id}",
                        },
                    }
                )
        return admin_after_sale_cards or [
            {
                "kind": "admin_after_sale_empty",
                "icon": "售",
                "eyebrow": "平台售后",
                "title": "当前没有匹配的售后记录",
                "badge": "空结果",
                "rows": [],
                "action": {
                    "label": "打开售后治理",
                    "path": "/admin/refund-applications",
                },
            }
        ]

    card_specs = {
        "users": (
            "用",
            "用户治理",
            "平台用户状态",
            data.get("user_status_counts"),
            user_labels,
            "/admin/users",
        ),
        "stores": (
            "店",
            "店铺治理",
            "店铺与商品状态",
            data.get("store_status_counts"),
            store_labels,
            "/admin/stores",
        ),
        "catalog": (
            "商",
            "商品治理",
            "平台商品状态",
            data.get("product_status_counts"),
            product_labels,
            "/admin/stores",
        ),
        "orders": (
            "单",
            "交易履约",
            "平台订单状态",
            data.get("order_status_counts"),
            order_labels,
            "/admin/orders",
        ),
        "runtime": (
            "AI",
            "运行诊断",
            "Agent 与事件链路",
            data,
            {
                "pending_outbox_events": "待投递事件",
                "stale_pending_outbox_events": "超过5分钟未投递",
                "failed_agent_runs_24h": "24小时失败运行",
                "successful_runs_after_latest_failure": "故障后成功运行",
                "unrecovered_agent_failures": "未恢复故障",
            },
            "/admin/observability",
        ),
        "after_sale": (
            "退",
            "售后治理",
            "平台售后状态",
            data.get("refund_status_counts"),
            {
                "submitted": "待受理",
                "merchant_review": "商家审核中",
                "approved": "已同意",
                "waiting_return": "待退货",
                "returning": "退货中",
                "refunding": "退款中",
                "succeeded": "退款成功",
                "rejected": "已拒绝",
                "closed": "已关闭",
            },
            "/admin/refunds",
        ),
        "support": (
            "客",
            "客服治理",
            "平台人工服务队列",
            data.get("ticket_status_counts"),
            {
                "queued": "排队中",
                "assigned": "待接入",
                "active": "处理中",
                "waiting_user": "等待对方",
                "resolved": "已解决",
                "closed": "已关闭",
            },
            "/admin/messages",
        ),
        "ai_governance": (
            "AI",
            "AI 治理",
            "Agent 与知识状态",
            data.get("agent_run_status_counts"),
            {
                "queued": "排队运行",
                "running": "运行中",
                "completed": "已完成",
                "failed": "失败",
                "cancelled": "已取消",
            },
            "/admin/ai",
        ),
    }
    if intent == "stores" and isinstance(data.get("service_profile"), Mapping):
        profile = data["service_profile"]
        missing_items = profile.get("missing_items")
        missing_values = missing_items if isinstance(missing_items, list) else []
        origin_values = profile.get("origin_region_codes")
        origins = origin_values if isinstance(origin_values, list) else []
        templates_value = profile.get("shipping_templates")
        templates = templates_value if isinstance(templates_value, list) else []
        policies_value = profile.get("after_sale_policies")
        policies = policies_value if isinstance(policies_value, list) else []
        default_carrier = profile.get("default_carrier")
        carrier_info = default_carrier if isinstance(default_carrier, Mapping) else {}
        recent_carriers_value = carrier_info.get("recent_carriers")
        recent_carriers = recent_carriers_value if isinstance(recent_carriers_value, list) else []
        dispatch_min = profile.get("dispatch_min_hours")
        dispatch_max = profile.get("dispatch_max_hours")
        dispatch_display = (
            f"{dispatch_min}-{dispatch_max} 小时"
            if dispatch_min is not None and dispatch_max is not None
            else "未配置"
        )
        carrier_display = "发货创建包裹时确定"
        carrier_meta = (
            "近期使用："
            + "、".join(
                str(item.get("carrier_name") or item.get("carrier_code") or "")
                for item in recent_carriers[:3]
                if isinstance(item, Mapping)
            )
            if recent_carriers
            else str(carrier_info.get("reason") or "当前没有历史包裹样本")
        )
        return [
            {
                "kind": "admin_store_service_profile",
                "icon": "服",
                "eyebrow": "店铺服务资料",
                "title": str(profile.get("store_name") or "店铺"),
                "badge": "资料完整" if not missing_values else f"缺少 {len(missing_values)} 项",
                "tone": "" if not missing_values else "warning",
                "summary": (
                    "主库中的店铺介绍、商品履约、配送模板与售后政策已全部核对。"
                    if not missing_values
                    else f"建议补充：{'、'.join(str(item) for item in missing_values)}。"
                ),
                "rows": [
                    {
                        "label": "店铺简介",
                        "value": str(profile.get("description") or "未填写")[:240],
                    },
                    {
                        "label": "发货地",
                        "value": "、".join(str(item) for item in origins) if origins else "未配置",
                        "meta": (
                            f"{profile.get('fulfillment_configured_count', 0)}/"
                            f"{profile.get('product_count', 0)} 个商品已配置履约资料"
                        ),
                    },
                    {"label": "发货时效", "value": dispatch_display},
                    {
                        "label": "配送模板",
                        "value": (
                            "、".join(
                                str(item.get("template_name") or "未命名模板")
                                for item in templates[:4]
                                if isinstance(item, Mapping)
                            )
                            if templates
                            else "无有效模板"
                        ),
                    },
                    {"label": "默认快递", "value": carrier_display, "meta": carrier_meta},
                    {
                        "label": "售后政策",
                        "value": (
                            "、".join(
                                str(item.get("title") or item.get("policy_type") or "售后政策")
                                for item in policies[:4]
                                if isinstance(item, Mapping)
                            )
                            if policies
                            else "未发布"
                        ),
                    },
                ],
                "footer": "默认快递不是店铺级静态配置，实际承运商以已创建包裹为准。",
                "action": {
                    "label": "打开店铺治理",
                    "path": f"/admin/stores/{profile.get('store_id') or ''}",
                },
            }
        ]
    if intent in card_specs:
        icon, eyebrow, title, status_counts, labels, path = card_specs[intent]
        rows = rows_from_counts(status_counts, labels)
        warning = any(
            int(row["value"]) > 0
            for row in rows
            if row["value"].isdigit()
            and row["label"]
            in {"冻结", "停用", "已暂停", "待发货", "待投递事件", "24小时失败运行", "未恢复故障"}
        )
        admin_cards: list[dict[str, object]] = [
            {
                "kind": f"admin_{intent}",
                "icon": icon,
                "eyebrow": eyebrow,
                "title": title,
                "badge": "需要关注" if warning else "已核对",
                "tone": "warning" if warning else "",
                "rows": rows,
                "action": {"label": "打开管理页面", "path": path},
            }
        ]
        if intent == "orders" and data.get("query_mode") in {"list", "detail"}:
            return admin_cards
        detail_key = {
            "users": "recent_users",
            "stores": "stores",
            "orders": "recent_orders",
            "after_sale": "recent_refunds",
            "catalog": "products",
            "inventory": "products",
        }.get(intent)
        details = data.get(detail_key) if detail_key else None
        for item in details if isinstance(details, list) else []:
            if not isinstance(item, Mapping):
                continue
            if intent == "users":
                status = str(item.get("status") or "")
                wallet = item.get("wallet")
                admin_cards.append(
                    {
                        "kind": "admin_user_item",
                        "icon": "用",
                        "eyebrow": "用户",
                        "title": str(item.get("username") or "用户"),
                        "badge": user_labels.get(status, status),
                        "tone": "warning" if status != "active" else "",
                        "rows": [
                            {
                                "label": "在线状态",
                                "value": (
                                    "在线" if item.get("online_status") == "online" else "离线"
                                ),
                                "meta": f"{item.get('active_session_count', 0)} 个有效会话",
                            },
                            {
                                "label": "账户余额",
                                "value": str(
                                    wallet.get("display", "¥0.00")
                                    if isinstance(wallet, Mapping)
                                    else "¥0.00"
                                ),
                            },
                            {
                                "label": "有效订单",
                                "value": str(item.get("visible_order_count", 0)),
                            },
                            {
                                "label": "收货地址",
                                "value": str(item.get("address_count", 0)),
                            },
                            {
                                "label": "购物车商品",
                                "value": str(item.get("cart_item_count", 0)),
                            },
                            {
                                "label": "收藏与关注",
                                "value": str(
                                    int(item.get("product_favorite_count", 0))
                                    + int(item.get("store_follow_count", 0))
                                ),
                            },
                            {
                                "label": "最近登录",
                                "value": str(item.get("last_login_at") or "暂无记录"),
                            },
                        ],
                        "action": {"label": "管理用户", "path": "/admin/users"},
                    }
                )
            elif intent in {"stores", "catalog", "inventory"}:
                if intent in {"catalog", "inventory"} and item.get("product_name"):
                    status = str(item.get("status") or "")
                    price_minor = item.get("minimum_price")
                    admin_cards.append(
                        {
                            "kind": "admin_product_item",
                            "icon": "商",
                            "eyebrow": str(item.get("store_name") or "平台商品"),
                            "title": str(item.get("product_name") or "商品"),
                            "badge": product_labels.get(status, status),
                            "tone": "warning" if status != "on_sale" else "",
                            "rows": [
                                {"label": "累计销量", "value": str(item.get("sales_count", 0))},
                                {
                                    "label": "最低售价",
                                    "value": (
                                        _money_display(price_minor, "CNY")
                                        if isinstance(price_minor, int)
                                        else "暂无在售款式"
                                    ),
                                },
                            ],
                            "action": {"label": "管理所属店铺", "path": "/admin/stores"},
                        }
                    )
                    continue
                status = str(item.get("status") or "")
                revenue = item.get("completed_revenue")
                admin_cards.append(
                    {
                        "kind": "admin_store_item",
                        "icon": "店",
                        "eyebrow": "店铺",
                        "title": str(item.get("store_name") or "店铺"),
                        "badge": store_labels.get(status, status),
                        "tone": "warning" if status != "active" else "",
                        "rows": [
                            {
                                "label": "店主账号",
                                "value": str(item.get("owner_username") or "未找到"),
                            },
                            {"label": "商品数量", "value": str(item.get("product_count", 0))},
                            {"label": "累计销量", "value": str(item.get("sales_count", 0))},
                            {
                                "label": "已确认营业额",
                                "value": str(
                                    revenue.get("display", "¥0.00")
                                    if isinstance(revenue, Mapping)
                                    else "¥0.00"
                                ),
                            },
                            {"label": "店铺评分", "value": str(item.get("rating", 0))},
                            {
                                "label": "进行中售后",
                                "value": str(item.get("pending_after_sale_count", 0)),
                            },
                        ],
                        "action": {"label": "管理店铺", "path": "/admin/stores"},
                    }
                )
            elif intent == "orders":
                status = str(item.get("status") or "")
                amount = item.get("amount")
                admin_cards.append(
                    {
                        "kind": "admin_order_item",
                        "icon": "单",
                        "eyebrow": "平台订单",
                        "title": str(item.get("store_name") or "订单"),
                        "badge": order_labels.get(status, status),
                        "tone": "warning" if status == "pending_shipment" else "",
                        "summary": f"顾客 {item.get('customer_name') or '未知'}",
                        "rows": [
                            {
                                "label": "实付",
                                "value": str(
                                    amount.get("display", "¥0.00")
                                    if isinstance(amount, Mapping)
                                    else "¥0.00"
                                ),
                            }
                        ],
                        "action": {"label": "查看所属店铺", "path": "/admin/stores"},
                    }
                )
            elif intent == "after_sale":
                status = str(item.get("status") or "")
                amount = item.get("amount")
                admin_cards.append(
                    {
                        "kind": "admin_refund_item",
                        "icon": "退",
                        "eyebrow": "售后申请",
                        "title": str(item.get("store_name") or "售后申请"),
                        "badge": {
                            "submitted": "待受理",
                            "merchant_review": "商家审核中",
                            "approved": "已同意",
                            "waiting_return": "待退货",
                            "returning": "退货中",
                            "refunding": "退款中",
                            "succeeded": "退款成功",
                            "rejected": "已拒绝",
                        }.get(status, status),
                        "tone": "warning" if status in {"submitted", "merchant_review"} else "",
                        "summary": str(
                            item.get("reason_detail") or item.get("reason_code") or "顾客申请售后"
                        ),
                        "rows": [
                            {
                                "label": "顾客",
                                "value": str(item.get("customer_name") or "未知"),
                            },
                            {
                                "label": "订单",
                                "value": str(item.get("order_id") or "未知"),
                            },
                            {
                                "label": "申请金额",
                                "value": str(
                                    amount.get("display", "¥0.00")
                                    if isinstance(amount, Mapping)
                                    else "¥0.00"
                                ),
                            },
                        ],
                        "action": {"label": "打开售后治理", "path": "/admin/refunds"},
                    }
                )
        if intent == "catalog" and isinstance(data.get("selected_product"), Mapping):
            selected_product = data["selected_product"]
            selected_skus = selected_product.get("skus")
            selected_attributes = selected_product.get("attributes")
            selected_ocr = selected_product.get("ocr_results")
            selected_faqs = selected_product.get("faqs")
            selected_store = selected_product.get("store")
            admin_cards.append(
                {
                    "kind": "admin_product_editor",
                    "icon": "编",
                    "eyebrow": (
                        str(selected_store.get("store_name") or "平台商品")
                        if isinstance(selected_store, Mapping)
                        else "平台商品"
                    ),
                    "title": str(selected_product.get("name") or "商品"),
                    "badge": product_labels.get(
                        str(selected_product.get("status") or ""),
                        str(selected_product.get("status") or ""),
                    ),
                    "rows": [
                        {
                            "label": "款式",
                            "value": str(len(selected_skus))
                            if isinstance(selected_skus, list)
                            else "0",
                        },
                        {
                            "label": "商品参数",
                            "value": str(len(selected_attributes))
                            if isinstance(selected_attributes, list)
                            else "0",
                        },
                        {
                            "label": "详情 OCR",
                            "value": str(len(selected_ocr))
                            if isinstance(selected_ocr, list)
                            else "0",
                        },
                        {
                            "label": "常见问题",
                            "value": str(len(selected_faqs))
                            if isinstance(selected_faqs, list)
                            else "0",
                        },
                        {
                            "label": "发货资料",
                            "value": "已配置" if selected_product.get("fulfillment") else "未配置",
                        },
                        {"label": "资源版本", "value": str(selected_product.get("version", 0))},
                    ],
                    "action": {"label": "进入店铺商品管理", "path": "/admin/stores"},
                }
            )
        if intent == "ai_governance":
            requested_asset = data.get("requested_governance_asset")
            if requested_asset == "evaluations":
                active_dataset = data.get("active_dataset")
                registered = data.get("registered_comparison")
                if isinstance(active_dataset, Mapping):
                    admin_cards.append(
                        {
                            "kind": "admin_evaluation_dataset",
                            "icon": "测",
                            "eyebrow": "固定发布测试集",
                            "title": str(active_dataset.get("dataset_id") or "AI 评估数据集"),
                            "badge": str(active_dataset.get("dataset_version") or "未登记"),
                            "rows": [
                                {
                                    "label": "固定用例",
                                    "value": f"{int(active_dataset.get('case_count') or 0)} 个",
                                },
                                {
                                    "label": "数据集哈希",
                                    "value": (
                                        str(active_dataset.get("dataset_sha256") or "未知")[:16]
                                        + "…"
                                    ),
                                },
                                {
                                    "label": "生产基线",
                                    "value": (
                                        str(registered.get("baseline_version") or "未登记")
                                        if isinstance(registered, Mapping)
                                        else "未登记"
                                    ),
                                },
                                {
                                    "label": "候选版本",
                                    "value": (
                                        str(registered.get("candidate_version") or "未登记")
                                        if isinstance(registered, Mapping)
                                        else "未登记"
                                    ),
                                },
                            ],
                            "action": {"label": "打开 AI 评估", "path": "/admin/ai/evaluations"},
                        }
                    )
                gate_labels = {
                    "pass": "通过发布门禁",
                    "fail": "发布门禁失败",
                    "insufficient_evidence": "证据不足",
                }
                status_labels = {
                    "queued": "排队中",
                    "running": "采集中",
                    "completed": "已完成",
                    "failed": "运行失败",
                    "cancelled": "已取消",
                }
                evaluations = data.get("evaluations")
                for evaluation in evaluations if isinstance(evaluations, list) else []:
                    if not isinstance(evaluation, Mapping):
                        continue
                    metrics = evaluation.get("metrics")
                    metric_values = metrics if isinstance(metrics, Mapping) else {}
                    gate = str(evaluation.get("release_gate") or "")
                    status = str(evaluation.get("status") or "unknown")
                    reasons = evaluation.get("reasons")
                    reason_values = (
                        [str(item) for item in reasons] if isinstance(reasons, list) else []
                    )
                    admin_cards.append(
                        {
                            "kind": "admin_ai_evaluation",
                            "icon": "评",
                            "eyebrow": "AI 发布评估",
                            "title": str(evaluation.get("evaluation_id") or "评估运行"),
                            "badge": gate_labels.get(gate, status_labels.get(status, status)),
                            "tone": (
                                "warning"
                                if gate in {"fail", "insufficient_evidence"} or status == "failed"
                                else ""
                            ),
                            "summary": (
                                "阻断原因: " + "、".join(reason_values[:3])
                                if reason_values
                                else "当前没有记录阻断原因。"
                            ),
                            "rows": [
                                {
                                    "label": "候选通过率",
                                    "value": _percentage_display(
                                        metric_values.get("candidate_pass_rate")
                                    ),
                                },
                                {
                                    "label": "工具选择正确率",
                                    "value": _percentage_display(
                                        metric_values.get("candidate_tool_accuracy")
                                    ),
                                },
                                {
                                    "label": "引用正确率",
                                    "value": _percentage_display(
                                        metric_values.get("candidate_citation_accuracy")
                                    ),
                                },
                                {
                                    "label": "回答正确率",
                                    "value": _percentage_display(
                                        metric_values.get("candidate_answer_accuracy")
                                    ),
                                },
                            ],
                            "footer": f"Trace: {evaluation.get('trace_id') or '未记录'}",
                            "action": {"label": "打开 AI 评估", "path": "/admin/ai/evaluations"},
                        }
                    )
            documents = data.get("documents")
            if requested_asset == "knowledge_documents" and isinstance(documents, list):
                document_labels = {
                    "draft": "草稿",
                    "published": "已发布",
                    "withdrawn": "已撤回",
                }
                job_labels = {
                    "created": "待调度",
                    "queued": "排队中",
                    "running": "索引中",
                    "succeeded": "索引成功",
                    "failed": "索引失败",
                    "cancelled": "已取消",
                }
                for document in documents:
                    if not isinstance(document, Mapping):
                        continue
                    status = str(document.get("status") or "unknown")
                    latest_job = document.get("latest_index_job")
                    job_status = (
                        str(latest_job.get("status") or "")
                        if isinstance(latest_job, Mapping)
                        else ""
                    )
                    document_id = str(document.get("document_id") or "")
                    admin_cards.append(
                        {
                            "kind": "admin_knowledge_document",
                            "icon": "知",
                            "eyebrow": (
                                "平台知识"
                                if document.get("scope_type") == "platform"
                                else f"店铺知识 · {document.get('scope_name') or '未知店铺'}"
                            ),
                            "title": str(document.get("title") or "知识文档"),
                            "badge": document_labels.get(status, status),
                            "tone": "warning" if status != "published" else "",
                            "summary": f"文档编号 {document_id}",
                            "rows": [
                                {
                                    "label": "内容版本",
                                    "value": str(document.get("content_version") or "未知"),
                                },
                                {
                                    "label": "正文规模",
                                    "value": f"{int(document.get('character_count') or 0)} 字符",
                                },
                                {
                                    "label": "最近索引",
                                    "value": job_labels.get(job_status, job_status or "尚未创建"),
                                    "meta": (
                                        str(latest_job.get("job_id") or "")
                                        if isinstance(latest_job, Mapping)
                                        else None
                                    ),
                                },
                                {
                                    "label": "更新时间",
                                    "value": str(document.get("updated_at") or "未知"),
                                },
                            ],
                            "action": {
                                "label": "打开知识文档",
                                "path": f"/admin/knowledge/documents/{document_id}",
                            },
                        }
                    )
                    index_jobs = document.get("index_jobs")
                    if (
                        data.get("query_mode") == "knowledge_document_detail"
                        and isinstance(index_jobs, list)
                        and index_jobs
                    ):
                        admin_cards.append(
                            {
                                "kind": "admin_knowledge_index_history",
                                "icon": "索",
                                "eyebrow": "索引任务历史",
                                "title": str(document.get("title") or "知识文档"),
                                "badge": f"{len(index_jobs)} 次",
                                "rows": [
                                    {
                                        "label": str(job.get("job_id") or "索引任务"),
                                        "value": job_labels.get(
                                            str(job.get("status") or ""),
                                            str(job.get("status") or "未知"),
                                        ),
                                        "meta": (
                                            f"模型 {job.get('embedding_model') or '未记录'}"
                                            f" · 版本 {job.get('content_version') or '未知'}"
                                            + (
                                                f" · {job.get('error_code')}"
                                                if job.get("error_code")
                                                else ""
                                            )
                                        ),
                                    }
                                    for job in index_jobs[:8]
                                    if isinstance(job, Mapping)
                                ],
                                "action": {
                                    "label": "查看索引任务",
                                    "path": "/admin/knowledge/indexing-jobs",
                                },
                            }
                        )
            agents = data.get("agents")
            for agent_item in (
                agents if requested_asset in {None, "agents"} and isinstance(agents, list) else []
            ):
                if not isinstance(agent_item, Mapping):
                    continue
                status = str(agent_item.get("status") or "unknown")
                admin_cards.append(
                    {
                        "kind": "admin_agent_definition",
                        "icon": "AI",
                        "eyebrow": str(agent_item.get("agent_type") or "Agent"),
                        "title": str(agent_item.get("display_name") or "Agent"),
                        "badge": "运行中" if status == "active" else "已停用",
                        "tone": "warning" if status != "active" else "",
                        "rows": [
                            {
                                "label": "发布版本",
                                "value": str(agent_item.get("published_version") or "未发布"),
                            },
                            {
                                "label": "模型",
                                "value": str(agent_item.get("model_profile") or "未配置"),
                            },
                            {
                                "label": "授权工具",
                                "value": str(agent_item.get("tool_count", 0)),
                            },
                            {
                                "label": "作用范围",
                                "value": (
                                    "平台"
                                    if agent_item.get("scope_type") == "platform"
                                    else "指定店铺"
                                ),
                            },
                        ],
                        "action": {
                            "label": "打开 AI 治理",
                            "path": "/admin/ai",
                        },
                    }
                )
            skills = data.get("skills")
            for skill_item in (
                skills if requested_asset in {None, "skills"} and isinstance(skills, list) else []
            ):
                if not isinstance(skill_item, Mapping):
                    continue
                bindings = skill_item.get("tool_bindings")
                binding_values = (
                    [item for item in bindings if isinstance(item, Mapping)]
                    if isinstance(bindings, list)
                    else []
                )
                max_budget = max(
                    (int(item.get("call_budget") or 0) for item in binding_values),
                    default=0,
                )
                confirmations = sorted(
                    {str(item.get("confirmation_policy") or "none") for item in binding_values}
                )
                admin_cards.append(
                    {
                        "kind": "admin_skill_definition",
                        "icon": "技",
                        "eyebrow": "Skill 治理",
                        "title": str(skill_item.get("display_name") or "Skill"),
                        "badge": (
                            "已发布"
                            if skill_item.get("version_status") == "published"
                            else "未发布"
                        ),
                        "tone": (
                            ""
                            if skill_item.get("status") == "active"
                            and skill_item.get("version_status") == "published"
                            else "warning"
                        ),
                        "summary": str(skill_item.get("skill_code") or ""),
                        "rows": [
                            {
                                "label": "发布版本",
                                "value": str(skill_item.get("published_version") or "未发布"),
                            },
                            {"label": "绑定工具", "value": str(len(binding_values))},
                            {
                                "label": "确认策略",
                                "value": "、".join(confirmations) or "无绑定",
                            },
                            {
                                "label": "单工具调用预算",
                                "value": str(max_budget) if max_budget else "未配置",
                            },
                        ],
                        "action": {"label": "打开 AI 治理", "path": "/admin/ai"},
                    }
                )
            tools = data.get("tools")
            for tool_item in (
                tools if requested_asset in {None, "tools"} and isinstance(tools, list) else []
            ):
                if not isinstance(tool_item, Mapping):
                    continue
                fields = tool_item.get("input_schema_fields")
                field_values = [str(item) for item in fields] if isinstance(fields, list) else []
                status = str(tool_item.get("status") or "unknown")
                admin_cards.append(
                    {
                        "kind": "admin_tool_definition",
                        "icon": "工",
                        "eyebrow": "Tool / MCP 治理",
                        "title": str(tool_item.get("tool_code") or "Tool"),
                        "badge": "可用" if status == "active" else "已停用",
                        "tone": "" if status == "active" else "warning",
                        "summary": f"服务: {tool_item.get('server_code') or '未配置'}",
                        "rows": [
                            {
                                "label": "风险级别",
                                "value": str(tool_item.get("risk_level") or "未配置"),
                            },
                            {
                                "label": "发布版本",
                                "value": str(tool_item.get("published_version") or "未发布"),
                            },
                            {
                                "label": "输入字段",
                                "value": "、".join(field_values[:6]) or "无",
                                "meta": (
                                    f"另有 {len(field_values) - 6} 个字段"
                                    if len(field_values) > 6
                                    else None
                                ),
                            },
                        ],
                        "action": {"label": "打开 AI 治理", "path": "/admin/ai"},
                    }
                )
            recent_runs = data.get("recent_runs")
            failed_runs = (
                [
                    run
                    for run in recent_runs
                    if isinstance(run, Mapping) and run.get("status") == "failed"
                ]
                if isinstance(recent_runs, list)
                else []
            )
            if failed_runs:
                admin_cards.append(
                    {
                        "kind": "admin_agent_failures",
                        "icon": "诊",
                        "eyebrow": "近期运行",
                        "title": "需要复核的 Agent 失败",
                        "badge": f"{len(failed_runs)} 条",
                        "tone": "warning",
                        "rows": [
                            {
                                "label": str(run.get("agent_name") or "Agent"),
                                "value": str(run.get("error_code") or "未知错误"),
                                "meta": str(run.get("created_at") or ""),
                            }
                            for run in failed_runs[:4]
                        ],
                        "action": {
                            "label": "查看可观测性",
                            "path": "/admin/observability",
                        },
                    }
                )
        if intent == "support":
            tickets = data.get("active_tickets")
            ticket_labels = {
                "queued": "排队中",
                "assigned": "待接入",
                "active": "人工处理中",
                "waiting_user": "等待对方",
            }
            for ticket in tickets if isinstance(tickets, list) else []:
                if not isinstance(ticket, Mapping):
                    continue
                status = str(ticket.get("status") or "queued")
                target = (
                    str(ticket.get("store_name") or "店铺")
                    if ticket.get("queue_type") == "merchant"
                    else str(ticket.get("customer_name") or "用户")
                )
                admin_cards.append(
                    {
                        "kind": "admin_support_ticket",
                        "icon": "客",
                        "eyebrow": "店铺支持"
                        if ticket.get("queue_type") == "merchant"
                        else "用户支持",
                        "title": target,
                        "badge": ticket_labels.get(status, status),
                        "tone": "warning" if status in {"queued", "assigned"} else "",
                        "summary": str(ticket.get("summary") or "请求平台人工协助"),
                        "rows": [
                            {"label": "优先级", "value": str(ticket.get("priority") or "普通")},
                            {"label": "队列", "value": str(ticket.get("queue_code") or "平台客服")},
                            {
                                "label": "SLA 截止",
                                "value": str(ticket.get("sla_due_at") or "未设置"),
                            },
                        ],
                        "action": {"label": "进入会话", "path": "/admin/messages"},
                    }
                )
            selected_ticket = data.get("selected_ticket")
            if isinstance(selected_ticket, Mapping):
                recent_messages = selected_ticket.get("recent_messages")
                active_contexts = selected_ticket.get("active_contexts")
                message_values = (
                    [item for item in recent_messages if isinstance(item, Mapping)]
                    if isinstance(recent_messages, list)
                    else []
                )
                admin_cards.insert(
                    1,
                    {
                        "kind": "admin_support_ticket_context",
                        "icon": "聊",
                        "eyebrow": "工单会话上下文",
                        "title": str(
                            selected_ticket.get("store_name")
                            or selected_ticket.get("customer_name")
                            or "服务对象"
                        ),
                        "badge": ticket_labels.get(
                            str(selected_ticket.get("status") or "queued"),
                            str(selected_ticket.get("status") or "queued"),
                        ),
                        "summary": (
                            "已按消息顺序读取当前工单最近对话，"
                            "回复或处理前仍会重新核对关联业务数据。"
                        ),
                        "rows": [
                            {
                                "label": (
                                    "用户"
                                    if item.get("sender") == "user"
                                    else "人工客服"
                                    if item.get("sender") == "human"
                                    else "AI"
                                    if item.get("sender") == "agent"
                                    else "系统"
                                ),
                                "value": str(
                                    item.get("text")
                                    or (
                                        "[商品卡片]"
                                        if item.get("type") == "product_card"
                                        else "[订单卡片]"
                                        if item.get("type") == "order_card"
                                        else "[业务卡片]"
                                    )
                                )[:220],
                                "meta": str(item.get("sent_at") or ""),
                            }
                            for item in message_values[-10:]
                        ],
                        "footer": (
                            f"当前绑定 {len(active_contexts)} 个业务上下文"
                            if isinstance(active_contexts, list)
                            else "当前没有业务上下文"
                        ),
                        "action": {"label": "进入工单会话", "path": "/admin/messages"},
                    },
                )
        if intent == "users" and isinstance(data.get("selected_user"), Mapping):
            selected_user = data["selected_user"]
            addresses = selected_user.get("addresses")
            recent_user_orders = selected_user.get("recent_orders")
            cart_items = selected_user.get("cart_items")
            favorite_products = selected_user.get("favorite_products")
            followed_stores = selected_user.get("followed_stores")
            wallet_transactions = selected_user.get("wallet_transactions")
            admin_cards.append(
                {
                    "kind": "admin_user_assets",
                    "icon": "资",
                    "eyebrow": "用户资产与关系",
                    "title": str(selected_user.get("username") or "用户"),
                    "badge": "已核对",
                    "rows": [
                        {
                            "label": "收货地址",
                            "value": (
                                f"{len(addresses)} 条" if isinstance(addresses, list) else "0 条"
                            ),
                            "meta": (
                                "含默认地址"
                                if isinstance(addresses, list)
                                and any(
                                    isinstance(address, Mapping) and address.get("is_default")
                                    for address in addresses
                                )
                                else "未设置默认地址"
                            ),
                        },
                        {
                            "label": "购物车商品",
                            "value": str(selected_user.get("cart_item_count", 0)),
                        },
                        {
                            "label": "收藏商品",
                            "value": str(selected_user.get("product_favorite_count", 0)),
                        },
                        {
                            "label": "关注店铺",
                            "value": str(selected_user.get("store_follow_count", 0)),
                        },
                        {
                            "label": "近期有效订单",
                            "value": (
                                str(len(recent_user_orders))
                                if isinstance(recent_user_orders, list)
                                else "0"
                            ),
                        },
                    ],
                    "action": {"label": "打开用户管理", "path": "/admin/users"},
                }
            )
            if isinstance(addresses, list) and addresses:
                admin_cards.append(
                    {
                        "kind": "admin_user_addresses",
                        "icon": "址",
                        "eyebrow": "用户收货地址",
                        "title": f"共 {len(addresses)} 条地址",
                        "badge": "实时",
                        "rows": [
                            {
                                "label": "默认地址" if item.get("is_default") else "收货地址",
                                "value": " / ".join(
                                    str(code) for code in (item.get("region_codes") or []) if code
                                ),
                                "meta": str(item.get("phone_masked") or ""),
                            }
                            for item in addresses[:8]
                            if isinstance(item, Mapping)
                        ],
                        "action": {"label": "管理收货地址", "path": "/admin/users"},
                    }
                )
            if isinstance(recent_user_orders, list) and recent_user_orders:
                admin_cards.append(
                    {
                        "kind": "admin_user_orders",
                        "icon": "单",
                        "eyebrow": "用户非取消订单",
                        "title": f"最近 {len(recent_user_orders)} 笔",
                        "badge": "实时",
                        "rows": [
                            {
                                "label": str(item.get("store_name") or "商城订单")[:80],
                                "value": (
                                    str((item.get("amount") or {}).get("display", "¥0.00"))
                                    if isinstance(item.get("amount"), Mapping)
                                    else "¥0.00"
                                ),
                                "meta": order_labels.get(
                                    str(item.get("status") or ""),
                                    str(item.get("status") or ""),
                                ),
                            }
                            for item in recent_user_orders[:8]
                            if isinstance(item, Mapping)
                        ],
                        "action": {"label": "查看用户订单", "path": "/admin/users"},
                    }
                )
            if isinstance(wallet_transactions, list) and wallet_transactions:
                admin_cards.append(
                    {
                        "kind": "admin_user_wallet_transactions",
                        "icon": "账",
                        "eyebrow": "用户资金流水",
                        "title": str(
                            (selected_user.get("wallet") or {}).get("display", "¥0.00")
                            if isinstance(selected_user.get("wallet"), Mapping)
                            else "¥0.00"
                        ),
                        "badge": "主库账本",
                        "rows": [
                            {
                                "label": str(
                                    item.get("description") or item.get("type") or "资金变动"
                                )[:80],
                                "value": (
                                    ("+" if item.get("direction") == "credit" else "-")
                                    + str((item.get("amount") or {}).get("display", "¥0.00"))
                                    if isinstance(item.get("amount"), Mapping)
                                    else "¥0.00"
                                ),
                                "meta": str(item.get("occurred_at") or ""),
                            }
                            for item in wallet_transactions[:8]
                            if isinstance(item, Mapping)
                        ],
                        "action": {"label": "打开用户管理", "path": "/admin/users"},
                    }
                )
            if isinstance(cart_items, list) and cart_items:
                admin_cards.append(
                    {
                        "kind": "admin_user_cart_items",
                        "icon": "购",
                        "eyebrow": "用户购物车",
                        "title": f"当前共 {len(cart_items)} 个购物车条目",
                        "badge": "实时",
                        "rows": [
                            {
                                "label": str(item.get("product_name") or "商品")[:80],
                                "value": (
                                    str((item.get("current_price") or {}).get("display", "¥0.00"))
                                    if isinstance(item.get("current_price"), Mapping)
                                    else "¥0.00"
                                ),
                                "meta": (
                                    f"{item.get('sku_name') or '默认款式'} x "
                                    f"{item.get('quantity', 0)}"
                                ),
                            }
                            for item in cart_items[:8]
                            if isinstance(item, Mapping)
                        ],
                        "action": {"label": "管理用户购物车", "path": "/admin/users"},
                    }
                )
            if (isinstance(favorite_products, list) and favorite_products) or (
                isinstance(followed_stores, list) and followed_stores
            ):
                admin_cards.append(
                    {
                        "kind": "admin_user_favorites",
                        "icon": "藏",
                        "eyebrow": "用户收藏",
                        "title": "收藏商品与关注店铺",
                        "badge": "实时",
                        "rows": [
                            {
                                "label": "商品",
                                "value": str(item.get("product_name") or "商品")[:90],
                                "meta": str(item.get("store_name") or ""),
                            }
                            for item in (
                                favorite_products[:5] if isinstance(favorite_products, list) else []
                            )
                            if isinstance(item, Mapping)
                        ]
                        + [
                            {
                                "label": "店铺",
                                "value": str(item.get("store_name") or "店铺")[:90],
                                "meta": store_labels.get(
                                    str(item.get("status") or ""),
                                    str(item.get("status") or ""),
                                ),
                            }
                            for item in (
                                followed_stores[:5] if isinstance(followed_stores, list) else []
                            )
                            if isinstance(item, Mapping)
                        ],
                        "action": {"label": "打开用户管理", "path": "/admin/users"},
                    }
                )
        if intent == "stores" and isinstance(data.get("selected_store"), Mapping):
            selected_store = data["selected_store"]
            selected_products = selected_store.get("products")
            selected_orders = selected_store.get("recent_orders")
            admin_cards.append(
                {
                    "kind": "admin_store_operations",
                    "icon": "营",
                    "eyebrow": "店铺经营明细",
                    "title": str(selected_store.get("store_name") or "店铺"),
                    "badge": "实时",
                    "summary": str(selected_store.get("description") or "尚未填写店铺简介"),
                    "rows": [
                        {
                            "label": "商品状态",
                            "value": " · ".join(
                                f"{product_labels.get(str(key), key)} {value}"
                                for key, value in (
                                    selected_store.get("product_status_counts") or {}
                                ).items()
                            )
                            if isinstance(selected_store.get("product_status_counts"), Mapping)
                            else "暂无商品",
                        },
                        {
                            "label": "订单状态",
                            "value": " · ".join(
                                f"{order_labels.get(str(key), key)} {value}"
                                for key, value in (
                                    selected_store.get("order_status_counts") or {}
                                ).items()
                            )
                            if isinstance(selected_store.get("order_status_counts"), Mapping)
                            else "暂无订单",
                        },
                        {
                            "label": "近期商品",
                            "value": (
                                str(len(selected_products))
                                if isinstance(selected_products, list)
                                else "0"
                            ),
                        },
                        {
                            "label": "近期订单",
                            "value": (
                                str(len(selected_orders))
                                if isinstance(selected_orders, list)
                                else "0"
                            ),
                        },
                        {
                            "label": "累计评价",
                            "value": str(selected_store.get("rating_count", 0)),
                        },
                        {
                            "label": "收藏人数",
                            "value": str(selected_store.get("follower_count", 0)),
                        },
                    ],
                    "action": {"label": "进入店铺管理", "path": "/admin/stores"},
                }
            )
        return admin_cards[:9]
    return [
        {
            "kind": "admin_overview",
            "icon": "总",
            "eyebrow": "平台运营",
            "title": "商城运行总览",
            "badge": "实时",
            "rows": rows_from_counts(data.get("user_status_counts"), user_labels, maximum=3)
            + rows_from_counts(data.get("store_status_counts"), store_labels, maximum=3)
            + rows_from_counts(data.get("order_status_counts"), order_labels, maximum=3),
            "action": {"label": "进入管理首页", "path": "/admin"},
        }
    ]


def _money_display(minor_units: int, currency: str) -> str:
    normalized = currency.upper()
    symbol = "¥" if normalized == "CNY" else f"{normalized} "
    return f"{symbol}{minor_units / 100:.2f}"


def _percentage_display(value: object) -> str:
    if not isinstance(value, int | float):
        return "—"
    return f"{float(value) * 100:.1f}%"


def _display_timestamp(value: object) -> str:
    return str(value or "").replace("T", " ")[:19]


def _requested_metrics_days(value: str) -> int:
    compact = re.sub(r"\s+", "", value).casefold()
    if any(marker in compact for marker in ("今天", "今日", "当天")):
        return 1
    match = re.search(r"(?:近|最近|过去)(\d{1,3})(?:天|日)", compact)
    if match is not None:
        return max(1, min(int(match.group(1)), 90))
    if any(marker in compact for marker in ("本月", "这个月", "近一个月", "最近一个月")):
        return 30
    return 7


async def _complete(
    session: AsyncSession,
    context: TrustedOperationsContext,
    text: str,
    intent: str,
    data: Mapping[str, Any],
    *,
    tool_code: str | None = None,
    degraded_reason: str | None = None,
    trace_extra: Mapping[str, Any] | None = None,
) -> None:
    now = utc_now()
    conversation = await lock_conversation_for_append(session, context.conversation.id)
    conversation.last_sequence_no += 1
    conversation.last_message_at = now
    conversation.version += 1
    default_steps = [
        {"kind": "plan", "label": "识别只读任务", "status": "completed"},
        *(
            [
                {
                    "kind": "tool",
                    "label": "读取授权范围数据",
                    "tool_code": tool_code,
                    "status": "completed",
                }
            ]
            if tool_code
            else []
        ),
        {"kind": "answer", "label": "生成证据约束回复", "status": "completed"},
    ]
    extra = dict(trace_extra or {})
    if context.run.degraded_reason:
        extra.setdefault("degraded_reason", context.run.degraded_reason)
    supplied_steps = extra.pop("steps", None)
    steps = (
        [dict(item) for item in supplied_steps if isinstance(item, Mapping)]
        if isinstance(supplied_steps, list)
        else default_steps
    )
    supplied_source_ids = extra.pop("source_ids", None)
    source_ids = (
        [str(item) for item in supplied_source_ids if isinstance(item, str)]
        if isinstance(supplied_source_ids, list)
        else ([f"tool:{tool_code}"] if tool_code else [])
    )
    trace = public_trace(
        run_id=context.run.run_no,
        agent=context.agent_definition.display_name,
        model=context.agent_version.model_profile,
        question=context.trigger.text_content,
        intent=intent,
        data=data,
        steps=steps,
        source_ids=source_ids,
        tool_code=tool_code,
        extra=extra,
    )
    rich_order_cards: list[dict[str, object]] = []
    order_data_sources: list[Mapping[str, Any]] = [data]
    specialist_results = data.get("specialists")
    if isinstance(specialist_results, Mapping):
        for result in specialist_results.values():
            if isinstance(result, Mapping) and isinstance(result.get("data"), Mapping):
                order_data_sources.append(result["data"])
    seen_order_ids: set[str] = set()
    explicit_display_focus = _explicit_operations_display_focus(context.trigger.text_content or "")
    for order_data in order_data_sources:
        if explicit_display_focus not in {None, "orders"}:
            continue
        if context.audience not in {"merchant", "admin"} or order_data.get("query_mode") not in {
            "list",
            "detail",
        }:
            continue
        raw_orders = order_data.get("recent_orders")
        for raw_order in raw_orders if isinstance(raw_orders, list) else []:
            if not isinstance(raw_order, Mapping):
                continue
            order_id = str(raw_order.get("order_id") or "")
            if not order_id or order_id in seen_order_ids:
                continue
            seen_order_ids.add(order_id)
            rich_order_cards.append(
                {
                    "schema_version": 2,
                    "order_id": raw_order.get("order_id"),
                    "display_order_id": raw_order.get("display_order_id"),
                    "order_status": raw_order.get("status"),
                    "payment_status": raw_order.get("payment_status"),
                    "fulfillment_status": raw_order.get("fulfillment_status"),
                    "after_sale_status": raw_order.get("after_sale_status"),
                    "has_pending_review": raw_order.get("has_pending_review"),
                    "store": raw_order.get("store"),
                    "customer": raw_order.get("customer"),
                    "items": raw_order.get("items"),
                    "item_count": raw_order.get("item_count"),
                    "total_quantity": raw_order.get("total_quantity"),
                    "payable_amount": raw_order.get("payable_amount"),
                    "created_at": raw_order.get("created_at"),
                }
            )
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
            "data_scope": context.trusted_scope,
            "execution_trace": trace,
            "continuations": _operations_continuations(data),
            "order_cards": rich_order_cards[:8],
            "detail_cards": (
                []
                if intent == "security_refusal"
                else _operations_detail_cards(context, intent, data)
            ),
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
    context.run.degraded_reason = degraded_reason or context.run.degraded_reason
    context.run.version += 1
    session.add_all(_message_events(context, message, text, now))


def _message_events(
    context: TrustedOperationsContext, message: Message, text: str, now: Any
) -> list[OutboxEvent]:
    common = {"conversation_id": context.conversation.conversation_no, "run_id": context.run.run_no}
    events: list[OutboxEvent] = []
    for index, end in enumerate(range(160, len(text) + 160, 160), start=1):
        events.append(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="agent.response.delta.v1",
                aggregate_type="conversation",
                aggregate_no=context.conversation.conversation_no,
                aggregate_version=context.conversation.version,
                payload={**common, "chunk_index": index, "text_so_far": text[:end]},
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=context.run.trace_id,
            )
        )
    events.extend(
        [
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
            ),
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="message.sent.v1",
                aggregate_type="conversation",
                aggregate_no=context.conversation.conversation_no,
                aggregate_version=context.conversation.version,
                payload={
                    "conversation_id": context.conversation.conversation_no,
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
    return events


def _checkpoint(context: TrustedOperationsContext, intent: str | None) -> dict[str, object]:
    state: dict[str, object] = {
        "run_no": context.run.run_no,
        "conversation_no": context.conversation.conversation_no,
        "trigger_message_no": context.trigger.message_no,
        "user_no": context.user.user_no,
        "agent_version_no": str(context.agent_version.version_no),
        "audience": context.audience,
    }
    if context.store:
        state["store_no"] = context.store.store_no
    if intent:
        state["intent"] = intent
    return state


async def _finish_checkpoint(
    store: AgentCheckpointStore, context: TrustedOperationsContext, intent: str
) -> None:
    try:
        await store.write(
            context.run.run_no,
            "completed",
            _checkpoint(context, intent),
            status="completed",
        )
    except Exception:
        await store.session.rollback()
        context.run.degraded_reason = "checkpoint_terminal_write_failed"
