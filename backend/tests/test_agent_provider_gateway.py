from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.modules.agent_runtime.model_gateway import ModelGatewayError
from app.modules.agent_runtime.provider_gateway import (
    OpenAICompatiblePlanner,
    _bounded_evidence_json,
    _loads_model_json,
    _strip_untrusted_user_salutation,
    configured_model_gateways,
    model_failure_code,
    probe_model_provider,
)


def _decision_json(
    intent: str,
    *,
    capabilities: tuple[str, ...] = (),
    search_text: str | None = None,
    confidence: float = 0.94,
    missing_slots: tuple[str, ...] = (),
    continuation: bool = False,
    needs_human: bool = False,
    handoff_reason: str | None = None,
    strategy: str = "answer",
) -> str:
    return json.dumps(
        {
            "intent": intent,
            "confidence": confidence,
            "required_capabilities": list(capabilities),
            "missing_slots": list(missing_slots),
            "continuation_of_previous_turn": continuation,
            "needs_human": needs_human,
            "handoff_reason": handoff_reason,
            "response_strategy": strategy,
            "search_text": search_text,
        },
        ensure_ascii=False,
    )


def _verdict_json(
    supported: bool = True,
    *,
    unsupported_claims: tuple[str, ...] = (),
    citations: tuple[str, ...] = (),
    confidence: str = "high",
    limitation: str | None = None,
    answers_user_request: bool = True,
    missing_required_facts: tuple[str, ...] = (),
) -> str:
    return json.dumps(
        {
            "supported": supported,
            "unsupported_claims": list(unsupported_claims),
            "answers_user_request": answers_user_request,
            "missing_required_facts": list(missing_required_facts),
            "cited_source_ids": list(citations),
            "confidence": confidence,
            "limitation": limitation,
        },
        ensure_ascii=False,
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
        assert set(schema["required"]) == set(schema["properties"])
        assert "support.create_store_ticket" in schema["properties"][
            "required_capabilities"
        ]["items"]["enum"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": _decision_json(
                                "inventory_lookup",
                                capabilities=("catalog.get_inventory_availability",),
                            )
                        }
                    }
                ]
            },
        )

    planner, client = _planner(httpx.MockTransport(respond))
    plan = await planner.plan_store("这个 SKU 还有库存吗")
    assert plan.intent == "inventory_lookup"
    assert plan.search_text is None
    assert plan.confidence == 0.94
    assert plan.required_capabilities == ("catalog.get_inventory_availability",)
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_plan_rejects_capability_outside_selected_intent() -> None:
    async def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": _decision_json(
                                "product_qa",
                                capabilities=("support.create_store_ticket",),
                            )
                        }
                    }
                ]
            },
        )

    planner, client = _planner(httpx.MockTransport(respond))
    with pytest.raises(ModelGatewayError, match="outside the selected intent"):
        await planner.plan_store("介绍商品")
    await client.aclose()


@pytest.mark.asyncio
async def test_low_confidence_plan_with_missing_slot_is_forced_to_clarify() -> None:
    async def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": _decision_json(
                                "product_search",
                                confidence=0.42,
                                missing_slots=("想找的商品类型",),
                                strategy="answer",
                            )
                        }
                    }
                ]
            },
        )

    planner, client = _planner(httpx.MockTransport(respond))
    plan = await planner.plan_exclusive("帮我找一个")
    assert plan.response_strategy == "clarify"
    assert plan.missing_slots == ("想找的商品类型",)
    assert plan.required_capabilities == ("catalog.search_products",)
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
                    {
                        "message": {
                            "content": _decision_json(
                                "policy_qa", capabilities=("rag.policy.search",)
                            )
                        }
                    }
                ]
            },
        )

    planner, client = _planner(httpx.MockTransport(respond), temperature=1.0)
    plan = await planner.plan_exclusive("平台规则是什么")
    assert plan.intent == "policy_qa"
    await client.aclose()


