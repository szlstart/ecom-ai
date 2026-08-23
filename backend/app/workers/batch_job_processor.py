from __future__ import annotations

import asyncio
import signal

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.integrations.object_storage import get_object_storage
from app.modules.batch_jobs.processor import BatchJobProcessor

logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    storage = get_object_storage()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("batch_job_processor_started")
    try:
        while not stopping.is_set():
            processed = False
            async for session in mysql_session():
                processed = await BatchJobProcessor(session, storage).process_one()
            if processed:
                continue
            try:
                await asyncio.wait_for(stopping.wait(), timeout=2)
            except TimeoutError:
                pass
    finally:
        await close_mysql()
        logger.info("batch_job_processor_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
