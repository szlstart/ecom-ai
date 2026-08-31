from __future__ import annotations

import asyncio
import signal
import time
from datetime import datetime, timedelta

import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.logging import configure_logging
from app.core.observability import AiMetric, metrics
from app.core.security import SecurityService, utc_now
from app.core.telemetry import configure_telemetry, shutdown_telemetry, traced_operation
from app.core.worker_health import start_worker_heartbeat
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.database.postgres import close_postgres, initialize_postgres, postgres_session
from app.database.redis import close_redis, get_redis, initialize_redis
from app.modules.agent_runtime.approval_service import AgentApprovalService
from app.modules.agent_runtime.checkpoints import AgentCheckpointStore
from app.modules.agent_runtime.exclusive_agent import process_exclusive_run
from app.modules.agent_runtime.live_stream import AgentLiveStreamPublisher
from app.modules.agent_runtime.models import AgentDefinition, AgentRun, AgentVersion
from app.modules.agent_runtime.operations_agent import process_operations_run
from app.modules.agent_runtime.provider_gateway import (
    configured_model_gateways,
    configured_operations_gateway,
    probe_model_provider,
)
from app.modules.agent_runtime.public_trace import public_question
from app.modules.agent_runtime.service import AgentRuntimeService
from app.modules.agent_runtime.store_agent import process_store_run
from app.modules.agent_runtime.trigger_text import agent_trace_question
from app.modules.identity.models import User
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
    settings = get_settings()
    security = SecurityService(settings)
    store_model_gateway, exclusive_model_gateway = configured_model_gateways(settings)
    operations_model_gateway = configured_operations_gateway(settings)
    redis = get_redis()
    run_nos: list[str] = []
    async for session in mysql_session():
        await AgentApprovalService(session, settings, security).reconcile_unknown(limit=limit)
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
        for run in runs:
            run.run_status = "running"
            run.current_phase = "claimed"
            run.version += 1
            run_nos.append(run.run_no)
            conversation = await session.get(Conversation, run.conversation_id)
            trigger = await session.get(Message, run.trigger_message_id)
            if conversation is not None and trigger is not None:
                session.add(
                    OutboxEvent(
                        event_no=new_prefixed_ulid("evt_"),
                        event_type="agent.response.started.v1",
                        aggregate_type="conversation",
                        aggregate_no=conversation.conversation_no,
                        aggregate_version=conversation.version,
                        payload={
                            "conversation_id": conversation.conversation_no,
                            "run_id": run.run_no,
                            "question": public_question(agent_trace_question(trigger)),
                            "stage": "understanding",
                            "label": "思考开始",
                            "summary": "正在识别问题、会话上下文、身份范围和可用权限。",
                        },
                        event_status="pending",
                        available_at=utc_now(),
                        attempt_count=0,
                        trace_id=run.trace_id,
                    )
                )
        await session.commit()

    for run_no in run_nos:
        async for session in mysql_session():
            claimed_run = await session.scalar(select(AgentRun).where(AgentRun.run_no == run_no))
            if (
                claimed_run is None
                or claimed_run.run_status != "running"
                or claimed_run.current_phase != "claimed"
            ):
                break
            run = claimed_run
            conversation = await session.get(Conversation, run.conversation_id)
            conversation_user = (
                await session.get(User, conversation.user_id) if conversation is not None else None
            )
            live_stream = (
                AgentLiveStreamPublisher(
                    redis,
                    settings,
                    conversation,
                    conversation_user,
                    run.run_no,
                )
                if conversation is not None and conversation_user is not None
                else None
            )
            agent_code = await session.scalar(
                select(AgentDefinition.agent_code)
                .join(AgentVersion, AgentVersion.agent_id == AgentDefinition.id)
                .where(AgentVersion.id == run.agent_version_id)
            )
            agent_code = str(agent_code or "unknown")
            started = time.perf_counter()
            try:
                async for checkpoint_session in postgres_session():
                    checkpoint_store = AgentCheckpointStore(checkpoint_session)
                    with traced_operation(
                        "agent.run",
                        {"ecom.agent.code": agent_code, "ecom.agent.run_id": run.run_no},
                    ):
                        if conversation is None:
                            run.run_status = "failed"
                            run.current_phase = "failed"
                            run.error_code = "AGENT_CONVERSATION_NOT_FOUND"
                            run.version += 1
                        elif agent_code == "exclusive_support":
                            await process_exclusive_run(
                                session,
                                run,
                                settings=settings,
                                security=security,
                                checkpoint_store=checkpoint_store,
                                model_gateway=exclusive_model_gateway,
                                stream_callback=live_stream.publish if live_stream else None,
                            )
                        elif agent_code == "store_support":
                            await process_store_run(
                                session,
                                run,
                                checkpoint_store=checkpoint_store,
                                model_gateway=store_model_gateway,
                                security=security,
                                stream_callback=live_stream.publish if live_stream else None,
                            )
                        elif agent_code in {"merchant_copilot", "admin_copilot"}:
                            await process_operations_run(
                                session,
                                run,
                                checkpoint_store=checkpoint_store,
                                model_gateway=operations_model_gateway,
                                security=security,
                                stream_callback=live_stream.publish if live_stream else None,
                            )
                        else:
                            run.run_status = "failed"
                            run.current_phase = "failed"
                            run.error_code = "AGENT_HANDLER_UNAVAILABLE"
                            run.version += 1
                    break
                await session.commit()
            except Exception:
                await session.rollback()
                failed = await session.scalar(select(AgentRun).where(AgentRun.run_no == run_no))
                if failed is not None:
                    failed.run_status = "failed"
                    failed.current_phase = "failed"
                    failed.error_code = "AGENT_RUNTIME_UNHANDLED_ERROR"
                    failed.version += 1
                    failed_conversation = await session.get(
                        Conversation, failed.conversation_id
                    )
                    if failed_conversation is not None and failed.response_message_id is None:
                        await _persist_failure_response(
                            session,
                            failed,
                            failed_conversation,
                            agent_code=agent_code,
                        )
                    await session.commit()
                logger.exception("agent_run_unhandled_error", run_no=run_no, agent_code=agent_code)
                run = failed or run
            metrics.observe_ai(
                AiMetric(
                    component="agent",
                    operation=agent_code,
                    outcome=_metric_outcome(run.run_status),
                    duration_seconds=time.perf_counter() - started,
                )
            )
            processed += 1
            break
    return processed