@pytest.mark.asyncio
async def test_responses_wire_uses_reasoning_and_structured_output() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert payload["model"] == "gpt-5.4"
        assert payload["reasoning"] == {"effort": "low", "summary": "auto"}
        assert payload["text"]["format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "识别商品问题"}],
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": _decision_json(
                                    "product_qa", capabilities=("catalog.get_product",)
                                ),
                            }
                        ],
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    planner = OpenAICompatiblePlanner(
        api_url="https://models.invalid/v1",
        api_key="model-secret",
        model="gpt-5.4",
        wire_api="responses",
        timeout_seconds=5,
        client=client,
    )
    plan = await planner.plan_store("这个衣服最大码是多大?")
    assert plan.intent == "product_qa"
    await client.aclose()


@pytest.mark.asyncio
async def test_responses_wire_streams_public_reasoning_and_answer() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("stream") is True:
            assert payload["reasoning"] == {"effort": "medium", "summary": "detailed"}
            assert "罗列全部参数" in payload["input"][0]["content"][0]["text"]
            frames = [
                {"type": "response.reasoning_summary_part.added", "part": {"type": "summary_text"}},
                {"type": "response.reasoning_summary_text.delta", "delta": "先识别用户是在"},
                {"type": "response.reasoning_summary_text.delta", "delta": "询问商品特点。"},
                {"type": "response.reasoning_summary_part.added", "part": {"type": "summary_text"}},
                {"type": "response.reasoning_summary_text.delta", "delta": "再核对公开资料。"},
                {"type": "response.output_text.delta", "delta": "这款商品轻便耐用。"},
                {
                    "type": "response.completed",
                    "response": {
                        "usage": {"input_tokens": 21, "output_tokens": 9, "total_tokens": 30}
                    },
                },
            ]
            content = "".join(
                f"data: {json.dumps(frame, ensure_ascii=False)}\n\n" for frame in frames
            )
            return httpx.Response(200, text=content)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": _verdict_json(
                                    citations=("product:prd_public",)
                                ),
                            }
                        ],
                    }
                ]
            },
        )

    updates: list[tuple[str, str]] = []

    async def capture(kind: str, text: str) -> None:
        updates.append((kind, text))

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    planner = OpenAICompatiblePlanner(
        api_url="https://models.invalid/v1",
        api_key="model-secret",
        model="gpt-5.4",
        wire_api="responses",
        timeout_seconds=5,
        client=client,
    )
    answer = await planner.synthesize(
        agent_prompt="只按证据回答",
        user_text="介绍一下这个商品",
        intent="product_qa",
        evidence={"name": "轻便水杯", "material": "不锈钢"},
        source_ids=("product:prd_public",),
        stream_callback=capture,
    )
    assert answer.text == "这款商品轻便耐用。"
    assert answer.analysis_summary == "先识别用户是在询问商品特点。\n\n再核对公开资料。"
    assert answer.grounding_verified is True
    assert answer.cited_source_ids == ("product:prd_public",)
    assert answer.confidence == "high"
    assert answer.model_name == "gpt-5.4"
    assert (answer.input_tokens, answer.output_tokens, answer.total_tokens) == (21, 9, 30)
    assert answer.first_token_latency_ms is not None
    assert answer.model_latency_ms is not None
    assert answer.estimated_cost_usd is None
    assert updates == [
        ("reasoning", "先识别用户是在"),
        ("reasoning", "先识别用户是在询问商品特点。"),
        ("reasoning", "先识别用户是在询问商品特点。\n\n"),
        ("reasoning", "先识别用户是在询问商品特点。\n\n再核对公开资料。"),
        ("answer", "这款商品轻便耐用。"),
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_streamed_responses_grounding_verifier_retries_once() -> None:
    verification_attempts = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal verification_attempts
        payload = json.loads(request.content)
        if payload.get("stream") is True:
            frames = [
                {"type": "response.reasoning_summary_text.delta", "delta": "核对订单金额。"},
                {"type": "response.output_text.delta", "delta": "这笔订单实付 ¥6.00。"},
                {"type": "response.completed"},
            ]
            return httpx.Response(
                200,
                text="".join(
                    f"data: {json.dumps(frame, ensure_ascii=False)}\n\n" for frame in frames
                ),
            )
        verification_attempts += 1
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    _verdict_json(
                                        False,
                                        unsupported_claims=("首次误判",),
                                        confidence="low",
                                    )
                                    if verification_attempts == 1
                                    else _verdict_json(
                                        citations=("order:ord_public",)
                                    )
                                ),
                            }
                        ],
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    planner = OpenAICompatiblePlanner(
        api_url="https://models.invalid/v1",
        api_key="model-secret",
        model="gpt-5.4",
        wire_api="responses",
        timeout_seconds=5,
        client=client,
    )
    answer = await planner.synthesize(
        agent_prompt="只按证据回答",
        user_text="这笔订单实付多少钱?",
        intent="order_explain",
        evidence={"amounts": {"paid": {"display": "¥6.00"}}},
        source_ids=("order:ord_public",),
    )
    assert answer.text == "这笔订单实付 ¥6.00。"
    assert answer.grounding_verified is True
    assert answer.confidence == "high"
    assert answer.cited_source_ids == ("order:ord_public",)
    assert verification_attempts == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_streamed_responses_rejects_answer_after_two_grounding_failures() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("stream") is True:
            frames = [
                {"type": "response.output_text.delta", "delta": "该订单已退款 600 元。"},
                {"type": "response.completed"},
            ]
            return httpx.Response(
                200,
                text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames),
            )
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": _verdict_json(
                                    False,
                                    unsupported_claims=("金额错误",),
                                    confidence="low",
                                ),
                            }
                        ],
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    planner = OpenAICompatiblePlanner(
        api_url="https://models.invalid/v1",
        api_key="model-secret",
        model="gpt-5.4",
        wire_api="responses",
        timeout_seconds=5,
        client=client,
    )
    with pytest.raises(ModelGatewayError, match="claims outside trusted evidence"):
        await planner.synthesize(
            agent_prompt="只按证据回答",
            user_text="退款多少钱?",
            intent="refund_progress",
            evidence={"amount": {"display": "¥6.00"}},
            source_ids=("refund:ref_public",),
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_streamed_responses_rejects_answer_that_omits_available_requested_fact() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("stream") is True:
            frames = [
                {"type": "response.output_text.delta", "delta": "这件衣服有多个尺码可选。"},
                {"type": "response.completed"},
            ]
            return httpx.Response(
                200,
                text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames),
            )
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": _verdict_json(
                                    answers_user_request=False,
                                    missing_required_facts=("最大尺码 L",),
                                    confidence="low",
                                ),
                            }
                        ],
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    planner = OpenAICompatiblePlanner(
        api_url="https://models.invalid/v1",
        api_key="model-secret",
        model="gpt-5.4",
        wire_api="responses",
        timeout_seconds=5,
        client=client,
    )
    with pytest.raises(ModelGatewayError, match="omitted evidence-backed requested facts"):
        await planner.synthesize(
            agent_prompt="只按证据回答",
            user_text="这件衣服最大码是什么?",
            intent="product_qa",
            evidence={"skus": [{"sku_name": "S"}, {"sku_name": "M"}, {"sku_name": "L"}]},
            source_ids=("product:prd_public",),
        )
    assert model_failure_code(
        ModelGatewayError("model answer omitted evidence-backed requested facts"),
        "answer",
    ) == "answer_model_answer_incomplete"
    await client.aclose()


