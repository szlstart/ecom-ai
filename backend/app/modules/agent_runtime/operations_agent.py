from __future__ import annotations

import re
import time
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session, mysql_session_factory
from app.modules.agent_runtime.checkpoints import AgentCheckpointStore
from app.modules.agent_runtime.context_window import ContextWindowBuilder
from app.modules.agent_runtime.conversation_summary import attach_rolling_summary
from app.modules.agent_runtime.delegation import (
    DelegationBudget,
    DelegationPacket,
    DelegationPlan,
    MultiAgentOrchestrator,
    MultiAgentRoutingPolicy,
    SpecialistResult,
    TrustedDelegationScope,
)
from app.modules.agent_runtime.delegation_ledger import SQLDelegationLedger
from app.modules.agent_runtime.handoff_intent import is_explicit_handoff_request
from app.modules.agent_runtime.langgraph_supervisor import (
    LangGraphSupervisor,
    SupervisorRequest,
    compile_specialist_subgraph,
)
from app.modules.agent_runtime.model_gateway import ModelGatewayError
from app.modules.agent_runtime.models import AgentRun
from app.modules.agent_runtime.operations_context import (
    OperationsContextBuilder,
    TrustedOperationsContext,
)
from app.modules.agent_runtime.prompt_safety import detects_prompt_injection
from app.modules.agent_runtime.provider_gateway import (
    AgentStreamCallback,
    ProviderOperationsModelGateway,
    model_failure_code,
)
from app.modules.agent_runtime.public_trace import public_trace
from app.modules.agent_runtime.store_agent import _model_invocation_trace
from app.modules.catalog.models import Product, ProductSku
from app.modules.identity.models import User
from app.modules.inventory.models import Inventory
from app.modules.knowledge.contracts import ToolResult, ToolScope
from app.modules.knowledge.mcp_host import McpHost, ToolAdapter
from app.modules.knowledge.mcp_registry import database_kill_switch_checker
from app.modules.messaging.human_schemas import HumanHandoffRequest
from app.modules.messaging.models import Message
from app.modules.messaging.service import MessagingService
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

    # Keep the parent AgentRun row unflushed here. Delegation ledger rows use a
    # foreign key to it from isolated sessions and must not wait on our row lock.
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
    intent = _deterministic_intent(user_text, context.audience)
    # Operations intents are a small, closed set and trusted data domains are known
    # server-side. Deterministic routing avoids an unnecessary remote model round trip;
    # the model remains responsible for grounded synthesis after tools have returned.
    complex_domains = (
        _admin_complex_domains(user_text)
        if context.audience == "admin"
        else _merchant_complex_domains(user_text)
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

    small_talk_reply = _operations_small_talk_reply(user_text, context.audience)
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
        if model_gateway is not None:
            run.current_phase = "answering"
            run.version += 1
            try:
                grounded = await model_gateway.synthesize(
                    agent_prompt=context.agent_version.system_prompt,
                    user_text=user_text,
                    intent="general_chat",
                    evidence=small_evidence,
                    source_ids=small_citations,
                    stream_callback=stream_callback,
                )
                small_answer = _normalize_operations_answer(grounded.text)
                if stream_callback is not None and small_answer != grounded.text:
                    await stream_callback("answer_replace", small_answer)
                small_answer_mode = "model_grounded"
                small_confidence = grounded.confidence
                small_citations = grounded.cited_source_ids or small_citations
                small_analysis = _grounded_analysis_trace(grounded)
            except (ModelGatewayError, TimeoutError) as exc:
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
                **small_analysis,
            },
        )
        await _finish_checkpoint(checkpoint_store, context, "general_chat")
        return

    if intent in {"complex_platform_diagnosis", "complex_store_diagnosis"}:
        multi_response = await _execute_operations_multi_agent(context, complex_domains)
        if multi_response is not None:
            evidence, trace_steps, source_ids = multi_response
            answer = (
                _render_merchant_multi_agent(evidence)
                if context.audience == "merchant"
                else _render_multi_agent(evidence)
            )
            answer_mode = "deterministic_fallback"
            confidence = "high"
            multi_citations = source_ids
            multi_analysis: dict[str, object] = {}
            if model_gateway is not None:
                run.current_phase = "answering"
                run.version += 1
                try:
                    grounded = await model_gateway.synthesize(
                        agent_prompt=context.agent_version.system_prompt,
                        user_text=user_text,
                        intent=intent,
                        evidence=evidence,
                        source_ids=source_ids,
                        stream_callback=stream_callback,
                    )
                    answer = _normalize_operations_answer(grounded.text)
                    if stream_callback is not None and answer != grounded.text:
                        await stream_callback("answer_replace", answer)
                    answer_mode = "model_grounded"
                    confidence = grounded.confidence
                    multi_citations = grounded.cited_source_ids or source_ids
                    multi_analysis = _grounded_analysis_trace(grounded)
                except (ModelGatewayError, TimeoutError) as exc:
                    run.degraded_reason = model_failure_code(exc, "answer")
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
                    "answer_mode": answer_mode,
                    "confidence": confidence,
                    **multi_analysis,
                },
            )
            await _finish_checkpoint(checkpoint_store, context, intent)
            return
    tool_code = _tool_for_intent(intent, context.audience)
    if tool_code not in context.allowed_tools:
        tool_code = (
            "store_ops.overview"
            if context.audience == "merchant"
            else "governance.platform_overview"
        )
        intent = "overview"
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

    evidence = dict(result.safe_data)
    if context_window.recent_turns or context_window.summary_no:
        evidence["conversation_window"] = context_window.model_projection()
    answer = _render(context, intent, evidence)
    answer_mode = "deterministic_fallback"
    confidence = "high"
    citations: tuple[str, ...] = (f"tool:{tool_code}",)
    grounded_analysis: dict[str, object] = {}
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
                stream_callback=stream_callback,
            )
            answer = _normalize_operations_answer(grounded.text)
            if stream_callback is not None and answer != grounded.text:
                await stream_callback("answer_replace", answer)
            answer_mode = "model_grounded"
            confidence = grounded.confidence
            citations = grounded.cited_source_ids or citations
            grounded_analysis = _grounded_analysis_trace(grounded)
        except (ModelGatewayError, TimeoutError) as exc:
            run.degraded_reason = model_failure_code(exc, "answer")
            if stream_callback is not None:
                await stream_callback("answer_replace", answer)
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
            **grounded_analysis,
        },
    )
    await _finish_checkpoint(checkpoint_store, context, intent)


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
    context: TrustedOperationsContext,
    domains: tuple[str, ...],
) -> tuple[dict[str, object], list[dict[str, object]], tuple[str, ...]] | None:
    routing = MultiAgentRoutingPolicy.from_agent_version_policy(context.agent_version.policy_config)
    deadline = time.monotonic() + 5.0
    parent_scope = TrustedDelegationScope(
        user_no=context.user.user_no,
        conversation_no=context.conversation.conversation_no,
    )
    parent_budget = DelegationBudget(
        deadline_monotonic=deadline,
        token_limit=4_800,
        tool_call_limit=4,
        model_call_limit=0,
    )
    packets: list[DelegationPacket] = []
    specialists: dict[str, Any] = {}
    for domain in domains[:4]:
        specialist_code, tool_code, objective = _operations_specialist(context.audience, domain)
        packet = DelegationPacket(
            delegation_no=new_prefixed_ulid("dlg_"),
            parent_run_no=context.run.run_no,
            subtask_key=f"{context.audience}-diagnosis:{domain}",
            specialist_code=specialist_code,
            specialist_version="v1",
            objective=objective,
            depth=1,
            trusted_scope=parent_scope,
            resource_refs=(),
            user_constraints=(),
            allowed_tools=frozenset({tool_code}),
            budget=parent_budget.child(
                token_limit=1_200,
                tool_call_limit=1,
                model_call_limit=0,
            ),
            ancestor_agents=(
                "admin_copilot" if context.audience == "admin" else "merchant_copilot",
            ),
        )
        packets.append(packet)
        specialists[specialist_code] = compile_specialist_subgraph(
            _admin_specialist_executor(context, tool_code, specialist_code)
        )

    orchestrator = MultiAgentOrchestrator(
        specialists,
        ledger=SQLDelegationLedger(mysql_session_factory()),
        max_parallel=3,
    )

    async def baseline(_request: SupervisorRequest) -> Mapping[str, Any]:
        return {"fallback": True}

    supervisor = LangGraphSupervisor(
        routing_policy=routing,
        orchestrator=orchestrator,
        baseline_executor=baseline,
    )
    response = await supervisor.run(
        SupervisorRequest(
            intent=(
                "complex_platform_diagnosis"
                if context.audience == "admin"
                else "complex_store_diagnosis"
            ),
            independent_read_subtasks=len(packets),
            has_write_intent=False,
            router_confidence=1.0,
            plan=DelegationPlan(tuple(packets)),
            parent_tools=context.allowed_tools,
            parent_scope=parent_scope,
            parent_resource_refs=frozenset(),
            budget=parent_budget,
        )
    )
    if response.mode != "multi_agent":
        return None
    evidence: dict[str, object] = {
        "specialists": dict(response.safe_output),
        "audience": context.audience,
        "result_policy": "只合并授权范围内、带工具审计的只读结果",
    }
    if context.store is not None:
        evidence["store"] = {
            "store_id": context.store.store_no,
            "store_name": context.store.store_name,
        }
    steps: list[dict[str, object]] = [
        {"kind": "plan", "label": "识别跨域只读诊断", "status": "completed"},
        {
            "kind": "supervisor",
            "label": "并行委派必要的领域助手",
            "status": "completed",
            "delegation_count": len(response.traces),
        },
    ]
    for trace in response.traces:
        steps.append(
            {
                "kind": "delegation",
                "label": _specialist_label(trace.specialist_code),
                "status": trace.status,
                "delegation_id": trace.delegation_no,
                "specialist": trace.specialist_code,
                "tool_code": _specialist_tool_code(trace.specialist_code),
                "latency_ms": trace.elapsed_ms,
                "tool_calls": trace.tool_calls,
                "tokens_used": trace.tokens_used,
                "error_code": trace.error_code,
            }
        )
    steps.append({"kind": "answer", "label": "合并可信诊断结果", "status": "completed"})
    source_ids = tuple(
        f"tool:{_operations_specialist(context.audience, domain)[1]}" for domain in domains[:4]
    )
    return evidence, steps, source_ids


