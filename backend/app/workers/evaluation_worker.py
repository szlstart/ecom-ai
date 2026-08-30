from __future__ import annotations

import asyncio
import signal

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.worker_health import start_worker_heartbeat
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.modules.evaluation.processor import EvaluationProcessor

logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    stopping = asyncio.Event()
    start_worker_heartbeat("evaluation-worker", settings, stopping)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("evaluation_worker_started")
    try:
        while not stopping.is_set():
            processed = False
            async for session in mysql_session():
                processed = await EvaluationProcessor(session).process_one()
            if processed:
                continue
            try:
                await asyncio.wait_for(stopping.wait(), timeout=2)
            except TimeoutError:
                pass
    finally:
        await close_mysql()
        logger.info("evaluation_worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