@pytest.mark.asyncio
async def test_responses_stream_falls_back_after_transient_primary_failure() -> None:
    seen: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload["model"])
        if payload["model"] == "overloaded-model":
            return httpx.Response(503, json={"error": {"type": "engine_overloaded_error"}})
        if payload.get("stream") is True:
            frames = [
                {"type": "response.output_text.delta", "delta": "当前有库存。"},
                {"type": "response.completed"},
            ]
            return httpx.Response(
                200,
                text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames),
            )
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": _verdict_json(),
                            }
                        ],
                    }
                ]
            },
        )

    updates: list[tuple[str, str]] = []

    async def capture(kind: str, text: str) -> None:
        updates.append((kind, text))

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    planner = OpenAICompatiblePlanner(
        api_url="https://models.invalid/v1",
        api_key="model-secret",
        model="overloaded-model",
        fallback_models=("fallback-model",),
        wire_api="responses",
        timeout_seconds=5,
        client=client,
    )
    answer = await planner.synthesize(
        agent_prompt="只按证据回答",
        user_text="还有库存吗?",
        intent="inventory_lookup",
        evidence={"available_quantity": 3},
        source_ids=("inventory:sku_public",),
        stream_callback=capture,
    )
    assert answer.text == "当前有库存。"
    assert seen == ["overloaded-model", "fallback-model", "fallback-model"]
    assert updates[:2] == [("reasoning_replace", ""), ("answer_replace", "")]
    await client.aclose()