def _admin_specialist_executor(
    context: TrustedOperationsContext,
    tool_code: str,
    specialist_code: str,
) -> Any:
    async def execute(packet: DelegationPacket, budget: DelegationBudget) -> SpecialistResult:
        budget.validate()
        async for child_session in mysql_session():
            result = await _execute_tool(child_session, context, tool_code)
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
        raise RuntimeError("MySQL session unavailable")

    return execute


def _admin_complex_domains(value: str) -> tuple[str, ...]:
    compact = re.sub(r"\s+", "", value).casefold()
    domains: list[str] = []
    rules = (
        ("users", ("用户", "账号", "注册", "登录")),
        ("stores", ("店铺", "商家", "商品", "上架", "库存")),
        ("orders", ("订单", "支付", "退款", "物流", "履约", "营业额")),
        ("runtime", ("运行", "告警", "积压", "故障", "agent", "ai", "worker")),
    )
    for domain, terms in rules:
        if any(term in compact for term in terms):
            domains.append(domain)
    return tuple(domains)


def _merchant_complex_domains(value: str) -> tuple[str, ...]:
    compact = re.sub(r"\s+", "", value).casefold()
    domains: list[str] = []
    rules = (
        ("catalog", ("商品", "款式", "sku", "价格", "在售")),
        ("inventory", ("库存", "缺货", "现货", "补货", "超卖")),
        ("orders", ("订单", "履约", "发货", "运输", "售后", "营业额", "收益")),
    )
    for domain, terms in rules:
        if any(term in compact for term in terms):
            domains.append(domain)
    return tuple(domains)


