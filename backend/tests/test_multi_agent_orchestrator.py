import asyncio
import time

import pytest

from app.modules.agent_runtime.delegation import (
    SPECIALIST_POLICIES,
    DelegatedResourceRef,
    DelegationBudget,
    DelegationPacket,
    DelegationPlan,
    InMemoryDelegationLedger,
    MultiAgentEvaluationReport,
    MultiAgentOrchestrator,
    MultiAgentReleaseGate,
    MultiAgentRoutingPolicy,
    SpecialistResult,
    TrustedDelegationScope,
    topological_order,
)
from app.modules.knowledge.contracts import READ_ONLY_TOOLS


def _scope(*, consent: bool = False) -> TrustedDelegationScope:
    return TrustedDelegationScope(
        "usr_1",
        "cnv_1",
        consent_types=frozenset({"personalization"}) if consent else frozenset(),
    )


def _budget(*, tokens: int = 10, tools: int = 1, models: int = 1) -> DelegationBudget:
    return DelegationBudget(time.monotonic() + 2, tokens, tools, models)


def _parent_refs() -> frozenset[tuple[str, str, int | None]]:
    return frozenset({("order", "ord_1", 2)})


def _packet(
    delegation_no: str,
    specialist: str,
    tool: str,
    *,
    scope: TrustedDelegationScope | None = None,
    tokens: int = 10,
) -> DelegationPacket:
    return DelegationPacket(
        delegation_no=delegation_no,
        parent_run_no="run_1",
        subtask_key=f"{specialist}:{delegation_no}",
        specialist_code=specialist,
        specialist_version=f"{specialist}:1",
        objective=f"resolve {specialist}",
        depth=1,
        trusted_scope=scope or _scope(),
        resource_refs=(DelegatedResourceRef("order", "ord_1", 2),),
        user_constraints=("only current resource",),
        allowed_tools=frozenset({tool}),
        budget=_budget(tokens=tokens),
        ancestor_agents=("exclusive_support",),
    )


async def test_parallel_read_only_dag_reducer_trace_and_idempotent_reuse() -> None:
    concurrent = 0
    peak = 0

    async def specialist(packet: DelegationPacket, budget: DelegationBudget) -> SpecialistResult:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.01)
        concurrent -= 1
        return SpecialistResult(
            packet.specialist_code,
            "succeeded",
            {"fact": packet.objective},
            2,
            1,
            1,
            packet.trusted_scope,
        )

    packets = (
        _packet("dlg_catalog", "catalog", "catalog.product.get"),
        _packet("dlg_order", "order", "order.get_user_order_detail"),
        _packet("dlg_logistics", "logistics", "logistics.get_user_order_shipments"),
    )
    plan = DelegationPlan(
        packets,
        {"dlg_logistics": frozenset({"dlg_order"})},
    )
    ledger = InMemoryDelegationLedger()
    orchestrator = MultiAgentOrchestrator(
        {"catalog": specialist, "order": specialist, "logistics": specialist},
        ledger=ledger,
        max_parallel=2,
    )
    parent_tools = frozenset(
        {
            "catalog.product.get",
            "order.get_user_order_detail",
            "logistics.get_user_order_shipments",
        }
    )
    result, traces = await orchestrator.execute(
        plan,
        parent_tools=parent_tools,
        parent_scope=_scope(),
        parent_resource_refs=_parent_refs(),
        budget=_budget(tokens=30, tools=3, models=3),
    )
    assert list(result) == ["dlg_catalog", "dlg_logistics", "dlg_order"]
    assert peak == 2
    logistics_trace = next(item for item in traces if item.delegation_no == "dlg_logistics")
    assert logistics_trace.dependency_nos == ("dlg_order",)
    assert all(len(item.span_id) == 16 for item in traces)

    _result, replay_traces = await orchestrator.execute(
        plan,
        parent_tools=parent_tools,
        parent_scope=_scope(),
        parent_resource_refs=_parent_refs(),
        budget=_budget(tokens=30, tools=3, models=3),
    )
    assert {item.status for item in replay_traces} == {"reused"}


async def test_failure_is_isolated_and_failed_dependency_is_cancelled() -> None:
    async def specialist(packet: DelegationPacket, budget: DelegationBudget) -> SpecialistResult:
        if packet.specialist_code == "order":
            raise RuntimeError("provider detail must not leak")
        return SpecialistResult(
            packet.specialist_code,
            "succeeded",
            {"safe": True},
            1,
            1,
            1,
            packet.trusted_scope,
        )

    plan = DelegationPlan(
        (
            _packet("dlg_catalog", "catalog", "catalog.search_products"),
            _packet("dlg_order", "order", "order.get_user_order_detail"),
            _packet("dlg_logistics", "logistics", "logistics.get_user_order_shipments"),
        ),
        {"dlg_logistics": frozenset({"dlg_order"})},
    )
    result, traces = await MultiAgentOrchestrator(
        {"catalog": specialist, "order": specialist, "logistics": specialist}
    ).execute(
        plan,
        parent_tools=frozenset(
            {
                "catalog.search_products",
                "order.get_user_order_detail",
                "logistics.get_user_order_shipments",
            }
        ),
        parent_scope=_scope(),
        parent_resource_refs=_parent_refs(),
        budget=_budget(tokens=30, tools=3, models=3),
    )
    assert list(result) == ["dlg_catalog"]
    outcomes = {item.delegation_no: (item.status, item.error_code) for item in traces}
    assert outcomes["dlg_order"] == ("failed", "AI_SPECIALIST_FAILED")
    assert outcomes["dlg_logistics"] == ("cancelled", "AI_SPECIALIST_DEPENDENCY_FAILED")


