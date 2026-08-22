from __future__ import annotations

import asyncio
import signal

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.integrations.object_storage import get_object_storage
from app.modules.files.processor import FileProcessor
from app.modules.files.scanner import ClamAvScanner

logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    storage = get_object_storage()
    scanner = ClamAvScanner(settings)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("file_processor_started")
    try:
        while not stopping.is_set():
            processed = 0
            expired = 0
            async for session in mysql_session():
                processor = FileProcessor(session, storage, scanner)
                processed = await processor.process_batch()
                expired = await processor.expire_uploads()
            if processed or expired:
                logger.info(
                    "file_processor_batch_completed",
                    processed=processed,
                    expired_uploads=expired,
                )
                continue
            try:
                await asyncio.wait_for(stopping.wait(), timeout=2)
            except TimeoutError:
                pass
    finally:
        await close_mysql()
        logger.info("file_processor_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
