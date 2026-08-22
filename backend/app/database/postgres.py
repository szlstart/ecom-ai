import asyncio
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def initialize_postgres(dsn: str) -> None:
    global _engine, _session_factory
    if _engine is not None:
        return
    _engine = create_async_engine(
        dsn,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def close_postgres() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def postgres_session() -> AsyncIterator[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("PostgreSQL is not initialized")
    async with _session_factory() as session:
        yield session


async def probe_postgres(timeout_seconds: float) -> None:
    if _engine is None:
        raise RuntimeError("PostgreSQL is not initialized")

    async def _probe() -> None:
        async with _engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    await asyncio.wait_for(_probe(), timeout=timeout_seconds)
