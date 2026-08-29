from __future__ import annotations

import asyncio
import signal

import structlog
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import utc_now
from app.core.worker_health import start_worker_heartbeat
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.database.postgres import close_postgres, initialize_postgres, postgres_session
from app.modules.knowledge.embedding import embedding_provider
from app.modules.knowledge.indexing import reconcile_index_job, run_index_job
from app.modules.knowledge.models import KnowledgeDocument
from app.modules.system.models import AdminBatchJob

logger = structlog.get_logger(__name__)


async def process_one() -> bool:
    settings = get_settings()
    async for mysql in mysql_session():
        command = await mysql.scalar(
            select(AdminBatchJob)
            .where(
                AdminBatchJob.execution_backend == "postgres_knowledge",
                AdminBatchJob.job_type == "knowledge_index",
                AdminBatchJob.job_status == "queued",
            )
            .order_by(AdminBatchJob.created_at, AdminBatchJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if command is None:
            return False
        document_no = command.request_config.get("document_no")
        content_version = command.request_config.get("content_version")
        document = await mysql.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.document_no == document_no,
                KnowledgeDocument.content_version == content_version,
                KnowledgeDocument.document_status == "published",
            )
        )
        if document is None:
            command.job_status = "failed"
            command.error_code = "KNOWLEDGE_DOCUMENT_VERSION_STALE"
            command.failure_count = 1
            await mysql.commit()
            return True
        command.job_status = "running"
        command.started_at = utc_now()
        command.execution_status_version += 1
        await mysql.commit()
        async for postgres in postgres_session():
            try:
                embedder = embedding_provider(settings)
                if command.request_config.get("embedding_model_code") != embedder.model_code:
                    raise RuntimeError("knowledge job embedding model snapshot is unavailable")
                await run_index_job(
                    postgres,
                    document,
                    command.execution_job_no or "",
                    embedder,
                )
            except Exception:
                await postgres.rollback()
                await postgres.execute(
                    text("""UPDATE knowledge.indexing_jobs
                    SET job_status='failed', progress=100, error_code='KNOWLEDGE_INDEX_FAILED',
                        error_owner='execution', status_version=status_version+1, updated_at=now()
                    WHERE command_job_no=:command_job_no
                      AND job_status IN ('queued','running')"""),
                    {"command_job_no": command.job_no},
                )
                await postgres.commit()
                logger.exception("knowledge_index_failed", command_job_no=command.job_no)
            await reconcile_index_job(mysql, postgres, command.job_no)
        return True
    return False


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    initialize_postgres(settings.postgres_dsn)
    stopping = asyncio.Event()
    start_worker_heartbeat("knowledge-indexer", settings, stopping)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("knowledge_indexer_started")
    try:
        while not stopping.is_set():
            if await process_one():
                continue
            try:
                await asyncio.wait_for(stopping.wait(), timeout=2)
            except TimeoutError:
                pass
    finally:
        await close_postgres()
        await close_mysql()
        logger.info("knowledge_indexer_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
