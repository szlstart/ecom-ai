from __future__ import annotations

import asyncio
import signal

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import SecurityService
from app.core.worker_health import (
    record_worker_failure,
    record_worker_success,
    start_worker_heartbeat,
)
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.modules.logistics.service import LogisticsService

logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    security = SecurityService(settings)
    stopping = asyncio.Event()
    start_worker_heartbeat("logistics-sync-worker", settings, stopping)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("logistics_synchronizer_started")
    try:
        while not stopping.is_set():
            processed = 0
            try:
                async for session in mysql_session():
                    processed = await LogisticsService(
                        session,
                        security,
                        settings.security_hmac_secret.get_secret_value(),
                    ).sync_due(
                        limit=settings.logistics_sync_batch_size,
                        stale_after_seconds=settings.logistics_sync_stale_seconds,
                    )
                record_worker_success()
            except Exception as exc:
                record_worker_failure(type(exc).__name__)
                logger.exception(
                    "logistics_sync_batch_failed",
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                )
            if processed:
                logger.info("logistics_sync_batch_completed", processed=processed)
                continue
            try:
                await asyncio.wait_for(
                    stopping.wait(), timeout=settings.logistics_sync_poll_seconds
                )
            except TimeoutError:
                pass
    finally:
        await close_mysql()
        logger.info("logistics_synchronizer_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
