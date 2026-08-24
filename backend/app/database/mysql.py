import asyncio
from collections.abc import AsyncIterator

from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def initialize_mysql(dsn: str) -> None:
    global _engine, _session_factory
    if _engine is not None:
        return
    settings = get_settings()
    _engine = create_async_engine(
        dsn,
        pool_pre_ping=True,
        pool_size=settings.mysql_pool_size,
        max_overflow=settings.mysql_max_overflow,
        pool_timeout=settings.mysql_pool_timeout_seconds,
        pool_recycle=1800,
    )
    SQLAlchemyInstrumentor().instrument(engine=_engine.sync_engine, enable_commenter=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def close_mysql() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def mysql_session() -> AsyncIterator[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("MySQL is not initialized")
    async with _session_factory() as session:
        yield session


async def probe_mysql(timeout_seconds: float) -> None:
    if _engine is None:
        raise RuntimeError("MySQL is not initialized")

    async def _probe() -> None:
        async with _engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    await asyncio.wait_for(_probe(), timeout=timeout_seconds)