def _operations_specialist(audience: str, domain: str) -> tuple[str, str, str]:
    return _admin_specialist(domain) if audience == "admin" else _merchant_specialist(domain)


def _admin_specialist(domain: str) -> tuple[str, str, str]:
    return {
        "users": ("governance_users", "governance.user_summary", "核对平台用户状态汇总"),
        "stores": ("governance_stores", "governance.store_summary", "核对店铺与商品状态汇总"),
        "orders": ("governance_orders", "governance.order_summary", "核对订单状态汇总"),
        "runtime": ("observability", "observability.runtime_health", "核对运行时健康和积压"),
    }[domain]


def _merchant_specialist(domain: str) -> tuple[str, str, str]:
    return {
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
    }[domain]


def _specialist_label(code: str) -> str:
    return {
        "governance_users": "用户治理助手: 已核对用户状态",
        "governance_stores": "店铺治理助手: 已核对店铺与商品状态",
        "governance_orders": "订单助手: 已核对订单状态",
        "observability": "运行诊断助手: 已核对服务健康",
        "merchant_catalog": "商品助手: 已核对商品、款式和实时库存",
        "merchant_inventory": "库存助手: 已核对缺货和低库存风险",
        "merchant_orders": "履约助手: 已核对订单与营业额",
    }.get(code, "领域助手: 已完成只读核对")


