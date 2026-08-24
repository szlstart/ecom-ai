from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

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


def embedding_provider(settings: Settings) -> EmbeddingProvider:
    if not settings.embedding_api_url or settings.embedding_api_key is None:
        return DisabledEmbeddingProvider(settings.embedding_model, settings.embedding_dimension)
    return HttpEmbeddingProvider(
        endpoint=settings.embedding_api_url,
        api_key=settings.embedding_api_key.get_secret_value(),
        model_code=settings.embedding_model,
        dimension=settings.embedding_dimension,
        timeout_seconds=settings.embedding_timeout_seconds,
    )


def vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(format(value, ".8g") for value in values) + "]"
