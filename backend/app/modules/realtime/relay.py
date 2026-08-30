from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import utc_now
from app.modules.identity.models import User
from app.modules.messaging.models import (
    Conversation,
    HumanServiceTicket,
    HumanServiceTicketEvent,
    Message,
    MessageRead,
)
from app.modules.realtime.channels import (
    admin_platform_channel,
    admin_store_channel,
    admin_user_channel,
    user_channel,
)
from app.modules.system.models import OutboxEvent

REALTIME_EVENT_TYPES = (
    "agent.response.started.v1",
    "agent.response.delta.v1",
    "agent.response.completed.v1",
    "message.sent.v1",
    "message.read_cursor.updated.v1",
    "support.ticket.status_changed.v1",
)


class RealtimeOutboxRelay:
    def __init__(self, session: AsyncSession, redis: Redis, settings: Settings) -> None:
        self.session = session
        self.redis = redis
        self.settings = settings

    async def process_batch(self, limit: int) -> int:
        processed = 0
        for _ in range(limit):
            event_no = await self._reserve_one()
            if event_no is None:
                break
            try:
                await self._dispatch(event_no)
            except RedisError:
                await self._retry(event_no, "REALTIME_REDIS_UNAVAILABLE")
                raise
            except Exception:
                await self._retry(event_no, "REALTIME_EVENT_INVALID")
                continue
            await self._published(event_no)
            processed += 1
        return processed

    async def _reserve_one(self) -> str | None:
        now = utc_now()
        event = await self.session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type.in_(REALTIME_EVENT_TYPES),
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
        event_no = event.event_no
        await self.session.commit()
        return event_no

    async def _dispatch(self, event_no: str) -> None:
        event = await self.session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_no == event_no)
        )
        if event is None or event.event_status != "pending":
            return
        dispatches: list[tuple[str, dict[str, object]]]
        if event.event_type.startswith("agent.response."):
            dispatches = await self._agent_response_dispatches(event)
        elif event.event_type == "message.sent.v1":
            dispatches = await self._message_dispatches(event)
        elif event.event_type == "message.read_cursor.updated.v1":
            dispatches = await self._read_dispatches(event)
        else:
            dispatches = await self._support_dispatches(event)
        if not dispatches:
            return
        pipeline = self.redis.pipeline(transaction=False)
        for channel, frame in dispatches:
            pipeline.publish(channel, json.dumps(frame, separators=(",", ":"), ensure_ascii=False))
        await pipeline.execute()

    async def _agent_response_dispatches(
        self, event: OutboxEvent
    ) -> list[tuple[str, dict[str, object]]]:
        row = (
            await self.session.execute(
                select(Conversation, User)
                .join(User, User.id == Conversation.user_id)
                .where(Conversation.conversation_no == event.aggregate_no)
            )
        ).one_or_none()
        if row is None or event.payload.get("conversation_id") != event.aggregate_no:
            raise ValueError("agent response event aggregate is missing or mismatched")
        conversation, user = row
        run_id = event.payload.get("run_id")
        if not isinstance(run_id, str) or not run_id.startswith("run_"):
            raise ValueError("agent response event has no valid run_id")
        suffix = event.event_type.removeprefix("agent.response.").removesuffix(".v1")
        data: dict[str, object] = {
            "conversation_id": conversation.conversation_no,
            "run_id": run_id,
        }
        if suffix == "started":
            question = event.payload.get("question")
            label = event.payload.get("label")
            summary = event.payload.get("summary")
            stage = event.payload.get("stage")
            if not all(isinstance(item, str) for item in (question, label, summary, stage)):
                raise ValueError("agent started event is invalid")
            if len(str(question)) > 360 or len(str(label)) > 80 or len(str(summary)) > 500:
                raise ValueError("agent started event exceeds public limits")
            data.update(
                {
                    "question": str(question),
                    "label": str(label),
                    "summary": str(summary),
                    "stage": str(stage),
                }
            )
        elif suffix == "delta":
            chunk_index = event.payload.get("chunk_index")
            text_so_far = event.payload.get("text_so_far")
            if (
                not isinstance(chunk_index, int)
                or isinstance(chunk_index, bool)
                or chunk_index < 1
                or not isinstance(text_so_far, str)
                or len(text_so_far) > 4000
            ):
                raise ValueError("agent delta event is invalid")
            data.update({"chunk_index": chunk_index, "text_so_far": text_so_far})
        elif suffix == "completed":
            message_id = event.payload.get("message_id")
            if not isinstance(message_id, str) or not message_id.startswith("msg_"):
                raise ValueError("agent completed event has no valid message_id")
            data["message_id"] = message_id
        frame = _frame(event, f"agent.response.{suffix}", data)
        dispatches = [(user_channel(self.settings.environment, user.user_no), frame)]
        if conversation.store_id is not None:
            dispatches.append(
                (admin_store_channel(self.settings.environment, conversation.store_id), frame)
            )
        elif conversation.conversation_type == "exclusive":
            dispatches.append((admin_platform_channel(self.settings.environment), frame))
        return dispatches

    async def _message_dispatches(self, event: OutboxEvent) -> list[tuple[str, dict[str, object]]]:
        message_no = event.payload.get("message_id")
        if not isinstance(message_no, str):
            raise ValueError("message event has no message_id")
        row = (
            await self.session.execute(
                select(Message, Conversation, User)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .join(User, User.id == Conversation.user_id)
                .where(Message.message_no == message_no)
            )
        ).one_or_none()
        if row is None:
            raise ValueError("message event aggregate is missing")
        message, conversation, user = row
        if message.message_status == "hidden":
            return []
        message_data = {
            "message_id": message.message_no,
            "sequence_no": message.sequence_no,
            "sender_type": message.sender_type,
            "message_type": message.message_type,
            "text": message.text_content,
            "message_status": message.message_status,
            "moderation_status": message.moderation_status,
            "content": message.content_payload,
            "sent_at": message.sent_at.isoformat() + "Z",
        }
        frame = _frame(
            event,
            "message.created",
            {
                "conversation_id": conversation.conversation_no,
                "conversation_version": conversation.version,
                "message": message_data,
            },
        )
        result = [(user_channel(self.settings.environment, user.user_no), frame)]
        if conversation.store_id is not None:
            result.append(
                (admin_store_channel(self.settings.environment, conversation.store_id), frame)
            )
        elif conversation.conversation_type == "exclusive":
            # Platform operators receive the user/merchant exclusive-support inbox in
            # real time even before an Agent decides that human takeover is required.
            result.append((admin_platform_channel(self.settings.environment), frame))
        unread = await self._user_unread(user.id, conversation.id)
        result.append(
            (
                user_channel(self.settings.environment, user.user_no),
                _frame(
                    event,
                    "unread.updated",
                    {
                        "conversation_id": conversation.conversation_no,
                        **unread,
                    },
                ),
            )
        )
        if conversation.human_ticket_id is not None:
            ticket = await self.session.get(HumanServiceTicket, conversation.human_ticket_id)
            if ticket is not None and ticket.current_assignee_user_id is not None:
                assignee = await self.session.get(User, ticket.current_assignee_user_id)
                if assignee is not None:
                    result.append(
                        (admin_user_channel(self.settings.environment, assignee.user_no), frame)
                    )
        return result

    async def _read_dispatches(self, event: OutboxEvent) -> list[tuple[str, dict[str, object]]]:
        conversation = await self.session.scalar(
            select(Conversation).where(Conversation.conversation_no == event.aggregate_no)
        )
        if conversation is None:
            raise ValueError("read event aggregate is missing")
        user = await self.session.get(User, conversation.user_id)
        if user is None:
            raise ValueError("read event user is missing")
        unread = await self._user_unread(user.id, conversation.id)
        return [
            (
                user_channel(self.settings.environment, user.user_no),
                _frame(
                    event,
                    "unread.updated",
                    {
                        "conversation_id": conversation.conversation_no,
                        "last_read_message_id": event.payload.get("last_read_message_id"),
                        "last_read_sequence_no": event.payload.get("last_read_sequence_no"),
                        "cursor_version": event.payload.get("cursor_version"),
                        **unread,
                    },
                ),
            )
        ]

    async def _support_dispatches(self, event: OutboxEvent) -> list[tuple[str, dict[str, object]]]:
        row = (
            await self.session.execute(
                select(HumanServiceTicket, Conversation, User)
                .join(Conversation, Conversation.id == HumanServiceTicket.conversation_id)
                .join(User, User.id == Conversation.user_id)
                .where(HumanServiceTicket.ticket_no == event.aggregate_no)
            )
        ).one_or_none()
        if row is None:
            raise ValueError("support event aggregate is missing")
        ticket, conversation, user = row
        ticket_event_no = event.payload.get("ticket_event_id")
        ticket_event = (
            await self.session.scalar(
                select(HumanServiceTicketEvent).where(
                    HumanServiceTicketEvent.event_no == ticket_event_no
                )
            )
            if isinstance(ticket_event_no, str)
            else None
        )
        user_frame = _frame(
            event,
            "support.status.updated",
            {
                "conversation_id": conversation.conversation_no,
                "ticket_id": ticket.ticket_no,
                "ticket_status": ticket.ticket_status,
                "resolution_summary": ticket.resolution_summary,
                "version": ticket.version,
            },
        )
        assigned_user_no = None
        if ticket.current_assignee_user_id is not None:
            assignee = await self.session.get(User, ticket.current_assignee_user_id)
            assigned_user_no = assignee.user_no if assignee is not None else None
        admin_frame = _frame(
            event,
            "support.ticket.updated",
            {
                "ticket_id": ticket.ticket_no,
                "conversation_id": conversation.conversation_no,
                "queue_type": ticket.queue_type,
                "queue_code": ticket.queue_code,
                "ticket_status": ticket.ticket_status,
                "assigned_user_id": assigned_user_no,
                "version": ticket.version,
                "event_type": ticket_event.event_type if ticket_event is not None else None,
            },
        )
        dispatches = [
            (user_channel(self.settings.environment, user.user_no), user_frame),
            (admin_platform_channel(self.settings.environment), admin_frame),
        ]
        if conversation.store_id is not None:
            dispatches.append(
                (admin_store_channel(self.settings.environment, conversation.store_id), admin_frame)
            )
        if assigned_user_no is not None:
            dispatches.append(
                (admin_user_channel(self.settings.environment, assigned_user_no), admin_frame)
            )
        return dispatches

    async def _user_unread(self, user_id: int, conversation_id: int) -> dict[str, int]:
        conversation_count = int(
            await self.session.scalar(
                select(func.count(Message.id))
                .outerjoin(
                    MessageRead,
                    and_(
                        MessageRead.conversation_id == Message.conversation_id,
                        MessageRead.reader_type == "user",
                        MessageRead.reader_id == user_id,
                    ),
                )
                .where(
                    Message.conversation_id == conversation_id,
                    Message.message_status != "hidden",
                    Message.sender_type != "user",
                    Message.sequence_no > func.coalesce(MessageRead.last_read_sequence_no, 0),
                )
            )
            or 0
        )
        total_count = int(
            await self.session.scalar(
                select(func.count(Message.id))
                .join(Conversation, Conversation.id == Message.conversation_id)
                .outerjoin(
                    MessageRead,
                    and_(
                        MessageRead.conversation_id == Message.conversation_id,
                        MessageRead.reader_type == "user",
                        MessageRead.reader_id == user_id,
                    ),
                )
                .where(
                    Conversation.user_id == user_id,
                    Conversation.deleted_at.is_(None),
                    Message.message_status != "hidden",
                    Message.sender_type != "user",
                    Message.sequence_no > func.coalesce(MessageRead.last_read_sequence_no, 0),
                )
            )
            or 0
        )
        return {"conversation_unread": conversation_count, "total_unread": total_count}

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
            # The generic Outbox dispatcher reconciles failed rows into the
            # operator-visible dead-letter queue.  Do not retry a malformed
            # realtime event forever and starve newer messages.
            event.event_status = "failed"
            event.published_at = utc_now()
            event.last_error_code = error_code
            await self.session.commit()
            return
        delay = min(300, 2 ** min(event.attempt_count, 8))
        event.available_at = utc_now() + timedelta(seconds=delay)
        event.last_error_code = error_code
        await self.session.commit()


def _frame(event: OutboxEvent, event_type: str, data: dict[str, object]) -> dict[str, object]:
    digest = hashlib.sha256(f"{event.event_no}:{event_type}".encode()).hexdigest()[:26].upper()
    return {
        "schema_version": 1,
        "event_id": f"rte_{digest}",
        "type": event_type,
        "occurred_at": (event.created_at or utc_now()).isoformat() + "Z",
        "data": data,
    }
