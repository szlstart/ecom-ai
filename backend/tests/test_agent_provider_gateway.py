from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.modules.agent_runtime.model_gateway import ModelGatewayError
from app.modules.agent_runtime.provider_gateway import (
    OpenAICompatiblePlanner,
    configured_model_gateways,
    probe_model_provider,
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
                "choices": [{"message": {"content": '{"intent":"policy_qa","search_text":null}'}}]
            },
        )

    planner, client = _planner(httpx.MockTransport(respond), temperature=1.0)
    plan = await planner.plan_exclusive("平台规则是什么")
    assert plan.intent == "policy_qa"
    await client.aclose()


@pytest.mark.asyncio
async def test_previous_handoff_message_cannot_turn_current_greeting_into_handoff() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "Classify CURRENT_UNTRUSTED_MESSAGE only" in payload["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"intent":"human_handoff","search_text":null}'}}
                ]
            },
        )

    planner, client = _planner(httpx.MockTransport(respond))
    plan = await planner.plan_exclusive(
        "CURRENT_UNTRUSTED_MESSAGE:\nhello\n\n"
        "RECENT_UNTRUSTED_DIALOGUE_FOR_COREFERENCE_ONLY:\n"
        "AI客服: 已为你转接平台人工客服，请留意排队状态。"
    )
    assert plan.intent == "general_chat"
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_cannot_reopen_handoff_for_a_status_question() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"intent":"human_handoff","search_text":null}'}}
                ]
            },
        )

    planner, client = _planner(httpx.MockTransport(respond))
    plan = await planner.plan_exclusive("人工服务结束了吗?")
    assert plan.intent == "general_chat"
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_synthesizes_only_from_closed_evidence_and_valid_sources() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "tools" not in payload
        assert "password" not in payload["messages"][1]["content"]
        assert "刀刀" not in payload["messages"][1]["content"]
        schema_name = payload["response_format"]["json_schema"]["name"]
        if schema_name == "grounding_verdict":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"supported":true,"unsupported_claims":[]}'}}
                    ]
                },
            )
        schema = payload["response_format"]["json_schema"]["schema"]
        assert "600 分是 ¥6.00" in payload["messages"][0]["content"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["cited_source_ids"]["items"]["enum"] == ["prd_public"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "这款商品当前公开价格为 12 元，库存以结算页为准。",
                                    "cited_source_ids": ["prd_public"],
                                    "confidence": "high",
                                    "limitation": "库存会实时变化",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    planner, client = _planner(httpx.MockTransport(respond))
    answer = await planner.synthesize(
        agent_prompt="只按证据回答",
        user_text="多少钱",
        intent="product_qa",
        evidence={
            "price": {"minor_units": "1200", "major_units": "12.00", "display": "¥12.00"},
            "conversation_window": {"recent_turns": [{"text": "AI: 刀刀你好"}]},
        },
        source_ids=("prd_public",),
    )
    assert answer.cited_source_ids == ("prd_public",)
    assert answer.confidence == "high"
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_accepts_moonshot_validated_source_ids_alias() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        schema_name = payload["response_format"]["json_schema"]["name"]
        content = (
            '{"supported":true,"unsupported_claims":[]}'
            if schema_name == "grounding_verdict"
            else '{"answer":"你好，请问想咨询什么?","source_ids":["context:assistant_scope"]}'
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    planner, client = _planner(httpx.MockTransport(respond))
    answer = await planner.synthesize(
        agent_prompt="安全回答",
        user_text="hello",
        intent="general_chat",
        evidence={"assistant_scope": "可以协助商城咨询。"},
        source_ids=("context:assistant_scope",),
    )
    assert answer.text == "你好，请问想咨询什么?"
    assert answer.cited_source_ids == ("context:assistant_scope",)
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_rejects_fabricated_source_identifier() -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"answer":"错误引用","cited_source_ids":["foreign"],'
                                '"confidence":"high","limitation":null}'
                            )
                        }
                    }
                ]
            },
        )

    planner, client = _planner(httpx.MockTransport(respond))
    with pytest.raises(ModelGatewayError, match="invalid grounded answer"):
        await planner.synthesize(
            agent_prompt="只按证据回答",
            user_text="问题",
            intent="product_qa",
            evidence={"value": "事实"},
            source_ids=("known",),
        )
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


@pytest.mark.asyncio
async def test_provider_health_probes_models_structured_stream_and_usage() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer model-secret"
        if request.method == "GET":
            assert request.url == httpx.URL("https://models.invalid/v1/models")
            return httpx.Response(
                200,
                json={"data": [{"id": "approved-model"}, {"id": "other-model"}]},
            )
        payload = json.loads(request.content)
        if payload.get("stream") is True:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=('data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n'),
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"total_tokens": 9},
            },
        )

    settings = Settings(
        _env_file=None,
        agent_model_api_url="https://models.invalid/v1/chat/completions",
        agent_model_api_key="model-secret",
        agent_model_name="approved-model",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        health = await probe_model_provider(settings, client=client)
    assert health.status == "available"
    assert health.model_available is True
    assert health.structured_output is True
    assert health.streaming is True
    assert health.usage_reporting is True
    assert health.available_models == ("approved-model", "other-model")
    assert "model-secret" not in json.dumps(health.cache_payload())


@pytest.mark.asyncio
async def test_provider_health_reports_unconfigured_without_network() -> None:
    health = await probe_model_provider(Settings(_env_file=None))
    assert health.status == "unconfigured"
    assert health.error_code == "MODEL_PROVIDER_NOT_CONFIGURED"
