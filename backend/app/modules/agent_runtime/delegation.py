from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import structlog

logger = structlog.get_logger(__name__)

DelegationStatus = Literal[
    "succeeded",
    "partial",
    "failed",
    "denied",
    "unknown",
    "cancelled",
    "reused",
]

MAX_DELEGATIONS = 4
MAX_PARALLEL_SPECIALISTS = 3
MAX_DELEGATION_DEPTH = 1


@dataclass(frozen=True)
class TrustedDelegationScope:
    user_no: str
    conversation_no: str
    store_no: str | None = None
    consent_types: frozenset[str] = frozenset()

    def safe_snapshot(self) -> dict[str, object]:
        return {
            "user_ref": _stable_ref(self.user_no),
            "conversation_ref": _stable_ref(self.conversation_no),
            "store_ref": _stable_ref(self.store_no) if self.store_no else None,
            "consent_types": sorted(self.consent_types),
        }


@dataclass(frozen=True)
class DelegatedResourceRef:
    resource_type: str
    resource_no: str
    version: int | None = None


@dataclass(frozen=True)
class DelegationBudget:
    deadline_monotonic: float
    token_limit: int
    tool_call_limit: int
    model_call_limit: int = 1

    def validate(self) -> None:
        if self.token_limit < 1 or self.tool_call_limit < 0 or self.model_call_limit < 0:
            raise ValueError("delegation budget must be non-negative and include output tokens")
        if time.monotonic() >= self.deadline_monotonic:
            raise TimeoutError("delegation deadline exhausted")

    def child(
        self,
        *,
        token_limit: int,
        tool_call_limit: int,
        model_call_limit: int = 1,
    ) -> DelegationBudget:
        if (
            token_limit > self.token_limit
            or tool_call_limit > self.tool_call_limit
            or model_call_limit > self.model_call_limit
        ):
            raise ValueError("child budget cannot exceed parent budget")
        child = DelegationBudget(
            self.deadline_monotonic,
            token_limit,
            tool_call_limit,
            model_call_limit,
        )
        child.validate()
        return child


