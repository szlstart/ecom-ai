from __future__ import annotations

import asyncio
import signal
import time
from typing import cast

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.worker_health import start_worker_heartbeat
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.integrations.object_storage import get_object_storage
from app.modules.files.processor import FileProcessor
from app.modules.files.reconciliation import (
    FileGarbageCollector,
    FileReconciler,
    ObjectInventoryStorage,
)
from app.modules.files.scanner import ClamAvScanner

logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    storage = get_object_storage()
    scanner = ClamAvScanner(settings)
    inventory_storage = cast(ObjectInventoryStorage, storage)
    stopping = asyncio.Event()
    start_worker_heartbeat("file-worker", settings, stopping)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("file_processor_started")
    next_reconciliation = 0.0
    next_gc = 0.0
    try:
        while not stopping.is_set():
            processed = 0
            expired = 0
            async for session in mysql_session():
                processor = FileProcessor(session, storage, scanner)
                processed = await processor.process_batch()
                expired = await processor.expire_uploads()
                if time.monotonic() >= next_gc:
                    next_gc = time.monotonic() + settings.file_gc_interval_seconds
                    try:
                        gc_result = await FileGarbageCollector(
                            session, inventory_storage, settings
                        ).collect(settings.file_gc_batch_size)
                        if gc_result.deleted_files or gc_result.failed_deletions:
                            logger.info("file_gc_completed", **gc_result.__dict__)
                    except Exception:
                        await session.rollback()
                        logger.exception("file_gc_failed")
                if time.monotonic() >= next_reconciliation:
                    next_reconciliation = (
                        time.monotonic() + settings.file_reconciliation_interval_seconds
                    )
                    try:
                        result = await FileReconciler(
                            session, inventory_storage, settings
                        ).reconcile()
                        logger.info(
                            "file_reconciliation_completed",
                            **result.__dict__,
                        )
                    except Exception:
                        await session.rollback()
                        logger.exception("file_reconciliation_failed")
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
