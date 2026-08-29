from __future__ import annotations

import asyncio
import signal

import structlog
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.database.redis import close_redis, get_redis, initialize_redis
from app.modules.events.dispatcher import DomainEventDispatcher
from app.modules.realtime.relay import RealtimeOutboxRelay
from app.modules.reviews.moderation import ReviewModerationProcessor

logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    initialize_redis(settings.redis_url)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("realtime_outbox_relay_started")
    try:
        while not stopping.is_set():
            processed = 0
            try:
                async for session in mysql_session():
                    review_count = await ReviewModerationProcessor(session).process_batch(
                        settings.realtime_outbox_batch_size
                    )
                    processed = review_count
                    realtime_count = await RealtimeOutboxRelay(
                        session, get_redis(), settings
                    ).process_batch(settings.realtime_outbox_batch_size)
                    dispatcher = DomainEventDispatcher(session, get_redis(), settings.environment)
                    domain_count = await dispatcher.process_batch(
                        settings.realtime_outbox_batch_size
                    )
                    dead_letter_count = await dispatcher.reconcile_failed(
                        settings.realtime_outbox_batch_size
                    )
                    processed += realtime_count + domain_count + dead_letter_count
            except RedisError:
                logger.warning("realtime_outbox_redis_unavailable")
            except Exception:
                logger.exception("realtime_outbox_batch_failed")
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    stopping.wait(), timeout=settings.realtime_outbox_poll_seconds
                )
            except TimeoutError:
                pass
    finally:
        await close_redis()
        await close_mysql()
        logger.info("realtime_outbox_relay_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
