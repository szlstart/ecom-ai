from __future__ import annotations

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.modules.agent_runtime.models import AgentRun
from app.modules.identity.models import User
from app.modules.messaging.models import (
    Conversation,
    ConversationContext,
    ConversationStatusLog,
    HumanServiceAssignment,
    HumanServiceTicketEvent,
    Message,
)
from app.modules.messaging.repository import MessagingRepository
from app.modules.messaging.schemas import ConversationDeletionView
from app.modules.rbac.dependencies import AdminAccess


class ConversationDeletionService:
    """Soft-delete a chat and irreversibly remove its AI context and memories."""

    def __init__(self, mysql: AsyncSession, postgres: AsyncSession) -> None:
        self.mysql = mysql
        self.postgres = postgres
        self.repository = MessagingRepository(mysql)

    async def delete_owned(self, user: User, conversation_no: str) -> ConversationDeletionView:
        conversation = await self.repository.by_no(user.id, conversation_no, for_update=True)
        if conversation is None:
            raise _not_found()
        return await self._delete(conversation, actor_type="user", actor_id=user.id)

    async def delete_scoped(
        self, access: AdminAccess, conversation_no: str
    ) -> ConversationDeletionView:
        row = await self.repository.conversation_for_operator(conversation_no, for_update=True)
        if row is None:
            raise _not_found()
        conversation, _ = row
        allowed = ("platform", 0) in access.scopes or (
            conversation.store_id is not None and ("store", conversation.store_id) in access.scopes
        )
        if not allowed:
            raise _not_found()
        return await self._delete(conversation, actor_type="admin", actor_id=access.context.user.id)

    async def _delete(
        self, conversation: Conversation, *, actor_type: str, actor_id: int
    ) -> ConversationDeletionView:
        now = utc_now().replace(microsecond=0)
        message_nos = list(
            (
                await self.mysql.scalars(
                    select(Message.message_no).where(Message.conversation_id == conversation.id)
                )
            ).all()
        )
        run_nos = list(
            (
                await self.mysql.scalars(
                    select(AgentRun.run_no).where(AgentRun.conversation_id == conversation.id)
                )
            ).all()
        )
        await self._clear_postgres(conversation.conversation_no, message_nos, run_nos)

        ticket = await self.repository.active_ticket(conversation.id)
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        if ticket is not None:
            previous_ticket_status = ticket.ticket_status
            ticket.ticket_status = "closed"
            ticket.active_key = None
            ticket.closed_at = now
            ticket.resolution_code = "CONVERSATION_DELETED"
            ticket.resolution_summary = "会话已删除"
            ticket.version += 1
            await self.mysql.execute(
                update(HumanServiceAssignment)
                .where(
                    HumanServiceAssignment.ticket_id == ticket.id,
                    HumanServiceAssignment.assignment_status.in_(("assigned", "accepted")),
                )
                .values(assignment_status="ended", ended_at=now, end_reason="CONVERSATION_DELETED")
            )
            self.mysql.add(
                HumanServiceTicketEvent(
                    event_no=new_prefixed_ulid("hte_"),
                    ticket_id=ticket.id,
                    event_type="closed",
                    from_status=previous_ticket_status,
                    to_status="closed",
                    actor_type=actor_type,
                    actor_user_id=actor_id,
                    reason_code="CONVERSATION_DELETED",
                    reason=None,
                    sla_due_at_before=ticket.sla_due_at,
                    sla_due_at_after=None,
                    ticket_version=ticket.version,
                    request_id=request_id,
                    trace_id=request_id,
                )
            )

        previous_status = conversation.conversation_status
        conversation.deleted_at = now
        conversation.user_hidden_at = now
        conversation.conversation_status = "closed"
        conversation.human_ticket_id = None
        conversation.version += 1
        await self.mysql.execute(
            update(ConversationContext)
            .where(
                ConversationContext.conversation_id == conversation.id,
                ConversationContext.context_status == "active",
            )
            .values(context_status="inactive", active_context_key=None)
        )
        self.mysql.add(
            ConversationStatusLog(
                conversation_id=conversation.id,
                from_status=previous_status,
                to_status="closed",
                event_type="deleted",
                actor_type=actor_type,
                actor_id=actor_id,
                ticket_id=ticket.id if ticket else None,
                reason="CONVERSATION_DELETED_AND_AI_MEMORY_CLEARED",
                conversation_version=conversation.version,
                trace_id=request_id,
            )
        )
        await self.mysql.commit()
        return ConversationDeletionView(
            conversation_id=conversation.conversation_no, deleted_at=now, memory_cleared=True
        )

    async def _clear_postgres(
        self, conversation_no: str, message_nos: list[str], run_nos: list[str]
    ) -> None:
        if message_nos:
            await self.postgres.execute(
                text(
                    "DELETE FROM memory.events "
                    "WHERE source_message_no=ANY(:message_nos) OR memory_id IN "
                    "(SELECT id FROM memory.items "
                    "WHERE source_message_no=ANY(:message_nos))"
                ),
                {"message_nos": message_nos},
            )
            await self.postgres.execute(
                text(
                    "DELETE FROM memory.item_embeddings WHERE memory_id IN "
                    "(SELECT id FROM memory.items "
                    "WHERE source_message_no=ANY(:message_nos))"
                ),
                {"message_nos": message_nos},
            )
            await self.postgres.execute(
                text("DELETE FROM memory.items WHERE source_message_no=ANY(:message_nos)"),
                {"message_nos": message_nos},
            )
        await self.postgres.execute(
            text("DELETE FROM memory.summaries WHERE conversation_no=:conversation_no"),
            {"conversation_no": conversation_no},
        )
        if run_nos:
            await self.postgres.execute(
                text("DELETE FROM agent_runtime.checkpoint_writes WHERE run_no=ANY(:run_nos)"),
                {"run_nos": run_nos},
            )
            await self.postgres.execute(
                text("DELETE FROM agent_runtime.checkpoints WHERE run_no=ANY(:run_nos)"),
                {"run_nos": run_nos},
            )
            await self.postgres.execute(
                text("DELETE FROM agent_runtime.run_state_refs WHERE run_no=ANY(:run_nos)"),
                {"run_nos": run_nos},
            )
        await self.postgres.commit()


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="CONVERSATION_NOT_FOUND",
        title="Conversation not found",
        detail="会话不存在或已经删除。",
    )
