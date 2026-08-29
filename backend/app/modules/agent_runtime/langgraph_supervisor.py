from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from app.modules.agent_runtime.delegation import (
    DelegationBudget,
    DelegationPacket,
    DelegationPlan,
    DelegationTrace,
    MultiAgentOrchestrator,
    MultiAgentRoutingPolicy,
    RoutingDecision,
    Specialist,
    SpecialistResult,
    TrustedDelegationScope,
)


@dataclass(frozen=True)
class SupervisorRequest:
    intent: str
    independent_read_subtasks: int
    has_write_intent: bool
    router_confidence: float
    plan: DelegationPlan | None
    parent_tools: frozenset[str]
    parent_scope: TrustedDelegationScope
    parent_resource_refs: frozenset[tuple[str, str, int | None]]
    budget: DelegationBudget


@dataclass(frozen=True)
class SupervisorResponse:
    mode: Literal["single_agent", "multi_agent"]
    reason: str
    safe_output: Mapping[str, Any]
    traces: tuple[DelegationTrace, ...]


BaselineExecutor = Callable[[SupervisorRequest], Awaitable[Mapping[str, Any]]]
SpecialistExecutor = Callable[
    [DelegationPacket, DelegationBudget], Awaitable[SpecialistResult]
]


class _SupervisorState(TypedDict):
    request: SupervisorRequest
    decision: NotRequired[RoutingDecision]
    safe_output: NotRequired[dict[str, Any]]
    traces: NotRequired[list[DelegationTrace]]


class _SpecialistState(TypedDict):
    packet: DelegationPacket
    budget: DelegationBudget
    result: NotRequired[SpecialistResult]


class LangGraphSupervisor:
    """LangGraph control plane around the deterministic security/orchestration kernel."""

    def __init__(
        self,
        *,
        routing_policy: MultiAgentRoutingPolicy,
        orchestrator: MultiAgentOrchestrator,
        baseline_executor: BaselineExecutor,
    ) -> None:
        self.routing_policy = routing_policy
        self.orchestrator = orchestrator
        self.baseline_executor = baseline_executor
        self.graph = self._compile()

    def _compile(self) -> Any:
        builder = StateGraph(_SupervisorState)
        builder.add_node("route", self._route)
        builder.add_node("single_agent", self._single_agent)
        builder.add_node("multi_agent", self._multi_agent)
        builder.add_edge(START, "route")
        builder.add_conditional_edges(
            "route",
            self._branch,
            {"single_agent": "single_agent", "multi_agent": "multi_agent"},
        )
        builder.add_edge("single_agent", END)
        builder.add_edge("multi_agent", END)
        return builder.compile()

    async def _route(self, state: _SupervisorState) -> dict[str, object]:
        request = state["request"]
        decision = self.routing_policy.decide(
            intent=request.intent,
            independent_read_subtasks=request.independent_read_subtasks,
            has_write_intent=request.has_write_intent,
            confidence=request.router_confidence,
        )
        if decision.mode == "multi_agent" and request.plan is None:
            decision = RoutingDecision("single_agent", "delegation_plan_missing")
        return {"decision": decision}

    @staticmethod
    def _branch(state: _SupervisorState) -> Literal["single_agent", "multi_agent"]:
        return state["decision"].mode

    async def _single_agent(self, state: _SupervisorState) -> dict[str, object]:
        output = await self.baseline_executor(state["request"])
        return {"safe_output": dict(output), "traces": []}

    async def _multi_agent(self, state: _SupervisorState) -> dict[str, object]:
        request = state["request"]
        if request.plan is None:
            raise AssertionError("route node must reject a missing delegation plan")
        output, traces = await self.orchestrator.execute(
            request.plan,
            parent_tools=request.parent_tools,
            parent_scope=request.parent_scope,
            parent_resource_refs=request.parent_resource_refs,
            budget=request.budget,
        )
        return {"safe_output": output, "traces": traces}

    async def run(self, request: SupervisorRequest) -> SupervisorResponse:
        raw = cast(
            _SupervisorState,
            await self.graph.ainvoke(
                {"request": request},
                {"recursion_limit": 8},
            ),
        )
        decision = raw["decision"]
        return SupervisorResponse(
            decision.mode,
            decision.reason,
            raw.get("safe_output", {}),
            tuple(raw.get("traces", [])),
        )


def compile_specialist_subgraph(executor: SpecialistExecutor) -> Specialist:
    """Compile an invocation-stateless specialist into a one-node restricted Subgraph."""

    async def execute(state: _SpecialistState) -> dict[str, SpecialistResult]:
        result = await executor(state["packet"], state["budget"])
        return {"result": result}

    builder = StateGraph(_SpecialistState)
    builder.add_node("execute_specialist", execute)
    builder.add_edge(START, "execute_specialist")
    builder.add_edge("execute_specialist", END)
    graph = builder.compile()

    async def invoke(packet: DelegationPacket, budget: DelegationBudget) -> SpecialistResult:
        state = cast(
            _SpecialistState,
            await graph.ainvoke(
                {"packet": packet, "budget": budget},
                {"recursion_limit": 4},
            ),
        )
        return state["result"]

    return invoke