@pytest.mark.parametrize(
    ("packet", "parent_scope", "parent_tools", "error"),
    [
        (
            _packet("dlg_1", "catalog", "catalog.product.get"),
            TrustedDelegationScope("usr_other", "cnv_1"),
            frozenset({"catalog.product.get"}),
            PermissionError,
        ),
        (
            _packet("dlg_1", "catalog", "order.cancel"),
            _scope(),
            frozenset({"order.cancel"}),
            PermissionError,
        ),
        (
            _packet("dlg_1", "recommendation", "memory.list_mine"),
            _scope(),
            frozenset({"memory.list_mine"}),
            PermissionError,
        ),
    ],
)
async def test_scope_tool_and_consent_cannot_expand(
    packet: DelegationPacket,
    parent_scope: TrustedDelegationScope,
    parent_tools: frozenset[str],
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        await MultiAgentOrchestrator({}).execute(
            DelegationPlan((packet,)),
            parent_tools=parent_tools,
            parent_scope=parent_scope,
            parent_resource_refs=_parent_refs(),
            budget=_budget(tokens=10, tools=1, models=1),
        )


async def test_budget_overrun_is_failed_without_unsafe_result() -> None:
    async def specialist(packet: DelegationPacket, budget: DelegationBudget) -> SpecialistResult:
        return SpecialistResult(
            packet.specialist_code,
            "succeeded",
            {"unsafe": "must not be reduced"},
            budget.token_limit + 1,
            0,
            1,
            packet.trusted_scope,
        )

    packet = _packet("dlg_1", "catalog", "catalog.search_products")
    result, traces = await MultiAgentOrchestrator({"catalog": specialist}).execute(
        DelegationPlan((packet,)),
        parent_tools=frozenset({"catalog.search_products"}),
        parent_scope=_scope(),
        parent_resource_refs=_parent_refs(),
        budget=_budget(tokens=10, tools=1, models=1),
    )
    assert result == {}
    assert traces[0].status == "failed"
    assert traces[0].error_code == "AI_SPECIALIST_FAILED"


async def test_audit_failure_blocks_execution_and_scope_mismatch_is_discarded() -> None:
    called = False

    class BrokenLedger:
        async def get(self, packet: DelegationPacket) -> SpecialistResult | None:
            return None

        async def start(
            self,
            packet: DelegationPacket,
            *,
            dependency_nos: tuple[str, ...],
        ) -> None:
            raise RuntimeError("database unavailable")

        async def put(
            self,
            packet: DelegationPacket,
            result: SpecialistResult,
            *,
            dependency_nos: tuple[str, ...],
        ) -> None:
            raise AssertionError("put must not run")

    async def specialist(packet: DelegationPacket, budget: DelegationBudget) -> SpecialistResult:
        nonlocal called
        called = True
        return SpecialistResult(packet.specialist_code, "succeeded", {}, 1, 1)

    packet = _packet("dlg_audit", "catalog", "catalog.product.get")
    result, traces = await MultiAgentOrchestrator(
        {"catalog": specialist}, ledger=BrokenLedger()
    ).execute(
        DelegationPlan((packet,)),
        parent_tools=frozenset({"catalog.product.get"}),
        parent_scope=_scope(),
        parent_resource_refs=_parent_refs(),
        budget=_budget(),
    )
    assert not called
    assert result == {}
    assert traces[0].error_code == "AI_DELEGATION_AUDIT_FAILED"

    async def wrong_scope(
        packet: DelegationPacket, budget: DelegationBudget
    ) -> SpecialistResult:
        return SpecialistResult(
            packet.specialist_code,
            "succeeded",
            {"must_not_escape": True},
            1,
            1,
            1,
            TrustedDelegationScope("usr_other", "cnv_other"),
        )

    result, traces = await MultiAgentOrchestrator({"catalog": wrong_scope}).execute(
        DelegationPlan((packet,)),
        parent_tools=frozenset({"catalog.product.get"}),
        parent_scope=_scope(),
        parent_resource_refs=_parent_refs(),
        budget=_budget(),
    )
    assert result == {}
    assert traces[0].error_code == "AI_SPECIALIST_SCOPE_DENIED"


def test_dag_cycle_count_and_depth_are_hard_failures() -> None:
    assert topological_order({"b": frozenset({"a"})}) == ["a", "b"]
    with pytest.raises(ValueError, match="acyclic"):
        topological_order({"a": frozenset({"b"}), "b": frozenset({"a"})})
    packets = tuple(
        _packet(f"dlg_{index}", "catalog", "catalog.search_products") for index in range(5)
    )
    with pytest.raises(ValueError, match="count"):
        asyncio.run(
            MultiAgentOrchestrator({}).execute(
                DelegationPlan(packets),
                parent_tools=frozenset({"catalog.search_products"}),
                parent_scope=_scope(),
                parent_resource_refs=_parent_refs(),
                budget=_budget(tokens=50, tools=5, models=5),
            )
        )
    invalid_depth = _packet("dlg_depth", "catalog", "catalog.search_products")
    object.__setattr__(invalid_depth, "depth", 2)
    with pytest.raises(ValueError, match="depth"):
        asyncio.run(
            MultiAgentOrchestrator({}).execute(
                DelegationPlan((invalid_depth,)),
                parent_tools=frozenset({"catalog.search_products"}),
                parent_scope=_scope(),
                parent_resource_refs=_parent_refs(),
                budget=_budget(),
            )
        )


async def test_recursive_and_duplicate_delegations_are_rejected() -> None:
    recursive = _packet("dlg_recursive", "catalog", "catalog.search_products")
    object.__setattr__(recursive, "ancestor_agents", ("exclusive_support", "catalog"))
    with pytest.raises(ValueError, match="recursive"):
        await MultiAgentOrchestrator({}).execute(
            DelegationPlan((recursive,)),
            parent_tools=frozenset({"catalog.search_products"}),
            parent_scope=_scope(),
            parent_resource_refs=_parent_refs(),
            budget=_budget(),
        )

    first = _packet("dlg_first", "catalog", "catalog.search_products")
    duplicate = _packet("dlg_second", "catalog", "catalog.search_products")
    object.__setattr__(duplicate, "subtask_key", first.subtask_key)
    with pytest.raises(ValueError, match="duplicate"):
        await MultiAgentOrchestrator({}).execute(
            DelegationPlan((first, duplicate)),
            parent_tools=frozenset({"catalog.search_products"}),
            parent_scope=_scope(),
            parent_resource_refs=_parent_refs(),
            budget=_budget(tokens=20, tools=2, models=2),
        )
def test_release_gate_and_router_keep_unproven_intents_on_single_agent() -> None:
    gate = MultiAgentReleaseGate()
    approved = gate.evaluate(
        MultiAgentEvaluationReport(
            "multi-agent-golden-v1", 200, 130, 160, 0, 1800, 2000, 0.03, 0.04
        )
    )
    assert approved.approved
    rejected = gate.evaluate(
        MultiAgentEvaluationReport("v1", 200, 150, 152, 0, 1800, 2000, 0.03, 0.04)
    )
    assert rejected.reasons == ("quality_gain_not_significant",)

    policy = MultiAgentRoutingPolicy(
        enabled=True,
        approved_intents=frozenset({"order_and_logistics_compare"}),
    )
    assert (
        policy.decide(
            intent="simple_order_lookup",
            independent_read_subtasks=1,
            has_write_intent=False,
            confidence=0.99,
        ).mode
        == "single_agent"
    )
    assert (
        policy.decide(
            intent="order_and_logistics_compare",
            independent_read_subtasks=2,
            has_write_intent=True,
            confidence=0.99,
        ).reason
        == "write_intent_must_remain_serial"
    )
    assert (
        policy.decide(
            intent="order_and_logistics_compare",
            independent_read_subtasks=2,
            has_write_intent=False,
            confidence=0.9,
        ).mode
        == "multi_agent"
    )
    assert not MultiAgentRoutingPolicy.from_agent_version_policy(
        {"multi_agent": {"enabled": True, "approved_intents": ["complex"]}}
    ).enabled
    assert MultiAgentRoutingPolicy.from_agent_version_policy(
        {
            "multi_agent": {
                "enabled": True,
                "approved_intents": ["order_and_logistics_compare"],
                "evaluation_report_id": "eval_release_1",
            }
        }
    ).enabled


def test_specialist_tool_sets_match_published_read_contracts() -> None:
    assert set(SPECIALIST_POLICIES) == {
        "catalog",
        "order",
        "logistics",
        "after_sales",
        "recommendation",
        "policy",
        "governance_users",
        "governance_stores",
        "governance_orders",
        "observability",
        "merchant_catalog",
        "merchant_inventory",
        "merchant_orders",
    }
    for policy in SPECIALIST_POLICIES.values():
        assert policy.allowed_tools <= READ_ONLY_TOOLS | {"rag.policy.search"}
