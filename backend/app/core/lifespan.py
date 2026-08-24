from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.telemetry import configure_telemetry, shutdown_telemetry
from app.database.mysql import close_mysql, initialize_mysql
from app.database.postgres import close_postgres, initialize_postgres
from app.database.redis import close_redis, initialize_redis


@asynccontextmanager
async def application_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_telemetry(settings)
    initialize_mysql(settings.mysql_dsn)
    initialize_postgres(settings.postgres_dsn)
    initialize_redis(settings.redis_url)
    try:
        yield
    finally:
        await close_redis()
        await close_postgres()
        await close_mysql()
        shutdown_telemetry()
