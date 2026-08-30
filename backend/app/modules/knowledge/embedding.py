from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.config import Settings


class EmbeddingUnavailable(RuntimeError):
    """The optional semantic retrieval dependency is unavailable."""


class EmbeddingProvider(Protocol):
    @property
    def model_code(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class DisabledEmbeddingProvider:
    model_code: str
    dimension: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        _ = texts
        raise EmbeddingUnavailable("embedding provider is not configured")


@dataclass(frozen=True)
class HttpEmbeddingProvider:
    endpoint: str
    api_key: str
    model_code: str
    dimension: int
    timeout_seconds: float

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model_code, "input": list(texts)},
            )
            response.raise_for_status()
            payload = response.json()
        rows = sorted(payload.get("data", []), key=lambda item: int(item.get("index", 0)))
        vectors = [item.get("embedding") for item in rows]
        if len(vectors) != len(texts) or any(
            not isinstance(vector, list) or len(vector) != self.dimension for vector in vectors
        ):
            raise EmbeddingUnavailable("embedding provider returned an invalid shape")
        return [[float(value) for value in vector] for vector in vectors]


@dataclass(frozen=True)
class DashScopeMultimodalEmbeddingProvider:
    """DashScope native adapter for Tongyi multimodal embedding models.

    These models do not implement the OpenAI-compatible /embeddings contract. Each
    text is sent as an independent content item so returned indexes remain stable.
    """

    endpoint: str
    api_key: str
    model_code: str
    dimension: int
    timeout_seconds: float
    batch_size: int = 16

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start : start + self.batch_size]
                try:
                    response = await client.post(
                        self.endpoint,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.model_code,
                            "input": {"contents": [{"text": text} for text in batch]},
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise EmbeddingUnavailable("DashScope embedding request failed") from exc
                output = payload.get("output")
                rows = output.get("embeddings", []) if isinstance(output, dict) else []
                rows = sorted(rows, key=lambda item: int(item.get("index", 0)))
                batch_vectors = [item.get("embedding") for item in rows]
                if len(batch_vectors) != len(batch) or any(
                    not isinstance(vector, list) or len(vector) != self.dimension
                    for vector in batch_vectors
                ):
                    raise EmbeddingUnavailable("DashScope embedding returned an invalid shape")
                vectors.extend(
                    [float(value) for value in vector] for vector in batch_vectors
                )
        return vectors


def embedding_provider(settings: Settings) -> EmbeddingProvider:
    if not settings.embedding_api_url or settings.embedding_api_key is None:
        return DisabledEmbeddingProvider(settings.embedding_model, settings.embedding_dimension)
    provider_type = (
        DashScopeMultimodalEmbeddingProvider
        if settings.embedding_model.startswith(
            ("tongyi-embedding-vision-", "multimodal-embedding-")
        )
        else HttpEmbeddingProvider
    )
    endpoint = (
        _dashscope_multimodal_endpoint(settings.embedding_api_url)
        if provider_type is DashScopeMultimodalEmbeddingProvider
        else settings.embedding_api_url
    )
    return provider_type(
        endpoint=endpoint,
        api_key=settings.embedding_api_key.get_secret_value(),
        model_code=settings.embedding_model,
        dimension=settings.embedding_dimension,
        timeout_seconds=settings.embedding_timeout_seconds,
    )


def _dashscope_multimodal_endpoint(configured_url: str) -> str:
    parsed = urlsplit(configured_url)
    if parsed.hostname not in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"}:
        return configured_url
    return urlunsplit(
        (
            parsed.scheme or "https",
            parsed.netloc,
            "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding",
            "",
            "",
        )
    )


def vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(format(value, ".8g") for value in values) + "]"
