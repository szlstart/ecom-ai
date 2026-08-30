from __future__ import annotations

import re
import time
from collections.abc import Mapping
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
    ProviderOperationsModelGateway,
    model_failure_code,
)
from app.modules.agent_runtime.public_trace import public_trace
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
    planning_input = context_window.planning_input(user_text)
    intent = _deterministic_intent(user_text, context.audience)
    if model_gateway is not None:
        try:
            intent = await model_gateway.plan(planning_input, context.agent_definition.agent_code)
        except (ModelGatewayError, TimeoutError) as exc:
            run.degraded_reason = model_failure_code(exc, "planning")
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
                )
                small_answer = grounded.text
                small_answer_mode = "model_grounded"
                small_confidence = grounded.confidence
                small_citations = grounded.cited_source_ids or small_citations
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
                    )
                    answer = grounded.text
                    answer_mode = "model_grounded"
                    confidence = grounded.confidence
                    multi_citations = grounded.cited_source_ids or source_ids
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
        evidence["conversation_window"] = context_window.evidence_projection()
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
        except (ModelGatewayError, TimeoutError) as exc:
            run.degraded_reason = model_failure_code(exc, "answer")
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
        specialist_code, tool_code, objective = _operations_specialist(
            context.audience, domain
        )
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
        f"tool:{_operations_specialist(context.audience, domain)[1]}"
        for domain in domains[:4]
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
    order_counts: Mapping[str, Any] = {}
    low_stock_count = 0
    completed_revenue: Mapping[str, Any] = {}
    unsettled_paid: Mapping[str, Any] = {}
    for result in specialists.values():
        if not isinstance(result, dict):
            continue
        safe_data = result.get("data")
        if not isinstance(safe_data, dict):
            continue
        candidate_products = safe_data.get("on_sale_products")
        if isinstance(candidate_products, list):
            products = [item for item in candidate_products if isinstance(item, Mapping)]
        candidate_counts = safe_data.get("order_status_counts")
        if isinstance(candidate_counts, Mapping):
            order_counts = candidate_counts
        candidate_revenue = safe_data.get("completed_order_revenue")
        if isinstance(candidate_revenue, Mapping):
            completed_revenue = candidate_revenue
        candidate_unsettled = safe_data.get("unsettled_paid_amount")
        if isinstance(candidate_unsettled, Mapping):
            unsettled_paid = candidate_unsettled
        low_stock_count = max(low_stock_count, int(safe_data.get("low_stock_sku_count", 0)))
    lines = ["已并行完成本店商品、库存和订单的只读经营诊断:"]
    if products:
        lines.append("在售商品与款式:")
        for product in products:
            lines.append(f"- {product.get('name')}:")
            sku_values = product.get("skus")
            for sku in sku_values if isinstance(sku_values, list) else []:
                if not isinstance(sku, Mapping):
                    continue
                price = sku.get("price")
                inventory = sku.get("inventory")
                price_display = price.get("display") if isinstance(price, Mapping) else "价格待核对"
                available = inventory.get("available", 0) if isinstance(inventory, Mapping) else 0
                lines.append(f"  - {sku.get('name')}: {price_display}，可售库存 {available}")
    else:
        lines.append("当前没有查询到在售商品。")
    if order_counts:
        status_summary = "、".join(
            f"{_order_status_label(str(key))} {value} 单"
            for key, value in order_counts.items()
        )
        lines.append(f"订单履约: {status_summary}。")
    lines.append(
        f"已确认营业额: {completed_revenue.get('display', '¥0.00')}。"
        f"已支付但待确认收货金额: {unsettled_paid.get('display', '¥0.00')}。"
    )
    risks: list[str] = []
    if low_stock_count:
        risks.append(f"{low_stock_count} 个款式达到低库存或缺货阈值")
    pending_fulfillment = sum(
        int(order_counts.get(key, 0)) for key in ("paid", "pending_shipment", "shipped")
    )
    if pending_fulfillment:
        risks.append(f"{pending_fulfillment} 单仍在待履约或运输阶段")
    lines.append("当前风险: " + ("；".join(risks) if risks else "未发现低库存或待履约积压") + "。")  # noqa: RUF001
    lines.extend(
        [
            "经营建议:",
            "1. 优先补充零库存和低库存款式，并结合实际销量调整安全库存。",
            "2. 持续跟进待发货和运输中订单，避免履约超时与售后升级。",
            "3. 商品价格、库存和订单状态变化后重新运行诊断，所有写操作仍由商家本人确认执行。",
            "以上数据来自本店实时结构化查询，本次没有修改任何业务记录。",
        ]
    )
    return "\n".join(lines)


