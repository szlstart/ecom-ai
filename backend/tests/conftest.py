import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

os.environ["ECOM_READINESS_CHECKS_ENABLED"] = "false"
os.environ.setdefault("ECOM_DEBUG_VERIFICATION_CODE", "000000")

from app.core.config import get_settings
from app.database.redis import get_redis
from app.main import create_app


async def _clear_test_auth_rate_limits() -> None:
    redis = get_redis()
    keys = [
        key
        for pattern in ("ecom:rl:auth:*", "ecom:rl:admin-auth:*")
        async for key in redis.scan_iter(match=pattern)
    ]
    if keys:
        await redis.delete(*keys)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    get_settings.cache_clear()
    app = create_app()
    async with app.router.lifespan_context(app):
        await _clear_test_auth_rate_limits()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as test_client:
            try:
                yield test_client
            finally:
                await _clear_test_auth_rate_limits()
