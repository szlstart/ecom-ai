from __future__ import annotations

import asyncio
import signal

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.modules.system.lifecycle import LifecycleProcessor

logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("lifecycle_worker_started", build_sha=settings.build_sha)
    try:
        while not stopping.is_set():
            processed = 0
            try:
                async for session in mysql_session():
                    result = await LifecycleProcessor(session, settings).process_batch()
                    processed = result.processed
                    if processed:
                        logger.info("lifecycle_batch_completed", **result.__dict__)
            except Exception:
                logger.exception("lifecycle_batch_failed")
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    stopping.wait(), timeout=settings.lifecycle_poll_seconds
                )
            except TimeoutError:
                pass
    finally:
        await close_mysql()
        logger.info("lifecycle_worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
