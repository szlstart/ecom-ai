from __future__ import annotations

from typing import cast

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.messaging.models import (
    Conversation,
    ConversationContext,
    HumanServiceInternalNote,
    HumanServiceTicket,
    HumanServiceTicketEvent,
    Message,
    MessageRead,
)
from app.modules.stores.models import Store


class MessagingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def conversations(self, user_id: int) -> list[tuple[Conversation, Store | None]]:
        rows = (
            await self.session.execute(
                select(Conversation, Store)
                .outerjoin(Store, Store.id == Conversation.store_id)
                .where(
                    Conversation.user_id == user_id,
                    Conversation.deleted_at.is_(None),
                    Conversation.user_hidden_at.is_(None),
                )
                .order_by(
                    Conversation.is_fixed.desc(),
                    Conversation.last_message_at.desc(),
                    Conversation.id.desc(),
                )
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def by_no(
        self, user_id: int, conversation_no: str, *, for_update: bool = False
    ) -> Conversation | None:
        statement = select(Conversation).where(
            Conversation.user_id == user_id,
            Conversation.conversation_no == conversation_no,
            Conversation.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(Conversation | None, await self.session.scalar(statement))

    async def exclusive(self, user_id: int) -> Conversation | None:
        return cast(
            Conversation | None,
            await self.session.scalar(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.conversation_type == "exclusive",
                    Conversation.deleted_at.is_(None),
                )
            ),
        )

    async def store_conversation(
        self, user_id: int, store_id: int, *, for_update: bool = False
    ) -> Conversation | None:
        statement = select(Conversation).where(
            Conversation.user_id == user_id,
            Conversation.store_id == store_id,
            Conversation.conversation_type == "store",
            Conversation.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(
            Conversation | None,
            await self.session.scalar(statement),
        )

    async def messages(self, conversation_id: int, limit: int) -> list[Message]:
        rows = list(
            (
                await self.session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.message_status != "hidden",
                    )
                    .order_by(Message.sequence_no.desc())
                    .limit(limit)
                )
            ).all()
        )
        rows.reverse()
        return rows

    async def messages_after(
        self, conversation_id: int, after_sequence: int, limit: int
    ) -> list[Message]:
        return list(
            (
                await self.session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.sequence_no > after_sequence,
                        Message.message_status != "hidden",
                    )
                    .order_by(Message.sequence_no)
                    .limit(limit)
                )
            ).all()
        )

    async def messages_before(
        self, conversation_id: int, before_sequence: int, limit: int
    ) -> list[Message]:
        rows = list(
            (
                await self.session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.sequence_no < before_sequence,
                        Message.message_status != "hidden",
                    )
                    .order_by(Message.sequence_no.desc())
                    .limit(limit)
                )
            ).all()
        )
        rows.reverse()
        return rows

    async def has_message_before(self, conversation_id: int, sequence_no: int) -> bool:
        return bool(
            await self.session.scalar(
                select(Message.id)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.sequence_no < sequence_no,
                    Message.message_status != "hidden",
                )
                .limit(1)
            )
        )

    async def active_context(
        self, conversation_id: int, context_type: str, *, for_update: bool = False
    ) -> ConversationContext | None:
        statement = select(ConversationContext).where(
            ConversationContext.conversation_id == conversation_id,
            ConversationContext.context_type == context_type,
            ConversationContext.context_status == "active",
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(ConversationContext | None, await self.session.scalar(statement))

    async def active_contexts(self, conversation_id: int) -> list[ConversationContext]:
        return list(
            (
                await self.session.scalars(
                    select(ConversationContext)
                    .where(
                        ConversationContext.conversation_id == conversation_id,
                        ConversationContext.context_status == "active",
                    )
                    .order_by(ConversationContext.created_at, ConversationContext.id)
                )
            ).all()
        )

    async def message_by_no(self, conversation_id: int, message_no: str) -> Message | None:
        return cast(
            Message | None,
            await self.session.scalar(
                select(Message).where(
                    Message.conversation_id == conversation_id, Message.message_no == message_no
                )
            ),
        )

    async def messages_by_nos(self, conversation_id: int, message_nos: list[str]) -> list[Message]:
        if not message_nos:
            return []
        return list(
            (
                await self.session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.message_no.in_(message_nos),
                        Message.message_status == "sent",
                    )
                    .order_by(Message.sequence_no)
                )
            ).all()
        )

    async def last_visible_message(self, conversation: Conversation) -> Message | None:
        if conversation.last_message_id is None:
            return None
        return cast(
            Message | None,
            await self.session.scalar(
                select(Message).where(
                    Message.id == conversation.last_message_id,
                    Message.conversation_id == conversation.id,
                    Message.message_status == "sent",
                )
            ),
        )

    async def client_message(self, conversation_id: int, client_no: str) -> Message | None:
        return cast(
            Message | None,
            await self.session.scalar(
                select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.client_message_no == client_no,
                )
            ),
        )

    async def read_cursor(
        self, conversation_id: int, user_id: int, *, for_update: bool = False
    ) -> MessageRead | None:
        statement = select(MessageRead).where(
            MessageRead.conversation_id == conversation_id,
            MessageRead.reader_type == "user",
            MessageRead.reader_id == user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(MessageRead | None, await self.session.scalar(statement))

    async def support_read_cursor(
        self, conversation_id: int, reader_id: int, *, for_update: bool = False
    ) -> MessageRead | None:
        statement = select(MessageRead).where(
            MessageRead.conversation_id == conversation_id,
            MessageRead.reader_type == "human",
            MessageRead.reader_id == reader_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(MessageRead | None, await self.session.scalar(statement))

    async def support_unread_count(self, conversation_id: int, last_read: int) -> int:
        return int(
            await self.session.scalar(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conversation_id,
                    Message.sequence_no > last_read,
                    Message.sender_type != "human",
                    Message.message_status == "sent",
                )
            )
            or 0
        )

    async def support_unread_counts(
        self, conversation_ids: set[int], reader_id: int
    ) -> dict[int, int]:
        if not conversation_ids:
            return {}
        rows = (
            await self.session.execute(
                select(Conversation.id, func.count(Message.id))
                .outerjoin(
                    MessageRead,
                    and_(
                        MessageRead.conversation_id == Conversation.id,
                        MessageRead.reader_type == "human",
                        MessageRead.reader_id == reader_id,
                    ),
                )
                .outerjoin(
                    Message,
                    and_(
                        Message.conversation_id == Conversation.id,
                        Message.sequence_no > func.coalesce(MessageRead.last_read_sequence_no, 0),
                        Message.sender_type != "human",
                        Message.message_status == "sent",
                    ),
                )
                .where(Conversation.id.in_(conversation_ids))
                .group_by(Conversation.id)
            )
        ).all()
        return {conversation_id: int(count) for conversation_id, count in rows}

    async def unread_count(self, conversation_id: int, last_read: int) -> int:
        return int(
            await self.session.scalar(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conversation_id,
                    Message.sequence_no > last_read,
                    Message.sender_type != "user",
                    Message.message_status == "sent",
                )
            )
            or 0
        )

    async def active_ticket(self, conversation_id: int) -> HumanServiceTicket | None:
        return cast(
            HumanServiceTicket | None,
            await self.session.scalar(
                select(HumanServiceTicket).where(
                    HumanServiceTicket.conversation_id == conversation_id,
                    HumanServiceTicket.active_key == 1,
                )
            ),
        )

    async def support_tickets(
        self, *, queue_type: str | None, statuses: tuple[str, ...], limit: int
    ) -> list[tuple[HumanServiceTicket, Conversation]]:
        statement = (
            select(HumanServiceTicket, Conversation)
            .join(Conversation, Conversation.id == HumanServiceTicket.conversation_id)
            .where(HumanServiceTicket.ticket_status.in_(statuses))
        )
        if queue_type is not None:
            statement = statement.where(HumanServiceTicket.queue_type == queue_type)
        rows = (
            await self.session.execute(
                statement.order_by(HumanServiceTicket.created_at, HumanServiceTicket.id).limit(
                    limit
                )
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def support_ticket(
        self, ticket_no: str, *, for_update: bool = False
    ) -> tuple[HumanServiceTicket, Conversation] | None:
        statement = (
            select(HumanServiceTicket, Conversation)
            .join(Conversation, Conversation.id == HumanServiceTicket.conversation_id)
            .where(HumanServiceTicket.ticket_no == ticket_no)
        )
        if for_update:
            statement = statement.with_for_update(of=(HumanServiceTicket, Conversation))
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1]) if row is not None else None

    async def support_ticket_for_conversation(
        self, conversation_no: str, *, for_update: bool = False
    ) -> tuple[HumanServiceTicket, Conversation] | None:
        statement = (
            select(HumanServiceTicket, Conversation)
            .join(Conversation, Conversation.id == HumanServiceTicket.conversation_id)
            .where(
                Conversation.conversation_no == conversation_no,
                HumanServiceTicket.active_key == 1,
            )
        )
        if for_update:
            statement = statement.with_for_update(of=(HumanServiceTicket, Conversation))
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1]) if row is not None else None

    async def ticket_events(self, ticket_id: int) -> list[HumanServiceTicketEvent]:
        return list(
            (
                await self.session.scalars(
                    select(HumanServiceTicketEvent)
                    .where(HumanServiceTicketEvent.ticket_id == ticket_id)
                    .order_by(HumanServiceTicketEvent.created_at, HumanServiceTicketEvent.id)
                )
            ).all()
        )

    async def internal_notes(self, ticket_id: int) -> list[HumanServiceInternalNote]:
        return list(
            (
                await self.session.scalars(
                    select(HumanServiceInternalNote)
                    .where(HumanServiceInternalNote.ticket_id == ticket_id)
                    .order_by(HumanServiceInternalNote.created_at, HumanServiceInternalNote.id)
                )
            ).all()
        )
