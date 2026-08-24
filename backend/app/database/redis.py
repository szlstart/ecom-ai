import asyncio
from collections.abc import Awaitable
from typing import cast

from opentelemetry.instrumentation.redis import RedisInstrumentor
from redis.asyncio import Redis

from app.core.config import get_settings

_client: Redis | None = None


def initialize_redis(url: str) -> None:
    global _client
    if _client is None:
        RedisInstrumentor().instrument()
        _client = Redis.from_url(
            url,
            decode_responses=True,
            max_connections=get_settings().redis_max_connections,
        )


def get_redis() -> Redis:
    if _client is None:
        raise RuntimeError("Redis is not initialized")
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def probe_redis(timeout_seconds: float) -> None:
    client = get_redis()
    ping = cast(Awaitable[bool], client.ping())
    result = await asyncio.wait_for(ping, timeout=timeout_seconds)
    if result is not True:
        raise RuntimeError("Unexpected Redis PING response")
