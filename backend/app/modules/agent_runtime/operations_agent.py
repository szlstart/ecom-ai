from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.modules.agent_runtime.checkpoints import AgentCheckpointStore
from app.modules.agent_runtime.model_gateway import ModelGatewayError
from app.modules.agent_runtime.models import AgentRun
from app.modules.agent_runtime.operations_context import (
    OperationsContextBuilder,
    TrustedOperationsContext,
)
from app.modules.agent_runtime.prompt_safety import detects_prompt_injection
from app.modules.agent_runtime.provider_gateway import ProviderOperationsModelGateway
from app.modules.catalog.models import Product, ProductSku
from app.modules.identity.models import User
from app.modules.inventory.models import Inventory
from app.modules.knowledge.contracts import ToolResult, ToolScope
from app.modules.knowledge.mcp_host import McpHost, ToolAdapter
from app.modules.knowledge.mcp_registry import database_kill_switch_checker
from app.modules.messaging.models import Message
from app.modules.orders.models import Order
from app.modules.stores.models import Store
from app.modules.system.models import OutboxEvent


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def process_operations_run(
    session: AsyncSession,
    run: AgentRun,
    *,
    checkpoint_store: AgentCheckpointStore,
    model_gateway: ProviderOperationsModelGateway | None,
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

    intent = _deterministic_intent(user_text, context.audience)
    if model_gateway is not None:
        try:
            intent = await model_gateway.plan(user_text, context.agent_definition.agent_code)
        except (ModelGatewayError, TimeoutError):
            run.degraded_reason = "planner_model_unavailable"
    tool_code = _tool_for_intent(intent, context.audience)
    if tool_code not in context.allowed_tools:
        tool_code = (
            "store_ops.overview"
            if context.audience == "merchant"
            else "governance.platform_overview"
        )
        intent = "overview"
    await checkpoint_store.write(run.run_no, "tool_planned", _checkpoint(context, intent))

    result = await _execute_tool(session, context, tool_code)
    if result.status != "succeeded":
        await _complete(
            session,
            context,
            "当前数据范围内无法安全完成查询。本次没有执行任何写操作，请稍后重试或联系人工客服。",
            intent,
            {},
            tool_code=tool_code,
            degraded_reason=result.error_code or "tool_failed",
        )
        await _finish_checkpoint(checkpoint_store, context, intent)
        return

    evidence = result.safe_data
    answer = _render(context, intent, evidence)
    answer_mode = "deterministic_fallback"
    confidence = "high"
    citations: tuple[str, ...] = (f"tool:{tool_code}",)
    if model_gateway is not None:
        run.current_phase = "answering"
        run.version += 1
        try:
            grounded = await model_gateway.synthesize(
                agent_prompt=context.agent_version.system_prompt,
                user_text=user_text,
                intent=intent,
                evidence=evidence,
                source_ids=citations,
            )
            answer = grounded.text
            answer_mode = "model_grounded"
            confidence = grounded.confidence
            citations = grounded.cited_source_ids or citations
        except (ModelGatewayError, TimeoutError):
            run.degraded_reason = "answer_model_unavailable"
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
        },
    )
    await _finish_checkpoint(checkpoint_store, context, intent)


async def _execute_tool(
    session: AsyncSession, context: TrustedOperationsContext, tool_code: str
) -> ToolResult:
    async def handler(_arguments: BaseModel, _scope: ToolScope) -> Mapping[str, Any]:
        return await _snapshot(session, context, tool_code)

    host = McpHost(
        [ToolAdapter(tool_code, EmptyArguments, handler)],
        database_kill_switch_checker(session),
    )
    return await host.execute(
        session,
        run_id=context.run.id,
        tool_code=tool_code,
        untrusted_arguments={},
        trusted_scope=ToolScope(
            user_no=context.user.user_no,
            conversation_no=context.conversation.conversation_no,
            store_no=context.store.store_no if context.store else None,
            context_no=None,
            context_version=None,
        ),
        allowed_tools=context.allowed_tools,
    )