async def _persist_failure_response(
    session: object,
    run: AgentRun,
    conversation: Conversation,
    *,
    agent_code: str,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    now = utc_now()
    text = (
        "本次智能处理发生异常，系统已记录故障且没有执行任何写操作。"
        "你可以稍后重试。若问题持续，请联系人工客服。"
    )
    conversation.last_sequence_no += 1
    conversation.last_message_at = now
    conversation.version += 1
    message = Message(
        message_no=new_prefixed_ulid("msg_"),
        conversation_id=conversation.id,
        sequence_no=conversation.last_sequence_no,
        client_message_no=None,
        sender_type="agent",
        sender_id=None,
        message_type="text",
        text_content=text,
        content_payload={
            "run_id": run.run_no,
            "execution_trace": {
                "version": "public-agent-trace-v2",
                "run_id": run.run_no,
                "agent": agent_code,
                "status": "failed",
                "question": "本次会话消息",
                "intent": "runtime_failure",
                "analysis_summary": "处理流程在完成前发生异常，系统已停止后续步骤。",
                "analysis_details": [
                    "异常已记录并关联本次运行。",
                    "未继续调用业务工具，也没有执行写操作。",
                ],
                "steps": [
                    {
                        "kind": "security",
                        "label": "安全停止异常流程",
                        "status": "failed",
                    }
                ],
                "raw_reasoning_exposed": False,
            },
        },
        agent_version_id=run.agent_version_id,
        ai_run_no=run.run_no,
        message_status="sent",
        moderation_status="passed",
        sent_at=now,
    )
    session.add(message)
    await session.flush()
    conversation.last_message_id = message.id
    run.response_message_id = message.id
    run.public_output = text
    common = {"conversation_id": conversation.conversation_no, "run_id": run.run_no}
    session.add_all(
        [
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="agent.response.completed.v1",
                aggregate_type="conversation",
                aggregate_no=conversation.conversation_no,
                aggregate_version=conversation.version,
                payload={**common, "message_id": message.message_no},
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=run.trace_id,
            ),
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="message.sent.v1",
                aggregate_type="conversation",
                aggregate_no=conversation.conversation_no,
                aggregate_version=conversation.version,
                payload={
                    "conversation_id": conversation.conversation_no,
                    "message_id": message.message_no,
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=run.trace_id,
            ),
        ]
    )


def _reject_event(event: OutboxEvent, code: str, now: datetime) -> None:
    event.event_status = "failed"
    event.last_error_code = code
    event.attempt_count += 1
    event.published_at = now


def _metric_outcome(status: str) -> str:
    return {
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
        "waiting_confirmation": "user_input_required",
        "waiting_human": "user_input_required",
    }.get(status, "partial")


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_telemetry(settings)
    initialize_mysql(settings.mysql_dsn)
    initialize_postgres(settings.postgres_dsn)
    initialize_redis(settings.redis_url)
    stopping = asyncio.Event()
    start_worker_heartbeat("agent-runtime-worker", settings, stopping)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("agent_runtime_worker_started")
    next_provider_probe = 0.0
    try:
        while not stopping.is_set():
            if time.monotonic() >= next_provider_probe:
                provider_health = await probe_model_provider(settings, get_redis(), force=True)
                logger.info(
                    "agent_model_provider_probed",
                    status=provider_health.status,
                    error_code=provider_health.error_code,
                    latency_ms=provider_health.latency_ms,
                )
                next_provider_probe = (
                    time.monotonic() + settings.agent_provider_health_interval_seconds
                )
            dispatched = await dispatch_response_requests()
            processed = await process_batch()
            if dispatched or processed:
                continue
            try:
                await asyncio.wait_for(stopping.wait(), timeout=1)
            except TimeoutError:
                pass
    finally:
        await close_redis()
        await close_postgres()
        await close_mysql()
        shutdown_telemetry()
        logger.info("agent_runtime_worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