@dataclass(frozen=True)
class DelegationPacket:
    delegation_no: str
    parent_run_no: str
    subtask_key: str
    specialist_code: str
    specialist_version: str
    objective: str
    depth: int
    trusted_scope: TrustedDelegationScope
    resource_refs: tuple[DelegatedResourceRef, ...]
    user_constraints: tuple[str, ...]
    allowed_tools: frozenset[str]
    budget: DelegationBudget
    ancestor_agents: tuple[str, ...]
    packet_version: int = 1

    @property
    def fingerprint(self) -> str:
        value = json.dumps(
            {
                "packet_version": self.packet_version,
                "parent_run_no": self.parent_run_no,
                "subtask_key": self.subtask_key,
                "specialist": self.specialist_code,
                "specialist_version": self.specialist_version,
                "objective": self.objective.strip().casefold(),
                "scope": self.trusted_scope.safe_snapshot(),
                "resource_refs": [
                    (item.resource_type, item.resource_no, item.version)
                    for item in self.resource_refs
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class SpecialistPolicy:
    specialist_code: str
    allowed_tools: frozenset[str]
    requires_consent: frozenset[str] = frozenset()


SPECIALIST_POLICIES: Mapping[str, SpecialistPolicy] = {
    "catalog": SpecialistPolicy(
        "catalog",
        frozenset(
            {
                "catalog.product.get",
                "catalog.search_products",
                "catalog.compare_skus",
                "catalog.compare_products",
                "catalog.get_inventory_availability",
                "catalog.get_store_policy",
            }
        ),
    ),
    "order": SpecialistPolicy(
        "order",
        frozenset({"order.list_user_orders", "order.get_user_order_detail"}),
    ),
    "logistics": SpecialistPolicy("logistics", frozenset({"logistics.get_user_order_shipments"})),
    "after_sales": SpecialistPolicy(
        "after_sales",
        frozenset(
            {
                "after_sale.check_refund_eligibility",
                "after_sale.list_user_refunds",
                "after_sale.get_user_refund_detail",
            }
        ),
    ),
    "recommendation": SpecialistPolicy(
        "recommendation",
        frozenset({"catalog.search_products", "catalog.product.get", "memory.list_mine"}),
        frozenset({"personalization"}),
    ),
    "policy": SpecialistPolicy(
        "policy", frozenset({"rag.policy.search", "catalog.get_store_policy"})
    ),
    "governance_users": SpecialistPolicy(
        "governance_users", frozenset({"governance.user_summary"})
    ),
    "governance_stores": SpecialistPolicy(
        "governance_stores", frozenset({"governance.store_summary"})
    ),
    "governance_orders": SpecialistPolicy(
        "governance_orders", frozenset({"governance.order_summary"})
    ),
    "observability": SpecialistPolicy("observability", frozenset({"observability.runtime_health"})),
}


@dataclass(frozen=True)
class SpecialistResult:
    specialist_code: str
    status: DelegationStatus
    safe_data: Mapping[str, Any]
    tokens_used: int
    tool_calls: int
    model_calls: int = 1
    scope: TrustedDelegationScope | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class DelegationTrace:
    delegation_no: str
    parent_run_no: str
    specialist_code: str
    specialist_version: str
    fingerprint: str
    depth: int
    status: DelegationStatus
    elapsed_ms: int
    tokens_used: int
    tool_calls: int
    model_calls: int
    span_id: str
    dependency_nos: tuple[str, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class DelegationPlan:
    packets: tuple[DelegationPacket, ...]
    dependencies: Mapping[str, frozenset[str]] = field(default_factory=dict)


class DelegationLedger(Protocol):
    async def get(self, packet: DelegationPacket) -> SpecialistResult | None: ...

    async def start(
        self,
        packet: DelegationPacket,
        *,
        dependency_nos: tuple[str, ...],
    ) -> None: ...

    async def put(
        self,
        packet: DelegationPacket,
        result: SpecialistResult,
        *,
        dependency_nos: tuple[str, ...],
    ) -> None: ...


class InMemoryDelegationLedger:
    """Test/local ledger; production adapters persist the same fingerprint in MySQL."""

    def __init__(self) -> None:
        self._items: dict[str, SpecialistResult] = {}

    async def get(self, packet: DelegationPacket) -> SpecialistResult | None:
        return self._items.get(packet.fingerprint)

    async def start(
        self,
        packet: DelegationPacket,
        *,
        dependency_nos: tuple[str, ...],
    ) -> None:
        del packet, dependency_nos

    async def put(
        self,
        packet: DelegationPacket,
        result: SpecialistResult,
        *,
        dependency_nos: tuple[str, ...],
    ) -> None:
        del dependency_nos
        if result.status in {"succeeded", "partial"}:
            self._items[packet.fingerprint] = result


Specialist = Callable[[DelegationPacket, DelegationBudget], Awaitable[SpecialistResult]]


class MultiAgentOrchestrator:
    """Executes a bounded read-only DAG and returns a deterministic safe projection."""

    def __init__(
        self,
        specialists: Mapping[str, Specialist],
        *,
        ledger: DelegationLedger | None = None,
        max_parallel: int = MAX_PARALLEL_SPECIALISTS,
    ) -> None:
        if not 1 <= max_parallel <= MAX_PARALLEL_SPECIALISTS:
            raise ValueError("parallel specialist limit exceeds platform policy")
        self.specialists = specialists
        self.ledger = ledger or InMemoryDelegationLedger()
        self.max_parallel = max_parallel

    async def execute(
        self,
        plan: DelegationPlan,
        *,
        parent_tools: frozenset[str],
        parent_scope: TrustedDelegationScope,
        parent_resource_refs: frozenset[tuple[str, str, int | None]],
        budget: DelegationBudget,
    ) -> tuple[dict[str, Any], list[DelegationTrace]]:
        budget.validate()
        validate_plan(
            plan,
            parent_tools=parent_tools,
            parent_scope=parent_scope,
            parent_resource_refs=parent_resource_refs,
            parent_budget=budget,
        )
        packets = {packet.delegation_no: packet for packet in plan.packets}
        pending = set(packets)
        terminal: set[str] = set()
        successful: set[str] = set()
        results: dict[str, SpecialistResult] = {}
        traces: list[DelegationTrace] = []
        semaphore = asyncio.Semaphore(self.max_parallel)

        async def run(packet: DelegationPacket) -> tuple[SpecialistResult, DelegationTrace]:
            started = time.monotonic()
            dependency_nos = tuple(sorted(plan.dependencies.get(packet.delegation_no, frozenset())))
            cached = await self.ledger.get(packet)
            if cached is not None:
                reused = SpecialistResult(
                    cached.specialist_code,
                    "reused",
                    cached.safe_data,
                    cached.tokens_used,
                    cached.tool_calls,
                    cached.model_calls,
                    cached.scope,
                )
                return reused, _trace(packet, reused, started, dependency_nos)
            try:
                await self.ledger.start(packet, dependency_nos=dependency_nos)
            except Exception as exc:
                logger.exception(
                    "agent_delegation_audit_start_failed",
                    run_no=packet.parent_run_no,
                    delegation_no=packet.delegation_no,
                    specialist=packet.specialist_code,
                    error_type=type(exc).__name__,
                )
                result = _failed_result(packet, "AI_DELEGATION_AUDIT_FAILED")
                return result, _trace(packet, result, started, dependency_nos)
            specialist = self.specialists.get(packet.specialist_code)
            if specialist is None:
                result = _failed_result(packet, "AI_SPECIALIST_UNAVAILABLE")
                return result, _trace(packet, result, started, dependency_nos)
            try:
                async with semaphore:
                    result = await asyncio.wait_for(
                        specialist(packet, packet.budget),
                        timeout=max(0.001, packet.budget.deadline_monotonic - time.monotonic()),
                    )
                _validate_result(packet, result)
            except TimeoutError:
                result = _failed_result(packet, "AI_SPECIALIST_TIMEOUT", status="unknown")
            except PermissionError:
                result = _failed_result(packet, "AI_SPECIALIST_SCOPE_DENIED", status="denied")
            except Exception:
                result = _failed_result(packet, "AI_SPECIALIST_FAILED")
            try:
                await self.ledger.put(packet, result, dependency_nos=dependency_nos)
            except Exception as exc:
                logger.exception(
                    "agent_delegation_audit_finish_failed",
                    run_no=packet.parent_run_no,
                    delegation_no=packet.delegation_no,
                    specialist=packet.specialist_code,
                    error_type=type(exc).__name__,
                )
                result = _failed_result(packet, "AI_DELEGATION_AUDIT_FAILED")
            return result, _trace(packet, result, started, dependency_nos)

        while pending:
            ready = sorted(
                (
                    delegation_no
                    for delegation_no in pending
                    if plan.dependencies.get(delegation_no, frozenset()) <= terminal
                ),
                key=str,
            )
            if not ready:
                raise ValueError("delegation graph must be acyclic")
            runnable: list[DelegationPacket] = []
            for delegation_no in ready:
                dependencies = plan.dependencies.get(delegation_no, frozenset())
                if not dependencies <= successful:
                    packet = packets[delegation_no]
                    result = _failed_result(
                        packet, "AI_SPECIALIST_DEPENDENCY_FAILED", status="cancelled"
                    )
                    results[delegation_no] = result
                    traces.append(
                        _trace(packet, result, time.monotonic(), tuple(sorted(dependencies)))
                    )
                    terminal.add(delegation_no)
                    pending.remove(delegation_no)
                else:
                    runnable.append(packets[delegation_no])
            completed = await asyncio.gather(*(run(packet) for packet in runnable))
            for packet, (result, trace) in zip(runnable, completed, strict=True):
                results[packet.delegation_no] = result
                traces.append(trace)
                terminal.add(packet.delegation_no)
                if result.status in {"succeeded", "partial", "reused"}:
                    successful.add(packet.delegation_no)
                pending.remove(packet.delegation_no)

        reduced = {
            delegation_no: {
                "specialist": results[delegation_no].specialist_code,
                "data": dict(results[delegation_no].safe_data),
            }
            for delegation_no in sorted(results)
            if results[delegation_no].status in {"succeeded", "partial", "reused"}
        }
        return reduced, sorted(traces, key=lambda item: item.delegation_no)


@dataclass(frozen=True)
class MultiAgentEvaluationReport:
    golden_set_version: str
    sample_size: int
    baseline_successes: int
    candidate_successes: int
    candidate_safety_violations: int
    candidate_p95_latency_ms: int
    approved_p95_latency_ms: int
    candidate_average_cost: float
    approved_average_cost: float


@dataclass(frozen=True)
class MultiAgentReleaseDecision:
    approved: bool
    reasons: tuple[str, ...]


class MultiAgentReleaseGate:
    """Pre-registered one-sided quality, safety, latency and cost gate."""

    def __init__(self, *, min_quality_delta: float = 0.05, min_z_score: float = 1.645) -> None:
        self.min_quality_delta = min_quality_delta
        self.min_z_score = min_z_score

    def evaluate(self, report: MultiAgentEvaluationReport) -> MultiAgentReleaseDecision:
        reasons: list[str] = []
        if (
            report.sample_size < 0
            or not 0 <= report.baseline_successes <= report.sample_size
            or not 0 <= report.candidate_successes <= report.sample_size
            or report.candidate_safety_violations < 0
            or report.candidate_p95_latency_ms < 0
            or report.approved_p95_latency_ms < 0
            or report.candidate_average_cost < 0
            or report.approved_average_cost < 0
        ):
            reasons.append("evaluation_report_invalid")
        elif report.sample_size < 30:
            reasons.append("insufficient_sample")
        elif not _quality_gain_is_significant(report, self.min_quality_delta, self.min_z_score):
            reasons.append("quality_gain_not_significant")
        if report.candidate_safety_violations:
            reasons.append("safety_regression")
        if report.candidate_p95_latency_ms > report.approved_p95_latency_ms:
            reasons.append("latency_budget_exceeded")
        if report.candidate_average_cost > report.approved_average_cost:
            reasons.append("cost_budget_exceeded")
        if not report.golden_set_version.strip():
            reasons.append("golden_set_version_missing")
        return MultiAgentReleaseDecision(not reasons, tuple(reasons))


@dataclass(frozen=True)
class RoutingDecision:
    mode: Literal["single_agent", "multi_agent"]
    reason: str


class MultiAgentRoutingPolicy:
    """Fail-closed routing: only pre-approved complex read intents may fan out."""

    def __init__(self, *, enabled: bool, approved_intents: frozenset[str]) -> None:
        self.enabled = enabled
        self.approved_intents = approved_intents

    @classmethod
    def from_agent_version_policy(cls, value: Mapping[str, object]) -> MultiAgentRoutingPolicy:
        raw = value.get("multi_agent")
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            return cls(enabled=False, approved_intents=frozenset())
        report_id = raw.get("evaluation_report_id")
        intents = raw.get("approved_intents")
        if (
            not isinstance(report_id, str)
            or not report_id.startswith("eval_")
            or not isinstance(intents, list)
            or not intents
            or any(not isinstance(item, str) or not item.strip() for item in intents)
        ):
            return cls(enabled=False, approved_intents=frozenset())
        return cls(enabled=True, approved_intents=frozenset(intents))

    def decide(
        self,
        *,
        intent: str,
        independent_read_subtasks: int,
        has_write_intent: bool,
        confidence: float,
    ) -> RoutingDecision:
        if not self.enabled:
            return RoutingDecision("single_agent", "feature_disabled")
        if intent not in self.approved_intents:
            return RoutingDecision("single_agent", "intent_not_evaluation_approved")
        if has_write_intent:
            return RoutingDecision("single_agent", "write_intent_must_remain_serial")
        if independent_read_subtasks < 2:
            return RoutingDecision("single_agent", "single_agent_baseline_is_sufficient")
        if confidence < 0.8:
            return RoutingDecision("single_agent", "router_confidence_too_low")
        return RoutingDecision("multi_agent", "approved_complex_read")


def validate_plan(
    plan: DelegationPlan,
    *,
    parent_tools: frozenset[str],
    parent_scope: TrustedDelegationScope,
    parent_resource_refs: frozenset[tuple[str, str, int | None]],
    parent_budget: DelegationBudget,
) -> None:
    if not 1 <= len(plan.packets) <= MAX_DELEGATIONS:
        raise ValueError("delegation count exceeds platform policy")
    packet_nos = {packet.delegation_no for packet in plan.packets}
    if len(packet_nos) != len(plan.packets):
        raise ValueError("delegation numbers must be unique")
    if set(plan.dependencies) - packet_nos:
        raise ValueError("delegation dependency references an unknown task")
    for delegation_no, dependencies in plan.dependencies.items():
        if delegation_no in dependencies or not dependencies <= packet_nos:
            raise ValueError("delegation dependency is invalid")
    topological_order({key: frozenset(value) for key, value in plan.dependencies.items()})
    seen: set[str] = set()
    for packet in plan.packets:
        validate_delegation(
            packet,
            parent_tools=parent_tools,
            parent_scope=parent_scope,
            parent_resource_refs=parent_resource_refs,
            seen_fingerprints=frozenset(seen),
        )
        seen.add(packet.fingerprint)
    if sum(item.budget.token_limit for item in plan.packets) > parent_budget.token_limit:
        raise ValueError("delegation token reservations exceed parent budget")
    if sum(item.budget.tool_call_limit for item in plan.packets) > parent_budget.tool_call_limit:
        raise ValueError("delegation tool reservations exceed parent budget")
    if sum(item.budget.model_call_limit for item in plan.packets) > parent_budget.model_call_limit:
        raise ValueError("delegation model reservations exceed parent budget")
    if any(
        item.budget.deadline_monotonic > parent_budget.deadline_monotonic for item in plan.packets
    ):
        raise ValueError("delegation deadline exceeds parent deadline")


def validate_delegation(
    packet: DelegationPacket,
    *,
    parent_tools: frozenset[str],
    parent_scope: TrustedDelegationScope,
    parent_resource_refs: frozenset[tuple[str, str, int | None]],
    seen_fingerprints: frozenset[str],
) -> None:
    if packet.packet_version != 1:
        raise ValueError("delegation packet version is unsupported")
    if not packet.delegation_no.startswith("dlg_") or not packet.parent_run_no.startswith("run_"):
        raise ValueError("delegation identifiers are invalid")
    if packet.depth != MAX_DELEGATION_DEPTH:
        raise ValueError("delegation depth exceeds policy")
    if packet.specialist_code in packet.ancestor_agents:
        raise ValueError("recursive delegation is forbidden")
    if not packet.ancestor_agents:
        raise ValueError("trusted parent Agent ancestry is required")
    if packet.fingerprint in seen_fingerprints:
        raise ValueError("duplicate specialist subtask is forbidden")
    if packet.trusted_scope != parent_scope:
        raise PermissionError("child scope must exactly match the trusted parent scope")
    child_refs = {
        (item.resource_type, item.resource_no, item.version) for item in packet.resource_refs
    }
    if not child_refs <= parent_resource_refs:
        raise PermissionError("child resources cannot exceed the trusted parent resource set")
    if not packet.allowed_tools <= parent_tools:
        raise PermissionError("child tools cannot exceed parent tools")
    policy = SPECIALIST_POLICIES.get(packet.specialist_code)
    if policy is None:
        raise LookupError("specialist is not registered")
    if not packet.allowed_tools <= policy.allowed_tools:
        raise PermissionError("child tools exceed specialist policy")
    if not policy.requires_consent <= packet.trusted_scope.consent_types:
        raise PermissionError("specialist consent is missing")
    if len(packet.resource_refs) > 8 or len(packet.user_constraints) > 8:
        raise ValueError("delegation context exceeds minimization policy")
    if not packet.objective.strip() or len(packet.objective) > 500:
        raise ValueError("delegation objective is invalid")
    if any(len(value) > 200 for value in packet.user_constraints):
        raise ValueError("delegation constraint exceeds minimization policy")
    if any(not _is_read_only(code) for code in packet.allowed_tools):
        raise PermissionError("parallel delegation is read-only")
    packet.budget.validate()


def topological_order(graph: Mapping[str, frozenset[str]]) -> list[str]:
    nodes = set(graph)
    for dependencies in graph.values():
        nodes.update(dependencies)
    visited: set[str] = set()
    visiting: set[str] = set()
    result: list[str] = []

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError("delegation graph must be acyclic")
        if node in visited:
            return
        visiting.add(node)
        for dependency in sorted(graph.get(node, frozenset())):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)
        result.append(node)

    for node in sorted(nodes):
        visit(node)
    return result


def _validate_result(packet: DelegationPacket, result: SpecialistResult) -> None:
    if result.specialist_code != packet.specialist_code:
        raise PermissionError("specialist identity changed during delegation")
    if result.scope is not None and result.scope != packet.trusted_scope:
        raise PermissionError("specialist result escaped the delegated scope")
    if (
        result.tokens_used > packet.budget.token_limit
        or result.tool_calls > packet.budget.tool_call_limit
        or result.model_calls > packet.budget.model_call_limit
    ):
        raise RuntimeError("specialist exceeded inherited budget")
    if result.tokens_used < 0 or result.tool_calls < 0 or result.model_calls < 0:
        raise RuntimeError("specialist reported invalid budget usage")
    try:
        encoded = json.dumps(result.safe_data, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("specialist result must be JSON serializable") from exc
    if len(encoded.encode()) > 32_768:
        raise RuntimeError("specialist result exceeds safe reducer size")


def _trace(
    packet: DelegationPacket,
    result: SpecialistResult,
    started: float,
    dependency_nos: tuple[str, ...],
) -> DelegationTrace:
    span_source = f"{packet.parent_run_no}:{packet.delegation_no}".encode()
    span_id = hashlib.sha256(span_source).hexdigest()[:16]
    return DelegationTrace(
        packet.delegation_no,
        packet.parent_run_no,
        packet.specialist_code,
        packet.specialist_version,
        packet.fingerprint,
        packet.depth,
        result.status,
        max(0, int((time.monotonic() - started) * 1000)),
        result.tokens_used,
        result.tool_calls,
        result.model_calls,
        span_id,
        dependency_nos,
        result.error_code,
    )


def _failed_result(
    packet: DelegationPacket,
    error_code: str,
    *,
    status: DelegationStatus = "failed",
) -> SpecialistResult:
    return SpecialistResult(
        packet.specialist_code,
        status,
        {},
        0,
        0,
        0,
        packet.trusted_scope,
        error_code,
    )


def _is_read_only(tool_code: str) -> bool:
    return tool_code in {
        tool for policy in SPECIALIST_POLICIES.values() for tool in policy.allowed_tools
    }


def _stable_ref(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _quality_gain_is_significant(
    report: MultiAgentEvaluationReport,
    min_quality_delta: float,
    min_z_score: float,
) -> bool:
    n = report.sample_size
    if n <= 0:
        return False
    baseline = report.baseline_successes / n
    candidate = report.candidate_successes / n
    if candidate - baseline < min_quality_delta:
        return False
    pooled = (report.baseline_successes + report.candidate_successes) / (2 * n)
    variance = pooled * (1 - pooled) * (2 / n)
    if variance <= 0:
        return candidate > baseline
    return (candidate - baseline) / math.sqrt(variance) >= min_z_score