def _specialist_tool_code(code: str) -> str:
    return {
        "governance_users": "governance.user_summary",
        "governance_stores": "governance.store_summary",
        "governance_orders": "governance.order_summary",
        "observability": "observability.runtime_health",
        "merchant_catalog": "store_ops.catalog_summary",
        "merchant_inventory": "store_ops.inventory_risks",
        "merchant_orders": "store_ops.order_summary",
    }.get(code, "")


def _render_merchant_multi_agent(data: Mapping[str, Any]) -> str:
    specialists = data.get("specialists")
    if not isinstance(specialists, dict) or not specialists:
        return "本次经营诊断没有取得足够的可信结果，请缩小查询范围后重试。"
    products: list[Mapping[str, Any]] = []
    products_loaded = False
    order_counts: Mapping[str, Any] = {}
    low_stock_count = 0
    completed_revenue: Mapping[str, Any] = {}
    for result in specialists.values():
        if not isinstance(result, dict):
            continue
        safe_data = result.get("data")
        if not isinstance(safe_data, dict):
            continue
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
    checked_scopes = ["库存", "订单"]
    if products_loaded:
        checked_scopes.insert(0, f"{len(products)} 件在售商品")
    overview = (
        f"已并行核对{store_name}的{'、'.join(checked_scopes)}，已确认营业额 "
        f"{completed_revenue.get('display', '¥0.00')}。"
    )
    if risks:
        return overview + "建议先处理" + "、".join(risks) + "。明细和入口已整理在下方卡片中。"
    return overview + "当前未发现低库存或待履约积压，明细和入口已整理在下方卡片中。"


