from __future__ import annotations

import asyncio
import signal

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import SecurityService
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.modules.payments.service import PaymentService

logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    security = SecurityService(settings)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("payment_reconciler_started")
    try:
        while not stopping.is_set():
            processed = 0
            try:
                async for session in mysql_session():
                    processed = await PaymentService(session, security).reconcile_expired(
                        limit=settings.payment_reconcile_batch_size
                    )
            except Exception:
                logger.exception("payment_reconcile_batch_failed")
            if processed:
                logger.info("payment_reconcile_batch_completed", processed=processed)
                continue
            try:
                await asyncio.wait_for(
                    stopping.wait(), timeout=settings.payment_reconcile_poll_seconds
                )
            except TimeoutError:
                pass
    finally:
        await close_mysql()
        logger.info("payment_reconciler_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
