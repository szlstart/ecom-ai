from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.modules.agent_runtime.model_gateway import ModelGatewayError
from app.modules.agent_runtime.provider_gateway import (
    OpenAICompatiblePlanner,
    configured_model_gateways,
)


def _planner(
    handler: httpx.MockTransport,
    *,
    temperature: float = 0.0,
) -> tuple[OpenAICompatiblePlanner, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=handler)
    return OpenAICompatiblePlanner(
        api_url="https://models.invalid/v1/chat/completions",
        api_key="model-secret",
        model="approved-model",
        timeout_seconds=5,
        temperature=temperature,
        client=client,
    ), client


@pytest.mark.asyncio
async def test_provider_store_plan_uses_closed_schema_without_tools() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer model-secret"
        payload = json.loads(request.content)
        assert payload["temperature"] == 0
        assert "tools" not in payload
        schema = payload["response_format"]["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        assert "inventory_lookup" in schema["properties"]["intent"]["enum"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"intent":"inventory_lookup","search_text":null}'}}
                ]
            },
        )

    planner, client = _planner(httpx.MockTransport(respond))
    plan = await planner.plan_store("这个 SKU 还有库存吗")
    assert plan.intent == "inventory_lookup"
    assert plan.search_text is None
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_uses_model_specific_temperature() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["temperature"] == 1.0
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"intent":"policy_qa","search_text":null}'}}
                ]
            },
        )

    planner, client = _planner(httpx.MockTransport(respond), temperature=1.0)
    plan = await planner.plan_exclusive("平台规则是什么")
    assert plan.intent == "policy_qa"
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_exclusive_plan_rejects_unknown_or_malformed_output() -> None:
    responses = iter(
        (
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"intent":"admin_override","search_text":null}'}}
                    ]
                },
            ),
            httpx.Response(200, json={"choices": []}),
        )
    )

    def respond(_: httpx.Request) -> httpx.Response:
        return next(responses)

    planner, client = _planner(httpx.MockTransport(respond))
    with pytest.raises(ModelGatewayError, match="unsupported exclusive intent"):
        await planner.plan_exclusive("越权")
    with pytest.raises(ModelGatewayError, match="failed or returned an invalid plan"):
        await planner.plan_exclusive("订单在哪里")
    await client.aclose()


def test_gateway_factory_is_deterministic_by_default_and_configured_explicitly() -> None:
    assert configured_model_gateways(Settings(_env_file=None)) == (None, None)
    empty = Settings(
        _env_file=None,
        agent_model_api_url="",
        agent_model_api_key="",
        agent_model_name="",
    )
    assert configured_model_gateways(empty) == (None, None)
    settings = Settings(
        _env_file=None,
        agent_model_api_url="https://models.invalid/v1/chat/completions",
        agent_model_api_key="secret",
        agent_model_name="approved-model",
    )
    store, exclusive = configured_model_gateways(settings)
    assert store is not None
    assert exclusive is not None