def _render_multi_agent(data: Mapping[str, Any]) -> str:
    specialists = data.get("specialists")
    if not isinstance(specialists, dict) or not specialists:
        return "跨域诊断没有取得足够的可信结果，请缩小查询范围后重试。"
    all_metrics: dict[str, int] = {}
    for result in specialists.values():
        if not isinstance(result, dict):
            continue
        safe_data = result.get("data")
        if not isinstance(safe_data, dict):
            continue
        metrics = _flatten_summary(safe_data)
        all_metrics.update({key: value for key, value in metrics.items() if isinstance(value, int)})
    risks: list[str] = []
    if all_metrics.get("pending_outbox_events", 0) > 0:
        risks.append(f"仍有 {all_metrics['pending_outbox_events']} 条 Outbox 事件待处理")
    if all_metrics.get("unrecovered_agent_failures", 0) > 0:
        risks.append(f"存在 {all_metrics['unrecovered_agent_failures']} 个尚未恢复的 Agent 故障")
    if all_metrics.get("product_status_counts.on_sale", 0) == 0:
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
            f"已由 {checked} 个专业 Agent 并行完成只读诊断。需要优先关注: "
            + "、".join(risks)
            + "。具体指标和治理入口已整理在下方卡片中。"
            + recovery
        )
    return (
        f"已由 {checked} 个专业 Agent 并行完成只读诊断，当前未发现事件积压、"
        "未恢复的 Agent 故障或无在售商品风险。具体指标已整理在下方卡片中。" + recovery
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
        if tool_code == "store_ops.order_summary":
            return {
                "store_id": context.store.store_no,
                "order_status_counts": order_counts,
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
        "failed_agent_runs_24h": failed_runs,
        "successful_runs_after_latest_failure": successful_runs_after_latest_failure,
        "unrecovered_agent_failures": unrecovered_failures,
    }
    if tool_code == "observability.runtime_health":
        return runtime_health
    return {
        "user_status_counts": user_counts,
        "store_status_counts": store_counts,
        "product_status_counts": product_counts,
        "order_status_counts": order_counts,
        **runtime_health,
    }


async def _counts(session: AsyncSession, field: Any, *conditions: Any) -> dict[str, int]:
    rows = (
        await session.execute(select(field, func.count()).where(*conditions).group_by(field))
    ).all()
    return {str(key): int(value) for key, value in rows}


def _deterministic_intent(text: str, audience: str) -> str:
    compact = re.sub(r"\s+", "", text).casefold()
    if is_explicit_handoff_request(text):
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
    if context.audience == "merchant" and intent == "catalog":
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
        return {
            "orders": (
                "已完成本店订单、履约与营业额核对。先看卡片中的待处理数量，再进入订单页处理。"
            ),
            "inventory": "已完成本店库存风险扫描。卡片展示需要优先补货的款式数量和处理入口。",
        }.get(intent, f"已生成{store_name}经营快照，明细已整理为可操作卡片。")
    return {
        "users": "已完成平台用户状态核对。异常状态和治理入口已整理在卡片中。",
        "stores": "已完成店铺与商品状态核对。建议优先处理暂停店铺和非在售商品。",
        "catalog": "已完成平台商品状态核对。商品治理入口已附在卡片中。",
        "orders": "已完成订单履约状态核对。待付款、待发货、运输中和售后风险已整理在卡片中。",
        "runtime": "已完成 Agent 与异步链路健康核对。故障、积压和恢复状态已整理在卡片中。",
    }.get(intent, "已生成平台运营快照。用户、店铺、商品、订单和运行风险已整理为卡片。")


def _operations_detail_cards(
    context: TrustedOperationsContext, intent: str, data: Mapping[str, Any]
) -> list[dict[str, object]]:
    """Turn trusted operational evidence into compact, actionable UI cards."""

    specialists = data.get("specialists")
    if isinstance(specialists, Mapping):
        specialist_intents = {
            "merchant_catalog": "catalog",
            "merchant_inventory": "inventory",
            "merchant_orders": "orders",
            "governance_users": "users",
            "governance_stores": "stores",
            "governance_orders": "orders",
            "observability": "runtime",
        }
        specialist_results: dict[str, Mapping[str, Any]] = {}
        for result in specialists.values():
            if not isinstance(result, Mapping):
                continue
            safe_data = result.get("data")
            specialist = str(result.get("specialist"))
            if specialist in specialist_intents and isinstance(safe_data, Mapping):
                specialist_results[specialist] = safe_data
        specialist_cards: list[dict[str, object]] = []
        if context.audience == "merchant":
            # A cross-domain stock diagnosis must not dump the first five products just because
            # the catalog specialist completed first.  Put risks and orders first; catalog cards
            # are useful only when no more specific operational result is available.
            for specialist in ("merchant_inventory", "merchant_orders"):
                safe_data = specialist_results.get(specialist)
                if safe_data is not None:
                    specialist_cards.extend(
                        _operations_detail_cards(context, specialist_intents[specialist], safe_data)
                    )
            catalog_data = specialist_results.get("merchant_catalog")
            if catalog_data is not None and not specialist_cards:
                specialist_cards.extend(_operations_detail_cards(context, "catalog", catalog_data))
        else:
            for specialist in (
                "observability",
                "governance_orders",
                "governance_stores",
                "governance_users",
            ):
                safe_data = specialist_results.get(specialist)
                if safe_data is not None:
                    specialist_cards.extend(
                        _operations_detail_cards(context, specialist_intents[specialist], safe_data)
                    )
        if specialist_cards:
            return specialist_cards[:5]

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
        if intent == "catalog":
            product_cards: list[dict[str, object]] = []
            products = data.get("on_sale_products")
            for product in products if isinstance(products, list) else []:
                if not isinstance(product, Mapping):
                    continue
                sku_rows: list[dict[str, str]] = []
                skus = product.get("skus")
                for sku in skus if isinstance(skus, list) else []:
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
            return product_cards[:5]
        if intent == "inventory":
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
        return [
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
                "failed_agent_runs_24h": "24小时失败运行",
                "successful_runs_after_latest_failure": "故障后成功运行",
                "unrecovered_agent_failures": "未恢复故障",
            },
            "/admin/observability",
        ),
    }
    if intent in card_specs:
        icon, eyebrow, title, counts, labels, path = card_specs[intent]
        rows = rows_from_counts(counts, labels)
        warning = any(
            int(row["value"]) > 0
            for row in rows
            if row["value"].isdigit()
            and row["label"]
            in {"冻结", "停用", "已暂停", "待发货", "待投递事件", "24小时失败运行", "未恢复故障"}
        )
        return [
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
            "detail_cards": _operations_detail_cards(context, intent, data),
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