@pytest.mark.asyncio
async def test_responses_stream_rejects_malformed_or_incomplete_sse() -> None:
    responses = iter(
        [
            httpx.Response(200, text="data: {not-json}\n\n"),
            httpx.Response(
                200,
                text='data: {"type":"response.output_text.delta","delta":"半截"}\n\n',
            ),
        ]
    )

    async def respond(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    planner = OpenAICompatiblePlanner(
        api_url="https://models.invalid/v1",
        api_key="model-secret",
        model="gpt-5.4",
        wire_api="responses",
        timeout_seconds=5,
        client=client,
    )
    payload = {"model": "gpt-5.4", "stream": True}
    with pytest.raises(ModelGatewayError, match="stream was invalid"):
        await planner._request_responses_stream(payload, stream_callback=None)
    with pytest.raises(ModelGatewayError, match="stream was invalid"):
        await planner._request_responses_stream(payload, stream_callback=None)
    await client.aclose()


def test_bounded_evidence_is_valid_json_and_reports_truncation() -> None:
    payload, truncated, fields = _bounded_evidence_json(
        {"product": {"name": "保留名称", "detail": "说明" * 30_000}},
        max_chars=1_200,
    )
    decoded = json.loads(payload)
    assert truncated is True
    assert len(payload) <= 1_200
    assert decoded["_evidence_budget"]["truncated"] is True
    assert decoded["product"]["name"] == "保留名称"
    assert fields == ("product",)


@pytest.mark.asyncio
async def test_provider_falls_back_only_after_transient_primary_failure() -> None:
    seen: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload["model"])
        if payload["model"] == "overloaded-model":
            return httpx.Response(
                429,
                json={"error": {"type": "engine_overloaded_error"}},
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": _decision_json(
                                "policy_qa", capabilities=("rag.policy.search",)
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    planner = OpenAICompatiblePlanner(
        api_url="https://models.invalid/v1/chat/completions",
        api_key="model-secret",
        model="overloaded-model",
        fallback_models=("fallback-model",),
        timeout_seconds=5,
        client=client,
    )
    plan = await planner.plan_exclusive("平台规则是什么")
    assert plan.intent == "policy_qa"
    assert seen == ["overloaded-model", "fallback-model"]
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
                    {
                        "message": {
                            "content": _decision_json(
                                "human_handoff",
                                capabilities=("support.create_platform_ticket",),
                                needs_human=True,
                                handoff_reason="历史消息提及人工",
                                strategy="handoff",
                            )
                        }
                    }
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
    assert plan.required_capabilities == ()
    assert plan.needs_human is False
    assert plan.handoff_reason is None
    assert plan.response_strategy == "answer"
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_cannot_reopen_handoff_for_a_status_question() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": _decision_json(
                                "human_handoff",
                                capabilities=("support.create_platform_ticket",),
                                needs_human=True,
                                handoff_reason="误判为人工请求",
                                strategy="handoff",
                            )
                        }
                    }
                ]
            },
        )

    planner, client = _planner(httpx.MockTransport(respond))
    plan = await planner.plan_exclusive("人工服务结束了吗?")
    assert plan.intent == "general_chat"
    assert plan.required_capabilities == ()
    assert plan.needs_human is False
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
                        {
                            "message": {
                                "content": _verdict_json(citations=("prd_public",))
                            }
                        }
                    ]
                },
            )
        assert "DIALOGUE_CONTINUITY_JSON" in payload["messages"][1]["content"]
        assert "你好" in payload["messages"][1]["content"]
        schema = payload["response_format"]["json_schema"]["schema"]
        assert "600 分是 ¥6.00" in payload["messages"][0]["content"]
        assert "remaining_refundable_quantity" in payload["messages"][0]["content"]
        assert "不要把 shipped" in payload["messages"][0]["content"]
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
async def test_provider_accepts_validated_source_ids_alias() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        schema_name = payload["response_format"]["json_schema"]["name"]
        content = (
            _verdict_json(citations=("context:assistant_scope",))
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
async def test_provider_accepts_compatible_public_answer_and_detail_shape_variants() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        schema_name = payload["response_format"]["json_schema"]["name"]
        if schema_name == "grounding_verdict":
            content = _verdict_json(citations=("product:prd_public",))
        else:
            content = json.dumps(
                {
                    "analysis": "这件商品最大尺码是 L。",
                    "source_ids": ["product:prd_public"],
                    "confidence": "high",
                    "limitation": None,
                    "analysis_summary": "已把问题识别为当前商品的尺码咨询。",
                    "analysis_details": "读取全部在售款式后，确认最大尺码为 L。",
                },
                ensure_ascii=False,
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": content,
                            "reasoning_content": "private provider reasoning",
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    planner = OpenAICompatiblePlanner(
        api_url="https://models.invalid/v1/chat/completions",
        api_key="model-secret",
        model="approved-model",
        timeout_seconds=5,
        client=client,
    )
    answer = await planner.synthesize(
        agent_prompt="只按证据回答",
        user_text="最大码是多大?",
        intent="product_qa",
        evidence={"product_id": "prd_public", "skus": [{"sku_name": "L"}]},
        source_ids=("product:prd_public",),
    )
    assert answer.text == "这件商品最大尺码是 L。"
    assert answer.analysis_details == ("读取全部在售款式后，确认最大尺码为 L。",)
    assert answer.thinking_used is True
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
                        {"message": {"content": _decision_json("admin_override")}}
                    ]
                },
            ),
            httpx.Response(200, json={"choices": []}),
        )
    )

    def respond(_: httpx.Request) -> httpx.Response:
        return next(responses)

    planner, client = _planner(httpx.MockTransport(respond))
    with pytest.raises(ModelGatewayError, match="unsupported Agent intent"):
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


