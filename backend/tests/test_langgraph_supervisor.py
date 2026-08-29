import time

from app.modules.agent_runtime.delegation import (
    DelegationBudget,
    DelegationPacket,
    DelegationPlan,
    MultiAgentOrchestrator,
    MultiAgentRoutingPolicy,
    SpecialistResult,
    TrustedDelegationScope,
)
from app.modules.agent_runtime.langgraph_supervisor import (
    LangGraphSupervisor,
    SupervisorRequest,
    compile_specialist_subgraph,
)


async def test_langgraph_supervisor_keeps_simple_tasks_on_baseline() -> None:
    baseline_calls = 0

    async def baseline(request: SupervisorRequest) -> dict[str, object]:
        nonlocal baseline_calls
        baseline_calls += 1
        return {"source": "single", "intent": request.intent}

    supervisor = LangGraphSupervisor(
        routing_policy=MultiAgentRoutingPolicy(
            enabled=True,
            approved_intents=frozenset({"catalog_and_policy"}),
        ),
        orchestrator=MultiAgentOrchestrator({}),
        baseline_executor=baseline,
    )
    deadline = time.monotonic() + 2
    response = await supervisor.run(
        SupervisorRequest(
            intent="simple_catalog",
            independent_read_subtasks=1,
            has_write_intent=False,
            router_confidence=0.99,
            plan=None,
            parent_tools=frozenset(),
            parent_scope=TrustedDelegationScope("usr_1", "conv_1"),
            parent_resource_refs=frozenset(),
            budget=DelegationBudget(deadline, 20, 2, 2),
        )
    )
    assert response.mode == "single_agent"
    assert response.reason == "intent_not_evaluation_approved"
    assert response.safe_output == {"source": "single", "intent": "simple_catalog"}
    assert baseline_calls == 1


async def test_langgraph_supervisor_runs_only_approved_read_subgraphs() -> None:
    async def baseline(request: SupervisorRequest) -> dict[str, object]:
        return {"source": "single"}

    async def catalog_executor(
        packet: DelegationPacket, budget: DelegationBudget
    ) -> SpecialistResult:
        return SpecialistResult(
            "catalog",
            "succeeded",
            {"items": [{"product_id": "prd_1"}]},
            4,
            1,
            1,
            packet.trusted_scope,
        )

    specialist = compile_specialist_subgraph(catalog_executor)
    supervisor = LangGraphSupervisor(
        routing_policy=MultiAgentRoutingPolicy(
            enabled=True,
            approved_intents=frozenset({"catalog_and_policy"}),
        ),
        orchestrator=MultiAgentOrchestrator({"catalog": specialist}),
        baseline_executor=baseline,
    )
    deadline = time.monotonic() + 2
    scope = TrustedDelegationScope("usr_1", "conv_1")
    packet = DelegationPacket(
        delegation_no="dlg_catalog",
        parent_run_no="run_1",
        subtask_key="catalog_search",
        specialist_code="catalog",
        specialist_version="catalog:1",
        objective="search approved public products",
        depth=1,
        trusted_scope=scope,
        resource_refs=(),
        user_constraints=("public products only",),
        allowed_tools=frozenset({"catalog.search_products"}),
        budget=DelegationBudget(deadline, 10, 1, 1),
        ancestor_agents=("exclusive_support",),
    )
    response = await supervisor.run(
        SupervisorRequest(
            intent="catalog_and_policy",
            independent_read_subtasks=2,
            has_write_intent=False,
            router_confidence=0.95,
            plan=DelegationPlan((packet,)),
            parent_tools=frozenset({"catalog.search_products"}),
            parent_scope=scope,
            parent_resource_refs=frozenset(),
            budget=DelegationBudget(deadline, 20, 2, 2),
        )
    )
    assert response.mode == "multi_agent"
    assert response.safe_output["dlg_catalog"]["specialist"] == "catalog"
    assert response.traces[0].status == "succeeded"

    write_response = await supervisor.run(
        SupervisorRequest(
            intent="catalog_and_policy",
            independent_read_subtasks=2,
            has_write_intent=True,
            router_confidence=0.95,
            plan=DelegationPlan((packet,)),
            parent_tools=frozenset({"catalog.search_products"}),
            parent_scope=scope,
            parent_resource_refs=frozenset(),
            budget=DelegationBudget(time.monotonic() + 2, 20, 2, 2),
        )
    )
    assert write_response.mode == "single_agent"
    assert write_response.reason == "write_intent_must_remain_serial"
