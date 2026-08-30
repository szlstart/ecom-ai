from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.modules.knowledge.embedding import (
    DashScopeMultimodalEmbeddingProvider,
    embedding_provider,
)


def test_tongyi_provider_uses_native_dashscope_multimodal_endpoint() -> None:
    provider = embedding_provider(
        Settings(
            embedding_api_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            embedding_api_key="test-key",
            embedding_model="tongyi-embedding-vision-flash",
            embedding_dimension=768,
        )
    )
    assert isinstance(provider, DashScopeMultimodalEmbeddingProvider)
    assert provider.endpoint.endswith(
        "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
    )


@pytest.mark.asyncio
async def test_dashscope_provider_maps_native_response_by_index() -> None:
    vector_a = [0.1] * 768
    vector_b = [0.2] * 768

    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer test-key"
        assert payload == {
            "model": "tongyi-embedding-vision-flash",
            "input": {"contents": [{"text": "你好"}, {"text": "商品图片"}]},
        }
        return httpx.Response(
            200,
            json={
                "output": {
                    "embeddings": [
                        {"index": 1, "embedding": vector_b, "type": "text"},
                        {"index": 0, "embedding": vector_a, "type": "text"},
                    ]
                }
            },
        )

    provider = DashScopeMultimodalEmbeddingProvider(
        endpoint="https://dashscope.invalid/native",
        api_key="test-key",
        model_code="tongyi-embedding-vision-flash",
        dimension=768,
        timeout_seconds=5,
    )
    transport = httpx.MockTransport(respond)
    original_client = httpx.AsyncClient

    def mock_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    # The adapter owns its short-lived client; replace only construction for this contract test.
    from app.modules.knowledge import embedding as embedding_module

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(embedding_module.httpx, "AsyncClient", mock_client)
    try:
        vectors = await provider.embed(["你好", "商品图片"])
    finally:
        monkeypatch.undo()
    assert vectors == [vector_a, vector_b]


def test_fixed_tongyi_dimension_is_rejected_when_mismatched() -> None:
    with pytest.raises(ValueError, match="requires embedding dimension 768"):
        Settings(
            embedding_model="tongyi-embedding-vision-flash",
            embedding_dimension=1536,
        )
