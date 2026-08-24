from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timedelta

import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.logging import configure_logging
from app.core.security import utc_now
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.database.postgres import close_postgres, initialize_postgres, postgres_session
from app.modules.agent_runtime.checkpoints import AgentCheckpointStore
from app.modules.agent_runtime.models import AgentRun
from app.modules.agent_runtime.service import AgentRuntimeService
from app.modules.agent_runtime.store_agent import process_store_run
from app.modules.messaging.models import Conversation, Message
from app.modules.system.models import OutboxEvent

logger = structlog.get_logger(__name__)


async def dispatch_response_requests(limit: int = 50) -> int:
    dispatched = 0
    now = utc_now()
    async for session in mysql_session():
        events = list(
            (
                await session.scalars(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.event_type == "message.response.requested.v1",
                        OutboxEvent.event_status == "pending",
                        OutboxEvent.available_at <= now,
                    )
                    .order_by(OutboxEvent.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        service = AgentRuntimeService(session)
        for event in events:
            conversation_no = event.payload.get("conversation_id")
            message_no = event.payload.get("message_id")
            if not isinstance(conversation_no, str) or not isinstance(message_no, str):
                _reject_event(event, "AGENT_EVENT_PAYLOAD_INVALID", now)
                continue
            row = (
                await session.execute(
                    select(Conversation, Message)
                    .join(Message, Message.conversation_id == Conversation.id)
                    .where(
                        Conversation.conversation_no == conversation_no,
                        Message.message_no == message_no,
                        Message.sender_type == "user",
                        Message.message_status == "sent",
                    )
                )
            ).one_or_none()
            if row is None:
                _reject_event(event, "AGENT_TRIGGER_NOT_FOUND", now)
                continue
            conversation, message = row
            if conversation.conversation_type != "store":
                event.available_at = now + timedelta(minutes=1)
                event.last_error_code = "AGENT_TYPE_NOT_RELEASED"
                event.attempt_count += 1
                continue
            refs = event.payload.get("context_refs", [])
            if not isinstance(refs, list) or any(not isinstance(item, dict) for item in refs):
                _reject_event(event, "AGENT_CONTEXT_SNAPSHOT_INVALID", now)
                continue
            try:
                await service.enqueue_for_message(
                    conversation,
                    message,
                    event.trace_id,
                    context_snapshot=refs,
                )
            except ApplicationError as exc:
                event.attempt_count += 1
                event.last_error_code = exc.code
                event.available_at = now + timedelta(seconds=min(60, 2**event.attempt_count))
                if event.attempt_count >= 8:
                    event.event_status = "failed"
                continue
            event.event_status = "published"
            event.published_at = now
            event.last_error_code = None
            dispatched += 1
        await session.commit()
    return dispatched


async def process_batch(limit: int = 20) -> int:
    processed = 0
    async for session in mysql_session():
        runs = list(
            (
                await session.scalars(
                    select(AgentRun)
                    .where(AgentRun.run_status == "queued")
                    .order_by(AgentRun.created_at, AgentRun.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        async for checkpoint_session in postgres_session():
            checkpoint_store = AgentCheckpointStore(checkpoint_session)
            for run in runs:
                await process_store_run(
                    session,
                    run,
                    checkpoint_store=checkpoint_store,
                )
                processed += 1
        await session.commit()
    return processed


def _reject_event(event: OutboxEvent, code: str, now: datetime) -> None:
    event.event_status = "failed"
    event.last_error_code = code
    event.attempt_count += 1
    event.published_at = now


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    initialize_postgres(settings.postgres_dsn)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("agent_runtime_worker_started")
    try:
        while not stopping.is_set():
            dispatched = await dispatch_response_requests()
            processed = await process_batch()
            if dispatched or processed:
                continue
            try:
                await asyncio.wait_for(stopping.wait(), timeout=1)
            except TimeoutError:
                pass
    finally:
        await close_postgres()
        await close_mysql()
        logger.info("agent_runtime_worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
