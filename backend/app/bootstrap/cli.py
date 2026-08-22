from __future__ import annotations

import asyncio

from app.bootstrap.reference_data import seed_reference_data
from app.core.config import get_settings
from app.database.mysql import close_mysql, initialize_mysql, mysql_session


async def run() -> None:
    initialize_mysql(get_settings().mysql_dsn)
    try:
        async for session in mysql_session():
            await seed_reference_data(session)
    finally:
        await close_mysql()


if __name__ == "__main__":
    asyncio.run(run())
