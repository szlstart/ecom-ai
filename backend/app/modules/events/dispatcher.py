from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable
from datetime import timedelta
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.modules.system.models import DeadLetterEvent, OutboxEvent

DIRECT_EVENT_TYPES = frozenset(
    {
        "message.response.requested.v1",
        "agent.response.started.v1",
        "agent.response.delta.v1",
        "agent.response.completed.v1",
        "message.sent.v1",
        "message.read_cursor.updated.v1",
        "support.ticket.status_changed.v1",
        "rbac.admin_approval_ready.v1",
    }
)

_SENSITIVE_KEY = re.compile(
    r"(?:password|secret|token|authorization|ciphertext|phone|email|address|tracking)", re.I
)

_PUBLISH_SCRIPT = """
if redis.call('SETNX', KEYS[1], '1') == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  return redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[2], '*',
    'event_no', ARGV[3], 'event_type', ARGV[4], 'aggregate_type', ARGV[5],
    'aggregate_no', ARGV[6], 'aggregate_version', ARGV[7], 'trace_id', ARGV[8],
    'occurred_at', ARGV[9], 'payload', ARGV[10])
end
return 'duplicate'
"""


class DomainEventDispatcher:
    """Reliably projects business facts from MySQL Outbox into Redis Streams.

    Business transactions only write MySQL. The dispatcher uses a Redis deduplication
    marker and XADD in one Lua script, so a crash between Redis and MySQL commits is safe
    to retry. Dedicated command/realtime events stay owned by their existing workers.
    """

    def __init__(self, session: AsyncSession, redis: Redis, environment: str) -> None:
        self.session = session
        self.redis = redis
        self.stream = f"ecom:{environment}:stream:domain-events:v1"
        self.group = "domain-event-observers-v1"
        self.consumer = "outbox-relay"

    async def process_batch(self, limit: int) -> int:
        processed = 0
        for _ in range(limit):
            event = await self._reserve_one()
            if event is None:
                break
            try:
                await self._publish(event)
            except (TypeError, ValueError) as exc:
                await self._fail(event.event_no, "OUTBOX_EVENT_INVALID", str(exc))
                continue
            except Exception:
                await self._retry(event.event_no, "OUTBOX_STREAM_UNAVAILABLE")
                raise
            await self._published(event.event_no)
            processed += 1
        if processed:
            await self.consume_observer_batch(limit)
        return processed

    async def reconcile_failed(self, limit: int = 50) -> int:
        """Backfill failed legacy/direct-worker events into the operator DLQ."""
        events = list(
            (
                await self.session.scalars(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.event_status == "failed",
                        ~exists().where(
                            DeadLetterEvent.source_type == "outbox",
                            DeadLetterEvent.source_no == OutboxEvent.event_no,
                            DeadLetterEvent.dead_status.in_(("open", "replaying")),
                        ),
                    )
                    .order_by(OutboxEvent.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        for event in events:
            await self._dead_letter(
                event,
                event.last_error_code or "OUTBOX_DELIVERY_FAILED",
                "Outbox event exhausted delivery attempts",
            )
        if events:
            await self.session.commit()
        return len(events)

    async def _reserve_one(self) -> OutboxEvent | None:
        now = utc_now()
        event = await self.session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type.not_in(DIRECT_EVENT_TYPES),
                OutboxEvent.event_status == "pending",
                OutboxEvent.available_at <= now,
            )
            .order_by(OutboxEvent.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if event is None:
            return None
        event.attempt_count += 1
        event.available_at = now + timedelta(seconds=30)
        await self.session.commit()
        return event

    async def _publish(self, event: OutboxEvent) -> None:
        payload = json.dumps(
            _redact(event.payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(payload.encode()) > 64 * 1024:
            raise ValueError("event payload exceeds stream projection limit")
        occurred_at = (event.created_at or utc_now()).isoformat() + "Z"
        await cast(
            Awaitable[Any],
            self.redis.eval(
                _PUBLISH_SCRIPT,
                2,
                f"ecom:outbox:published:{event.event_no}",
                self.stream,
                str(7 * 24 * 60 * 60),
                "100000",
                event.event_no,
                event.event_type,
                event.aggregate_type,
                event.aggregate_no,
                str(event.aggregate_version),
                event.trace_id,
                occurred_at,
                payload,
            ),
        )

    async def consume_observer_batch(self, limit: int) -> int:
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        records = await self.redis.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: ">"},
            count=limit,
            block=1,
        )
        acknowledged = 0
        for _stream, messages in records:
            for message_id, fields in messages:
                if not isinstance(fields, dict) or not fields.get("event_no"):
                    continue
                await self.redis.xack(self.stream, self.group, message_id)
                acknowledged += 1
        if acknowledged:
            await self.redis.set(
                f"{self.stream}:consumer:{self.consumer}:last_ack_at",
                utc_now().isoformat() + "Z",
                ex=300,
            )
        return acknowledged

    async def _published(self, event_no: str) -> None:
        event = await self.session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_no == event_no).with_for_update()
        )
        if event is None or event.event_status != "pending":
            return
        event.event_status = "published"
        event.published_at = utc_now()
        event.last_error_code = None
        await self.session.commit()

    async def _retry(self, event_no: str, error_code: str) -> None:
        await self.session.rollback()
        event = await self.session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_no == event_no).with_for_update()
        )
        if event is None or event.event_status != "pending":
            return
        if event.attempt_count >= 8:
            await self._dead_letter(event, error_code, "Redis Streams dispatch failed")
        else:
            event.last_error_code = error_code
            event.available_at = utc_now() + timedelta(
                seconds=min(300, 2 ** min(event.attempt_count, 8))
            )
        await self.session.commit()

    async def _fail(self, event_no: str, error_code: str, detail: str) -> None:
        await self.session.rollback()
        event = await self.session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_no == event_no).with_for_update()
        )
        if event is None or event.event_status != "pending":
            return
        await self._dead_letter(event, error_code, detail)
        await self.session.commit()

    async def _dead_letter(
        self, event: OutboxEvent, error_code: str, detail: str
    ) -> None:
        now = utc_now()
        existing = await self.session.scalar(
            select(DeadLetterEvent).where(
                DeadLetterEvent.source_type == "outbox",
                DeadLetterEvent.source_no == event.event_no,
                DeadLetterEvent.dead_status.in_(("open", "replaying")),
            )
        )
        payload = _redact(event.payload)
        payload_bytes = json.dumps(
            event.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        if existing is None:
            self.session.add(
                DeadLetterEvent(
                    dead_letter_no=new_prefixed_ulid("dlq_"),
                    source_type="outbox",
                    source_no=event.event_no,
                    event_type=event.event_type,
                    schema_version=1,
                    scope_type="platform",
                    scope_id=0,
                    payload_redacted=payload,
                    payload_hash=hashlib.sha256(payload_bytes).digest(),
                    failure_count=max(1, event.attempt_count),
                    first_failed_at=now,
                    last_failed_at=now,
                    last_error_code=error_code,
                    last_error=detail[:1000],
                    dead_status="open",
                    original_trace_id=event.trace_id,
                )
            )
        else:
            existing.failure_count += 1
            existing.last_failed_at = now
            existing.last_error_code = error_code
            existing.last_error = detail[:1000]
            existing.version += 1
        event.event_status = "failed"
        event.last_error_code = error_code
        event.published_at = now


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "***" if _SENSITIVE_KEY.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return value[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2000]