def _order_status_label(value: str) -> str:
    return {
        "pending_payment": "待付款",
        "paid": "已付款待处理",
        "pending_shipment": "待发货",
        "shipped": "运输中",
        "completed": "已完成",
        "cancelled": "已取消",
        "closed": "已关闭",
    }.get(value, value)


def _render_multi_agent(data: Mapping[str, Any]) -> str:
    specialists = data.get("specialists")
    if not isinstance(specialists, dict) or not specialists:
        return "跨域诊断没有取得足够的可信结果，请缩小查询范围后重试。"
    lines = ["已并行完成跨域只读诊断:"]
    all_metrics: dict[str, int] = {}
    for result in specialists.values():
        if not isinstance(result, dict):
            continue
        specialist = _specialist_label(str(result.get("specialist"))).split(":", 1)[0]
        safe_data = result.get("data")
        if not isinstance(safe_data, dict):
            continue
        metrics = _flatten_summary(safe_data)
        all_metrics.update({key: value for key, value in metrics.items() if isinstance(value, int)})
        summary = "、".join(f"{key}={value}" for key, value in metrics.items())
        if not summary:
            summary = "已取得结构化汇总，可在右侧工作记录查看各领域完成状态"
        lines.append(f"- {specialist}: {summary}")
    risks: list[str] = []
    if all_metrics.get("pending_outbox_events", 0) > 0:
        risks.append(f"仍有 {all_metrics['pending_outbox_events']} 条 Outbox 事件待处理")
    if all_metrics.get("failed_agent_runs", 0) > 0:
        risks.append(f"存在 {all_metrics['failed_agent_runs']} 次失败的 Agent 运行")
    if all_metrics.get("product_status_counts.on_sale", 0) == 0:
        risks.append("平台当前没有在售商品")
    if risks:
        lines.append("风险: " + "；".join(risks) + "。")  # noqa: RUF001
        lines.append("建议: 优先处理积压或失败项，处理后重新执行只读诊断确认恢复。")
    else:
        lines.append("风险: 当前汇总未发现 Outbox 积压、Agent 失败或无在售商品风险。")
        lines.append("建议: 继续监控订单履约、库存和 Worker 连续失败指标，并按告警阈值处置。")
    lines.append("以上仅为当前授权范围内的实时汇总，本次没有修改任何业务数据。")
    return "\n".join(lines)


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
        unsettled_paid_amount = int(
            await session.scalar(
                select(
                    func.coalesce(func.sum(Order.paid_amount - Order.refunded_amount), 0)
                ).where(
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
            "你好，我是商家专属客服。我可以协助查看本店经营概览、商品与库存、"
            "订单履约等信息。涉及平台人工处理时，也可以由我发起转接。"
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
        lines = ["本店当前在售商品与可用库存:"]
        low_stock: list[str] = []
        for product in products:
            if not isinstance(product, dict):
                continue
            lines.append(f"- {product.get('name')} ({product.get('product_id')})")
            skus = product.get("skus")
            for sku in skus if isinstance(skus, list) else []:
                if not isinstance(sku, dict):
                    continue
                price = sku.get("price")
                inventory = sku.get("inventory")
                display = price.get("display") if isinstance(price, dict) else "价格未知"
                available = inventory.get("available") if isinstance(inventory, dict) else 0
                lines.append(f"  - {sku.get('name')}: {display}，可售库存 {available}")
                if isinstance(available, int) and available <= 5:
                    low_stock.append(f"{product.get('name')}/{sku.get('name')}")
        if low_stock:
            lines.append(
                "经营建议: 优先核对低库存款式 "
                + "、".join(low_stock[:5])
                + "，避免超卖。"
            )
        else:
            lines.append("经营建议: 当前款式库存均高于低库存提醒线，可结合销量继续观察补货节奏。")
        lines.append("价格和库存来自本店实时结构化数据，本次没有修改任何业务记录。")
        return "\n".join(lines)
    label = "店铺经营" if context.audience == "merchant" else "平台运行"
    lines = [f"已完成{label}的只读查询 ({intent}):"]
    for key, value in data.items():
        if key == "conversation_window":
            continue
        if isinstance(value, dict):
            rendered = "、".join(
                f"{item_key}={item_value}" for item_key, item_value in value.items()
            )
            lines.append(f"- {key}: {rendered or '暂无数据'}")
        else:
            lines.append(f"- {key}: {value}")
    lines.append("本次只读取了授权范围内的数据，没有修改任何业务记录。")
    return "\n".join(lines)


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
