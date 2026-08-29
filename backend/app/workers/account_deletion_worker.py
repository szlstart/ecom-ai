from __future__ import annotations

import asyncio
import signal
from datetime import timedelta

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.logging import configure_logging
from app.core.security import utc_now
from app.core.worker_health import start_worker_heartbeat
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.database.postgres import close_postgres, initialize_postgres, postgres_session
from app.database.redis import close_redis, get_redis, initialize_redis
from app.integrations.object_storage import ObjectStorage, get_object_storage
from app.modules.finance.account_deletion import (
    AccountDeletionService,
    selected_ids,
    storage_objects,
)
from app.modules.finance.models import AccountDeletionTask
from app.modules.system.models import OutboxEvent

logger = structlog.get_logger(__name__)


async def process_one(storage: ObjectStorage | None) -> bool:
    async for mysql in mysql_session():
        now = utc_now()
        task = await mysql.scalar(
            select(AccountDeletionTask)
            .where(
                or_(
                    AccountDeletionTask.task_status.in_(("requested", "retryable")),
                    (
                        (AccountDeletionTask.task_status == "running")
                        & (AccountDeletionTask.available_at <= now)
                    ),
                ),
                AccountDeletionTask.available_at <= now,
            )
            .order_by(AccountDeletionTask.created_at, AccountDeletionTask.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if task is None:
            return False
        task.task_status = "running"
        task.current_phase = "external_cleanup"
        task.attempt_count += 1
        task.started_at = task.started_at or now
        task.available_at = now + timedelta(minutes=5)
        task.last_error_code = None
        task.last_error = None
        task.version += 1
        task_no = task.task_no
        user_no = task.user_no
        store_nos = list(task.store_nos)
        inventory = dict(task.inventory)
        trace_id = task.trace_id
        attempt_count = task.attempt_count
        max_attempts = task.max_attempts
        await mysql.commit()

        try:
            await _remove_objects(storage, inventory)
            await _purge_redis_identity(user_no)
            await _phase(mysql, task_no, "postgres_cleanup")
            async for postgres in postgres_session():
                await AccountDeletionService(mysql).purge_postgres(
                    postgres, user_no, store_nos
                )
            await _phase(mysql, task_no, "mysql_finalizing")
            selected = selected_ids(inventory)
            user_ids = selected.get("users", set())
            if len(user_ids) != 1:
                raise ValueError("account deletion inventory must contain exactly one user")
            await AccountDeletionService(mysql).delete_mysql_inventory(
                selected,
                user_id=next(iter(user_ids)),
                user_no=user_no,
                store_nos=store_nos,
            )
            completed = await mysql.scalar(
                select(AccountDeletionTask)
                .where(AccountDeletionTask.task_no == task_no)
                .with_for_update()
            )
            if completed is None:
                raise RuntimeError("account deletion task audit row was removed")
            completed.task_status = "completed"
            completed.current_phase = "completed"
            completed.completed_at = utc_now()
            completed.last_error_code = None
            completed.last_error = None
            completed.version += 1
            mysql.add(
                OutboxEvent(
                    event_no=new_prefixed_ulid("evt_"),
                    event_type="account.deletion.completed.v1",
                    aggregate_type="account_deletion_task",
                    aggregate_no=task_no,
                    aggregate_version=completed.version,
                    payload={
                        "task_id": task_no,
                        "subject_type": completed.subject_type,
                    },
                    event_status="pending",
                    available_at=utc_now(),
                    trace_id=trace_id,
                )
            )
            await mysql.commit()
            logger.info("account_deletion_completed", task_id=task_no)
        except Exception as exc:
            await mysql.rollback()
            failed = await mysql.scalar(
                select(AccountDeletionTask)
                .where(AccountDeletionTask.task_no == task_no)
                .with_for_update()
            )
            if failed is not None and failed.task_status != "completed":
                failed.task_status = (
                    "manual_review" if attempt_count >= max_attempts else "retryable"
                )
                failed.available_at = utc_now() + timedelta(
                    seconds=min(3600, 2 ** min(attempt_count, 12))
                )
                failed.last_error_code = _error_code(exc)
                failed.last_error = "跨系统清理未完成，系统将安全重试。"[:1000]
                failed.version += 1
                await mysql.commit()
            logger.exception("account_deletion_failed", task_id=task_no)
        return True
    return False


async def _phase(mysql: AsyncSession, task_no: str, phase: str) -> AccountDeletionTask:
    # Kept as a small helper so every external boundary has a durable checkpoint.
    task = await mysql.scalar(
        select(AccountDeletionTask)
        .where(AccountDeletionTask.task_no == task_no)
        .with_for_update()
    )
    if task is None:
        raise RuntimeError("account deletion task is missing")
    task.current_phase = phase
    task.version += 1
    await mysql.commit()
    return task


async def _remove_objects(
    storage: ObjectStorage | None, inventory: dict[str, object]
) -> None:
    objects = storage_objects(inventory)
    if objects and storage is None:
        raise RuntimeError("object storage is disabled while deletion inventory contains files")
    if storage is None:
        return
    for bucket, object_key in objects:
        try:
            await storage.remove(bucket, object_key)
        except ApplicationError as exc:
            if exc.code != "OBJECT_STORAGE_OBJECT_NOT_FOUND":
                raise


async def _purge_redis_identity(user_no: str) -> None:
    redis = get_redis()
    batch: list[str | bytes] = []
    async for key in redis.scan_iter(match=f"ecom:*:{user_no}*", count=200):
        batch.append(key)
        if len(batch) >= 200:
            await redis.delete(*batch)
            batch.clear()
    if batch:
        await redis.delete(*batch)


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ApplicationError):
        return exc.code[:64]
    if isinstance(exc, ValueError):
        return "ACCOUNT_DELETION_INVENTORY_INVALID"
    return "ACCOUNT_DELETION_STEP_FAILED"


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    initialize_postgres(settings.postgres_dsn)
    initialize_redis(settings.redis_url)
    storage = get_object_storage() if settings.object_storage_enabled else None
    stopping = asyncio.Event()
    start_worker_heartbeat("account-deletion-worker", settings, stopping)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("account_deletion_worker_started")
    try:
        while not stopping.is_set():
            if await process_one(storage):
                continue
            try:
                await asyncio.wait_for(stopping.wait(), timeout=2)
            except TimeoutError:
                pass
    finally:
        await close_redis()
        await close_postgres()
        await close_mysql()
        logger.info("account_deletion_worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