async def _snapshot(
    session: AsyncSession, context: TrustedOperationsContext, tool_code: str
) -> dict[str, object]:
    if context.audience == "merchant":
        assert context.store is not None
        store_id = context.store.id
        product_counts = await _counts(
            session, Product.product_status, Product.store_id == store_id
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
        if tool_code == "store_ops.catalog_summary":
            return {"store_id": context.store.store_no, "product_status_counts": product_counts}
        if tool_code == "store_ops.order_summary":
            return {
                "store_id": context.store.store_no,
                "order_status_counts": order_counts,
                "recognized_revenue": {"amount": revenue, "currency": "CNY"},
            }
        if tool_code == "store_ops.inventory_risks":
            low_stock = int(
                await session.scalar(
                    select(func.count(Inventory.id))
                    .join(ProductSku, ProductSku.id == Inventory.sku_id)
                    .where(
                        ProductSku.store_id == store_id,
                        Inventory.inventory_status == "active",
                        Inventory.on_hand_quantity - Inventory.reserved_quantity
                        <= Inventory.safety_stock_quantity,
                    )
                )
                or 0
            )
            return {"store_id": context.store.store_no, "low_stock_sku_count": low_stock}
        return {
            "store": {
                "store_id": context.store.store_no,
                "name": context.store.store_name,
                "status": context.store.store_status,
                "rating": str(context.store.rating_score),
            },
            "product_status_counts": product_counts,
            "order_status_counts": order_counts,
            "recognized_revenue": {"amount": revenue, "currency": "CNY"},
        }

    user_counts = await _counts(session, User.user_status)
    store_counts = await _counts(session, Store.store_status)
    order_counts = await _counts(session, Order.order_status)
    product_counts = await _counts(session, Product.product_status, Product.deleted_at.is_(None))
    if tool_code == "governance.user_summary":
        return {"user_status_counts": user_counts}
    if tool_code == "governance.store_summary":
        return {"store_status_counts": store_counts, "product_status_counts": product_counts}
    if tool_code == "governance.order_summary":
        return {"order_status_counts": order_counts}
    pending_outbox = int(
        await session.scalar(
            select(func.count(OutboxEvent.id)).where(OutboxEvent.event_status == "pending")
        )
        or 0
    )
    failed_runs = int(
        await session.scalar(select(func.count(AgentRun.id)).where(AgentRun.run_status == "failed"))
        or 0
    )
    if tool_code == "observability.runtime_health":
        return {"pending_outbox_events": pending_outbox, "failed_agent_runs": failed_runs}
    return {
        "user_status_counts": user_counts,
        "store_status_counts": store_counts,
        "product_status_counts": product_counts,
        "order_status_counts": order_counts,
        "pending_outbox_events": pending_outbox,
        "failed_agent_runs": failed_runs,
    }


async def _counts(session: AsyncSession, field: Any, *conditions: Any) -> dict[str, int]:
    rows = (
        await session.execute(select(field, func.count()).where(*conditions).group_by(field))
    ).all()
    return {str(key): int(value) for key, value in rows}


def _deterministic_intent(text: str, audience: str) -> str:
    compact = re.sub(r"\s+", "", text).casefold()
    if any(term in compact for term in ("人工", "真人", "平台客服")):
        return "human_handoff"
    if any(term in compact for term in ("运行", "告警", "积压", "agent", "ai")):
        return "runtime" if audience == "admin" else "overview"
    if any(term in compact for term in ("库存", "缺货", "低库存")):
        return "inventory"
    if any(term in compact for term in ("订单", "营业额", "收入", "履约")):
        return "orders"
    if any(term in compact for term in ("商品", "上架", "下架", "审核")):
        return "catalog"
    if audience == "admin" and any(term in compact for term in ("用户", "账号")):
        return "users"
    if audience == "admin" and any(term in compact for term in ("店铺", "商家")):
        return "stores"
    return "overview"


def _tool_for_intent(intent: str, audience: str) -> str:
    if audience == "merchant":
        return {
            "catalog": "store_ops.catalog_summary",
            "orders": "store_ops.order_summary",
            "inventory": "store_ops.inventory_risks",
        }.get(intent, "store_ops.overview")
    return {
        "users": "governance.user_summary",
        "stores": "governance.store_summary",
        "orders": "governance.order_summary",
        "catalog": "governance.store_summary",
        "inventory": "governance.store_summary",
        "runtime": "observability.runtime_health",
    }.get(intent, "governance.platform_overview")


def _render(context: TrustedOperationsContext, intent: str, data: Mapping[str, Any]) -> str:
    label = "店铺经营" if context.audience == "merchant" else "平台运行"
    lines = [f"已完成{label}的只读查询 ({intent}):"]
    for key, value in data.items():
        if isinstance(value, dict):
            rendered = "、".join(
                f"{item_key}={item_value}" for item_key, item_value in value.items()
            )
            lines.append(f"- {key}: {rendered or '暂无数据'}")
        else:
            lines.append(f"- {key}: {value}")
    lines.append("本次只读取了授权范围内的数据，没有修改任何业务记录。")
    return "\n".join(lines)


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
    conversation = context.conversation
    conversation.last_sequence_no += 1
    conversation.last_message_at = now
    conversation.version += 1
    trace: dict[str, Any] = {
        "version": "public-agent-trace-v1",
        "run_id": context.run.run_no,
        "agent": context.agent_definition.display_name,
        "model": context.agent_version.model_profile,
        "status": "completed",
        "intent": intent,
        "steps": [
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
        ],
        "source_ids": [f"tool:{tool_code}"] if tool_code else [],
        "raw_reasoning_exposed": False,
        **dict(trace_extra or {}),
    }
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
    context.run.degraded_reason = degraded_reason or context.run.degraded_reason
    context.run.version += 1
    session.add_all(_message_events(context, message, text, now))


def _message_events(
    context: TrustedOperationsContext, message: Message, text: str, now: Any
) -> list[OutboxEvent]:
    common = {"conversation_id": context.conversation.conversation_no, "run_id": context.run.run_no}
    events = [
        OutboxEvent(
            event_no=new_prefixed_ulid("evt_"),
            event_type="agent.response.started.v1",
            aggregate_type="conversation",
            aggregate_no=context.conversation.conversation_no,
            aggregate_version=context.conversation.version,
            payload={**common, "chunk_index": 0},
            event_status="pending",
            available_at=now,
            attempt_count=0,
            trace_id=context.run.trace_id,
        )
    ]
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
