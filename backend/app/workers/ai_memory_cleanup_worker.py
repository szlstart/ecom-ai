from __future__ import annotations

import asyncio
import signal

import structlog
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.logging import configure_logging
from app.core.security import utc_now
from app.core.worker_health import start_worker_heartbeat
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.database.postgres import close_postgres, initialize_postgres, postgres_session
from app.modules.agent_runtime.models import AiMemoryCleanupTask
from app.modules.identity.models import User

logger = structlog.get_logger(__name__)


async def process_one() -> bool:
    async for mysql in mysql_session():
        task = await mysql.scalar(
            select(AiMemoryCleanupTask)
            .where(AiMemoryCleanupTask.task_status == "queued")
            .order_by(AiMemoryCleanupTask.created_at, AiMemoryCleanupTask.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if task is None:
            return False
        user = await mysql.get(User, task.user_id)
        if user is None:
            task.task_status = "failed"
            task.error_code = "CLEANUP_OWNER_NOT_FOUND"
            task.failed_count = max(task.total_count, 1)
            task.completed_at = utc_now()
            task.version += 1
            await mysql.commit()
            return True
        task.task_status = "running"
        task.started_at = utc_now()
        task.error_code = None
        task.version += 1
        task_no = task.task_no
        command_type = task.command_type
        source_resource_no = task.source_resource_no
        total_count = task.total_count
        processed_before = task.processed_count
        running_version = task.version
        user_no = user.user_no
        await mysql.commit()
        try:
            async for postgres in postgres_session():
                if command_type == "memory_delete":
                    await postgres.execute(
                        text(
                            """DELETE FROM memory.item_embeddings WHERE memory_id IN
                            (SELECT id FROM memory.items WHERE user_no=:user_no
                             AND memory_no=:memory_no)"""
                        ),
                        {"user_no": user_no, "memory_no": source_resource_no},
                    )
                    await postgres.execute(
                        text(
                            """UPDATE memory.items SET content_ciphertext=NULL,
                            updated_at=now() WHERE user_no=:user_no AND memory_no=:memory_no
                            AND memory_status='deleted'"""
                        ),
                        {"user_no": user_no, "memory_no": source_resource_no},
                    )
                elif command_type in {"disable_all", "consent_revoke", "scope_pause"}:
                    revoked = (
                        await postgres.execute(
                            text(
                                """WITH targets AS MATERIALIZED (
                                SELECT id,memory_status AS from_status FROM memory.items
                                WHERE user_no=:user_no
                                AND memory_status IN ('active','candidate') FOR UPDATE
                                )
                                UPDATE memory.items item SET memory_status='revoked',
                                content_ciphertext=NULL, version=item.version+1, updated_at=now()
                                FROM targets WHERE item.id=targets.id
                                RETURNING item.id,item.memory_no,item.content_hash,
                                targets.from_status"""
                            ),
                            {"user_no": user_no},
                        )
                    ).mappings().all()
                    if revoked:
                        await postgres.execute(
                            text(
                                "DELETE FROM memory.item_embeddings WHERE memory_id = ANY(:ids)"
                            ),
                            {"ids": [row["id"] for row in revoked]},
                        )
                    for row in revoked:
                        await postgres.execute(
                            text(
                                """INSERT INTO memory.events
                                (event_no,memory_id,event_type,actor_type,reason_code,user_no,
                                 from_status,to_status,actor_no,content_hash_before,
                                 content_hash_after,trace_id,metadata_redacted)
                                VALUES (:event_no,:memory_id,'revoked','system',:reason_code,
                                 :user_no,:from_status,'revoked',:task_no,:content_hash,NULL,
                                 :trace_id,'{}'::jsonb)"""
                            ),
                            {
                                "event_no": new_prefixed_ulid("mev_"),
                                "memory_id": row["id"],
                                "reason_code": command_type,
                                "user_no": user_no,
                                "from_status": row["from_status"],
                                "task_no": task_no,
                                "content_hash": row["content_hash"],
                                "trace_id": task_no,
                            },
                        )
                else:
                    raise RuntimeError("unsupported AI memory cleanup command")
                processed = total_count
                await postgres.commit()
            task.task_status = "succeeded"
            task.processed_count = processed
            task.failed_count = 0
            task.error_code = None
        except Exception:
            task.task_status = "failed"
            task.failed_count = max(total_count - processed_before, 1)
            task.error_code = "AI_MEMORY_CLEANUP_FAILED"
            logger.exception("ai_memory_cleanup_failed", cleanup_task_id=task_no)
        task.completed_at = utc_now()
        task.version = running_version + 1
        await mysql.commit()
        return True
    return False


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    initialize_postgres(settings.postgres_dsn)
    stopping = asyncio.Event()
    start_worker_heartbeat("ai-memory-cleanup-worker", settings, stopping)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("ai_memory_cleanup_worker_started")
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
        logger.info("ai_memory_cleanup_worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