def test_grounded_answer_guard_removes_only_invented_leading_name() -> None:
    assert _strip_untrusted_user_salutation("刀锋您好，订单已签收。") == "您好，订单已签收。"
    assert _strip_untrusted_user_salutation("您好，订单已签收。") == "您好，订单已签收。"
    assert _strip_untrusted_user_salutation("订单已签收。") == "订单已签收。"
    assert _strip_untrusted_user_salutation("刀根据当前数据，运行正常。") == (
        "根据当前数据，运行正常。"
    )
    assert _strip_untrusted_user_salutation("刀锋绿杆铅笔有 3 个款式。") == (
        "绿杆铅笔有 3 个款式。"
    )
    assert _strip_untrusted_user_salutation("刀刀，订单已签收。") == "订单已签收。"
    assert _strip_untrusted_user_salutation("刀最近的订单如下\uff1a") == "最近的订单如下\uff1a"
    assert _strip_untrusted_user_salutation("刀本地模拟充值不会真实扣款。") == (
        "本地模拟充值不会真实扣款。"
    )


def test_compatible_json_parser_repairs_only_controls_inside_strings() -> None:
    assert _loads_model_json('{"answer":"第一行\n第二行","ok":true}') == {
        "answer": "第一行\n第二行",
        "ok": True,
    }
    assert _loads_model_json('```json\n{"answer":"正常"}\n```') == {"answer": "正常"}


def test_model_failure_codes_do_not_include_prompt_or_provider_output() -> None:
    assert (
        model_failure_code(
            ModelGatewayError("model answer contains claims outside trusted evidence"),
            "answer",
        )
        == "answer_model_grounding_rejected"
    )
    assert model_failure_code(TimeoutError("private message"), "planning") == (
        "planning_model_timeout"
    )
    assert model_failure_code(ModelGatewayError("model answer scope mismatch"), "answer") == (
        "answer_model_scope_mismatch"
    )
