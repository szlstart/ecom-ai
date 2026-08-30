from __future__ import annotations

import asyncio

from app.bootstrap.default_knowledge import seed_default_knowledge
from app.bootstrap.reference_data import seed_reference_data
from app.core.config import get_settings
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.database.postgres import close_postgres, initialize_postgres, postgres_session


async def run() -> None:
    settings = get_settings()
    initialize_mysql(settings.mysql_dsn)
    initialize_postgres(settings.postgres_dsn)
    try:
        async for session in mysql_session():
            await seed_reference_data(session)
            async for postgres in postgres_session():
                await seed_default_knowledge(session, postgres)
    finally:
        await close_postgres()
        await close_mysql()


if __name__ == "__main__":
    asyncio.run(run())
