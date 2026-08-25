from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyClaim, IdempotencyService
from app.core.security import SecurityService, utc_now
from app.modules.identity.models import User
from app.modules.messaging.content_safety import blocks_message
from app.modules.messaging.models import (
    Conversation,
    ConversationContext,
    ConversationStatusLog,
    HumanServiceAssignment,
    HumanServiceInternalNote,
    HumanServiceTicket,
    HumanServiceTicketEvent,
    Message,
    MessageRead,
)
from app.modules.messaging.repository import MessagingRepository
from app.modules.messaging.schemas import (
    ConversationContextView,
    MessageList,
    MessageView,
    ReadCursorRequest,
)
from app.modules.messaging.support_schemas import (
    SupportInternalNoteList,
    SupportInternalNoteRequest,
    SupportInternalNoteView,
    SupportMessageRequest,
    SupportReadCursorView,
    SupportResolveRequest,
    SupportTicketEventView,
    SupportTicketItem,
    SupportTicketList,
    SupportTicketView,
    SupportTransferRequest,
    SupportUserSummary,
    SupportWaitRequest,
    SupportWorkspaceView,
)
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.repository import RbacRepository
from app.modules.system.models import OutboxEvent


class SupportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = MessagingRepository(session)
        self.security = SecurityService(get_settings())
        self.idempotency = IdempotencyService(session)

    async def list(
        self,
        access: AdminAccess,
        queue_type: str | None,
        ticket_status: str | None,
        limit: int,
    ) -> SupportTicketList:
        rows = await self.repository.support_tickets(
            queue_type=queue_type,
            statuses=(ticket_status,)
            if ticket_status is not None
            else ("queued", "assigned", "active", "waiting_user"),
            limit=limit,
        )
        scoped = [row for row in rows if self._scope_allowed(access, row[1])]
        assignee_ids = {
            ticket.current_assignee_user_id
            for ticket, _conversation in scoped
            if ticket.current_assignee_user_id is not None
        }
        assignees = {
            user.id: user
            for user in (
                (await self.session.scalars(select(User).where(User.id.in_(assignee_ids)))).all()
                if assignee_ids
                else []
            )
        }
        return SupportTicketList(
            items=[
                _ticket_item(
                    ticket,
                    conversation,
                    assignees.get(ticket.current_assignee_user_id)
                    if ticket.current_assignee_user_id is not None
                    else None,
                )
                for ticket, conversation in scoped
            ]
        )

    async def get(self, access: AdminAccess, ticket_no: str) -> SupportTicketView:
        ticket, conversation = await self._ticket(access, ticket_no)
        return await self._view(ticket, conversation)

    async def workspace(self, access: AdminAccess, ticket_no: str) -> SupportWorkspaceView:
        ticket, conversation = await self._ticket(access, ticket_no)
        user = await self.session.get(User, ticket.user_id)
        if user is None:
            raise _not_found()
        referenced_nos = [
            str(item["message_id"])
            for item in ticket.handoff_message_refs
            if isinstance(item, dict) and item.get("message_id")
        ]
        referenced_messages = await self.repository.messages_by_nos(conversation.id, referenced_nos)
        contexts = await self.repository.active_contexts(conversation.id)
        events = await self.repository.ticket_events(ticket.id)
        record_admin_operation(
            self.session,
            access,
            action="support.workspace.read",
            target_type="human_service_ticket",
            target_no=ticket.ticket_no,
            scope_type="store" if conversation.store_id is not None else "platform",
            scope_id=conversation.store_id or 0,
        )
        await self.session.commit()
        return SupportWorkspaceView(
            ticket=await self._view(ticket, conversation),
            user=SupportUserSummary(
                user_id=user.user_no,
                nickname=user.nickname,
                account_status=user.user_status,
            ),
            referenced_messages=[
                _support_visible_message_view(item) for item in referenced_messages
            ],
            business_contexts=[_context_view(item) for item in contexts],
            events=[
                SupportTicketEventView(
                    event_id=item.event_no,
                    event_type=item.event_type,
                    from_status=item.from_status,
                    to_status=item.to_status,
                    reason_code=item.reason_code,
                    reason=item.reason,
                    occurred_at=item.created_at,
                )
                for item in events
            ],
        )

    async def conversation_messages(
        self, access: AdminAccess, conversation_no: str, limit: int
    ) -> MessageList:
        _ticket, conversation = await self._assigned_conversation(access, conversation_no)
        return MessageList(
            items=[
                _support_visible_message_view(item)
                for item in await self.repository.messages(conversation.id, limit)
            ]
        )

    async def internal_notes(self, access: AdminAccess, ticket_no: str) -> SupportInternalNoteList:
        ticket, conversation = await self._assigned(access, ticket_no)
        notes = await self.repository.internal_notes(ticket.id)
        author_ids = {item.author_user_id for item in notes}
        authors = {
            user.id: user
            for user in (
                (await self.session.scalars(select(User).where(User.id.in_(author_ids)))).all()
                if author_ids
                else []
            )
        }
        record_admin_operation(
            self.session,
            access,
            action="support.internal_notes.read",
            target_type="human_service_ticket",
            target_no=ticket.ticket_no,
            scope_type="store" if conversation.store_id is not None else "platform",
            scope_id=conversation.store_id or 0,
        )
        await self.session.commit()
        return SupportInternalNoteList(
            items=[
                SupportInternalNoteView(
                    note_id=item.note_no,
                    author_user_id=_author_no(authors, item.author_user_id),
                    note_type=item.note_type,
                    text=self.security.decrypt(
                        f"support-note:{ticket.ticket_no}", item.content_ciphertext
                    ),
                    visibility_scope=item.visibility_scope,
                    created_at=item.created_at,
                )
                for item in notes
            ]
        )

    async def claim(
        self,
        access: AdminAccess,
        ticket_no: str,
        expected_version: int,
        idempotency_key: str,
    ) -> SupportTicketView:
        command = await self._begin_command(access, ticket_no, "claim", {}, idempotency_key)
        ticket, conversation = await self._ticket(access, ticket_no, for_update=True)
        if command.replayed:
            return await self._view(ticket, conversation)
        _require_version(ticket, expected_version)
        if ticket.ticket_status == "queued":
            ticket.ticket_status = "active"
            ticket.current_assignee_user_id = access.context.user.id
            ticket.version += 1
            now = utc_now().replace(microsecond=0)
            self.session.add(
                HumanServiceAssignment(
                    ticket_id=ticket.id,
                    assignee_user_id=access.context.user.id,
                    assignment_type="manual",
                    assigned_by_type="human",
                    assigned_by_id=access.context.user.id,
                    assignment_status="accepted",
                    assigned_at=now,
                    accepted_at=now,
                )
            )
            self._ticket_event(ticket, "claimed", "queued", "active", access)
            self._conversation_status(conversation, "human_active", "assigned", access, ticket)
            self.idempotency.complete(command, response_status=200, resource_no=ticket.ticket_no)
            return await self._commit_view(ticket, conversation)
        if ticket.current_assignee_user_id != access.context.user.id:
            raise _conflict("SUPPORT_TICKET_ALREADY_CLAIMED", "工单已被其他客服领取。")
        if ticket.ticket_status == "assigned":
            assignment = await self.session.scalar(
                select(HumanServiceAssignment)
                .where(
                    HumanServiceAssignment.ticket_id == ticket.id,
                    HumanServiceAssignment.assignee_user_id == access.context.user.id,
                    HumanServiceAssignment.assignment_status == "assigned",
                )
                .with_for_update()
            )
            if assignment is None:
                raise _conflict(
                    "SUPPORT_ASSIGNMENT_NOT_AVAILABLE",
                    "当前转派记录已经变化，请刷新后重试。",
                )
            now = utc_now().replace(microsecond=0)
            assignment.assignment_status = "accepted"
            assignment.accepted_at = now
            assignment.version += 1
            ticket.ticket_status = "active"
            ticket.version += 1
            self._ticket_event(ticket, "accepted", "assigned", "active", access)
            self._conversation_status(conversation, "human_active", "accepted", access, ticket)
            self.idempotency.complete(command, response_status=200, resource_no=ticket.ticket_no)
            return await self._commit_view(ticket, conversation)
        if ticket.ticket_status != "active":
            raise _conflict("SUPPORT_CLAIM_NOT_ALLOWED", "当前工单不能领取或接受。")
        self.idempotency.complete(command, response_status=200, resource_no=ticket.ticket_no)
        return await self._commit_view(ticket, conversation)

    async def wait(
        self,
        access: AdminAccess,
        ticket_no: str,
        payload: SupportWaitRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> SupportTicketView:
        command = await self._begin_command(
            access, ticket_no, "wait", payload.model_dump(mode="json"), idempotency_key
        )
        ticket, conversation = await self._assigned(access, ticket_no)
        if command.replayed:
            return await self._view(ticket, conversation)
        _require_version(ticket, expected_version)
        if ticket.ticket_status != "active":
            raise _conflict("SUPPORT_WAIT_NOT_ALLOWED", "当前工单不能进入等待用户状态。")
        previous_status = ticket.ticket_status
        now = utc_now().replace(microsecond=0)
        sla_before = ticket.sla_due_at
        ticket.ticket_status = "waiting_user"
        ticket.waiting_started_at = now
        ticket.sla_remaining_seconds = max(
            0, int((ticket.sla_due_at - now).total_seconds()) if ticket.sla_due_at else 0
        )
        ticket.sla_due_at = None
        ticket.waiting_reason_code = payload.reason_code
        ticket.version += 1
        self._ticket_event(
            ticket,
            "waiting_user",
            previous_status,
            "waiting_user",
            access,
            reason_code=payload.reason_code,
            reason=payload.reason,
            sla_before=sla_before,
        )
        self._conversation_status(
            conversation, "human_active", "waiting_user", access, ticket, payload.reason
        )
        self.idempotency.complete(command, response_status=200, resource_no=ticket.ticket_no)
        return await self._commit_view(ticket, conversation)

    async def resume(
        self,
        access: AdminAccess,
        ticket_no: str,
        expected_version: int,
        idempotency_key: str,
    ) -> SupportTicketView:
        command = await self._begin_command(access, ticket_no, "resume", {}, idempotency_key)
        ticket, conversation = await self._assigned(access, ticket_no)
        if command.replayed:
            return await self._view(ticket, conversation)
        _require_version(ticket, expected_version)
        if ticket.ticket_status not in {"active", "waiting_user"}:
            raise _conflict("SUPPORT_TRANSFER_NOT_ALLOWED", "当前工单不能转派。")
        if ticket.ticket_status != "waiting_user":
            raise _conflict("SUPPORT_RESUME_NOT_ALLOWED", "当前工单不在等待用户状态。")
        sla_before = ticket.sla_due_at
        ticket.ticket_status = "active"
        ticket.waiting_started_at = None
        ticket.sla_due_at = utc_now() + timedelta(seconds=ticket.sla_remaining_seconds or 0)
        ticket.sla_remaining_seconds = None
        ticket.waiting_reason_code = None
        ticket.version += 1
        self._ticket_event(
            ticket,
            "resumed",
            "waiting_user",
            "active",
            access,
            reason_code="SUPPORT_RESUMED",
            sla_before=sla_before,
        )
        self._conversation_status(conversation, "human_active", "resumed", access, ticket)
        self.idempotency.complete(command, response_status=200, resource_no=ticket.ticket_no)
        return await self._commit_view(ticket, conversation)

    async def transfer(
        self,
        access: AdminAccess,
        ticket_no: str,
        payload: SupportTransferRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> SupportTicketView:
        command = await self._begin_command(
            access, ticket_no, "transfer", payload.model_dump(mode="json"), idempotency_key
        )
        ticket, conversation = await self._assigned(access, ticket_no)
        if command.replayed:
            return await self._view(ticket, conversation)
        _require_version(ticket, expected_version)
        target = await self.session.scalar(
            select(User).where(User.user_no == payload.assigned_user_id)
        )
        if target is None or target.user_status != "active":
            raise _not_found()
        if target.id == access.context.user.id:
            raise _conflict("SUPPORT_TRANSFER_TARGET_UNCHANGED", "目标客服不能是当前客服。")
        target_permissions = await RbacRepository(self.session).permissions_for_user(
            target.id, utc_now()
        )
        target_scope_allowed = any(
            permission.permission_code == "support:claim"
            and (
                grant.scope_type == "platform"
                or (
                    conversation.store_id is not None
                    and grant.scope_type == "store"
                    and grant.scope_id == conversation.store_id
                )
            )
            for permission, grant, _role in target_permissions
        )
        if not target_scope_allowed:
            raise _conflict(
                "SUPPORT_TRANSFER_TARGET_NOT_ELIGIBLE",
                "目标客服没有当前工单范围的领取权限。",
            )
        previous_status = ticket.ticket_status
        current_assignment = await self.session.scalar(
            select(HumanServiceAssignment)
            .where(
                HumanServiceAssignment.ticket_id == ticket.id,
                HumanServiceAssignment.assignment_status.in_(("assigned", "accepted")),
            )
            .with_for_update()
        )
        if current_assignment is not None:
            current_assignment.assignment_status = "released"
            current_assignment.ended_at = utc_now()
            current_assignment.end_reason = "transferred"
            current_assignment.version += 1
        ticket.current_assignee_user_id = target.id
        ticket.ticket_status = "assigned"
        ticket.version += 1
        self.session.add(
            HumanServiceAssignment(
                ticket_id=ticket.id,
                assignee_user_id=target.id,
                assignment_type="transfer",
                assigned_by_type="human",
                assigned_by_id=access.context.user.id,
                assignment_status="assigned",
                assigned_at=utc_now(),
            )
        )
        self._ticket_event(
            ticket,
            "transferred",
            previous_status,
            "assigned",
            access,
            reason_code="TRANSFERRED",
            reason=payload.reason,
        )
        self.idempotency.complete(command, response_status=200, resource_no=ticket.ticket_no)
        return await self._commit_view(ticket, conversation)

    async def resolve(
        self,
        access: AdminAccess,
        ticket_no: str,
        payload: SupportResolveRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> SupportTicketView:
        command = await self._begin_command(
            access, ticket_no, "resolve", payload.model_dump(mode="json"), idempotency_key
        )
        ticket, conversation = await self._assigned(access, ticket_no)
        if command.replayed:
            return await self._view(ticket, conversation)
        _require_version(ticket, expected_version)
        if ticket.ticket_status not in {"active", "waiting_user"}:
            raise _conflict("SUPPORT_RESOLVE_NOT_ALLOWED", "当前工单不能结束服务。")
        previous_status = ticket.ticket_status
        ticket.ticket_status = "resolved"
        ticket.active_key = None
        ticket.resolution_code = payload.resolution_code
        ticket.resolution_summary = payload.summary
        # Internal resolution details are never stored in the ticket projection. They use the
        # separately permissioned, encrypted internal-note resource so user/Agent DTOs cannot
        # expose them accidentally.
        ticket.resolution_note = None
        if payload.internal_note:
            self._add_internal_note(
                access,
                ticket,
                conversation,
                text=payload.internal_note,
                note_type="resolution",
                visibility_scope="current_queue",
            )
        ticket.resolved_at = utc_now()
        ticket.version += 1
        assignment = await self.session.scalar(
            select(HumanServiceAssignment)
            .where(
                HumanServiceAssignment.ticket_id == ticket.id,
                HumanServiceAssignment.assignment_status.in_(("assigned", "accepted")),
            )
            .with_for_update()
        )
        if assignment is not None:
            assignment.assignment_status = "completed"
            assignment.ended_at = utc_now()
            assignment.end_reason = "resolved"
            assignment.version += 1
        self._ticket_event(
            ticket,
            "resolved",
            previous_status,
            "resolved",
            access,
            reason_code=payload.resolution_code,
        )
        self._conversation_status(
            conversation, "active", "resolved", access, ticket, payload.summary
        )
        conversation.human_ticket_id = None
        await self._system_message(conversation, "人工服务已结束。如有新问题，请继续发送消息。")
        self.idempotency.complete(command, response_status=200, resource_no=ticket.ticket_no)
        return await self._commit_view(ticket, conversation)

    async def send(
        self, access: AdminAccess, ticket_no: str, payload: SupportMessageRequest
    ) -> MessageView:
        ticket, conversation = await self._assigned(access, ticket_no)
        if ticket.ticket_status != "active":
            raise _conflict("SUPPORT_REPLY_NOT_ALLOWED", "当前工单不能发送客服消息。")
        existing = await self.repository.client_message(conversation.id, payload.client_message_id)
        if existing is not None:
            return _support_message_view(existing)
        now = utc_now().replace(microsecond=0)
        blocked = blocks_message(payload.text)
        conversation.last_sequence_no += 1
        conversation.version += 1
        message = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=conversation.last_sequence_no,
            client_message_no=payload.client_message_id,
            sender_type="human",
            sender_id=access.context.user.id,
            message_type="text",
            text_content=payload.text if not blocked else None,
            content_payload=None,
            message_status="hidden" if blocked else "sent",
            moderation_status="blocked" if blocked else "passed",
            sent_at=now,
        )
        self.session.add(message)
        await self.session.flush()
        if not blocked:
            if conversation.user_hidden_at is not None:
                conversation.user_hidden_at = None
                self._conversation_status(
                    conversation,
                    conversation.conversation_status,
                    "restored",
                    access,
                    ticket,
                    "SUPPORT_SENT_MESSAGE",
                )
            conversation.last_message_at = now
            conversation.last_message_id = message.id
            self.session.add(
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
                    trace_id=new_prefixed_ulid("req_"),
                )
            )
        await self.session.commit()
        await self.session.refresh(message)
        return _support_message_view(message)

    async def send_conversation(
        self, access: AdminAccess, conversation_no: str, payload: SupportMessageRequest
    ) -> MessageView:
        ticket, _conversation = await self._assigned_conversation(
            access, conversation_no, for_update=True
        )
        return await self.send(access, ticket.ticket_no, payload)

    async def read_conversation(
        self,
        access: AdminAccess,
        conversation_no: str,
        payload: ReadCursorRequest,
    ) -> SupportReadCursorView:
        _ticket, conversation = await self._assigned_conversation(
            access, conversation_no, for_update=True
        )
        message = await self.repository.message_by_no(conversation.id, payload.last_read_message_id)
        if message is None or message.sequence_no != payload.last_read_sequence_no:
            raise _not_found()
        cursor = await self.repository.support_read_cursor(
            conversation.id, access.context.user.id, for_update=True
        )
        now = utc_now().replace(microsecond=0)
        if cursor is None:
            cursor = MessageRead(
                conversation_id=conversation.id,
                reader_type="human",
                reader_id=access.context.user.id,
                last_read_message_id=message.id,
                last_read_sequence_no=message.sequence_no,
                last_read_at=now,
            )
            self.session.add(cursor)
        elif message.sequence_no > cursor.last_read_sequence_no:
            cursor.last_read_message_id = message.id
            cursor.last_read_sequence_no = message.sequence_no
            cursor.last_read_at = now
            cursor.version += 1
        await self.session.commit()
        return SupportReadCursorView(
            conversation_id=conversation.conversation_no,
            last_read_message_id=message.message_no,
            last_read_sequence_no=cursor.last_read_sequence_no,
            unread_count=await self.repository.support_unread_count(
                conversation.id, cursor.last_read_sequence_no
            ),
            cursor_version=cursor.version,
        )

    async def note(
        self,
        access: AdminAccess,
        ticket_no: str,
        payload: SupportInternalNoteRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> SupportTicketView:
        command = await self._begin_command(
            access, ticket_no, "note", payload.model_dump(mode="json"), idempotency_key
        )
        ticket, conversation = await self._assigned(access, ticket_no)
        if command.replayed:
            return await self._view(ticket, conversation)
        _require_version(ticket, expected_version)
        self._add_internal_note(
            access,
            ticket,
            conversation,
            text=payload.text,
            note_type=payload.note_type,
            visibility_scope=payload.visibility_scope,
        )
        ticket.version += 1
        self.idempotency.complete(command, response_status=200, resource_no=ticket.ticket_no)
        return await self._commit_view(ticket, conversation)

    def _add_internal_note(
        self,
        access: AdminAccess,
        ticket: HumanServiceTicket,
        conversation: Conversation,
        *,
        text: str,
        note_type: str,
        visibility_scope: str,
    ) -> None:
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        self.session.add(
            HumanServiceInternalNote(
                note_no=new_prefixed_ulid("note_"),
                ticket_id=ticket.id,
                author_user_id=access.context.user.id,
                store_id=conversation.store_id,
                note_type=note_type,
                content_ciphertext=self.security.encrypt(f"support-note:{ticket.ticket_no}", text),
                content_hash=self.security.keyed_hash("support-note", text),
                visibility_scope=visibility_scope,
                key_version=1,
                request_id=request_id,
                trace_id=request_id,
            )
        )

    async def _begin_command(
        self,
        access: AdminAccess,
        ticket_no: str,
        action: str,
        payload: object,
        idempotency_key: str,
    ) -> IdempotencyClaim:
        return await self.idempotency.begin(
            scope_key=f"support:{access.context.user.user_no}:{ticket_no}:{action}",
            idempotency_key=idempotency_key,
            payload=payload,
            resource_type="human_service_ticket",
        )

    async def _commit_view(
        self, ticket: HumanServiceTicket, conversation: Conversation
    ) -> SupportTicketView:
        await self.session.commit()
        await self.session.refresh(ticket)
        await self.session.refresh(conversation)
        return await self._view(ticket, conversation)

    async def _item(
        self, ticket: HumanServiceTicket, conversation: Conversation
    ) -> SupportTicketItem:
        assignee = (
            await self.session.get(User, ticket.current_assignee_user_id)
            if ticket.current_assignee_user_id is not None
            else None
        )
        return _ticket_item(ticket, conversation, assignee)

    async def _view(
        self, ticket: HumanServiceTicket, conversation: Conversation
    ) -> SupportTicketView:
        return SupportTicketView(
            **(await self._item(ticket, conversation)).model_dump(),
            handoff_message_refs=ticket.handoff_message_refs,
            handoff_policy_version=ticket.handoff_policy_version,
            resolution_summary=ticket.resolution_summary,
        )

    async def _assigned(
        self, access: AdminAccess, ticket_no: str
    ) -> tuple[HumanServiceTicket, Conversation]:
        ticket, conversation = await self._ticket(access, ticket_no, for_update=True)
        if ticket.current_assignee_user_id != access.context.user.id:
            raise _conflict("SUPPORT_TICKET_NOT_ASSIGNED", "工单未分配给当前客服。")
        return ticket, conversation

    async def _assigned_conversation(
        self, access: AdminAccess, conversation_no: str, *, for_update: bool = False
    ) -> tuple[HumanServiceTicket, Conversation]:
        row = await self.repository.support_ticket_for_conversation(
            conversation_no, for_update=for_update
        )
        if row is None or not self._scope_allowed(access, row[1]):
            raise _not_found()
        if row[0].current_assignee_user_id != access.context.user.id:
            raise _conflict("SUPPORT_TICKET_NOT_ASSIGNED", "工单未分配给当前客服。")
        return row

    async def _ticket(
        self, access: AdminAccess, ticket_no: str, *, for_update: bool = False
    ) -> tuple[HumanServiceTicket, Conversation]:
        row = await self.repository.support_ticket(ticket_no, for_update=for_update)
        if row is None or not self._scope_allowed(access, row[1]):
            raise _not_found()
        return row

    @staticmethod
    def _scope_allowed(access: AdminAccess, conversation: Conversation) -> bool:
        return ("platform", 0) in access.scopes or (
            conversation.store_id is not None and ("store", conversation.store_id) in access.scopes
        )

    def _conversation_status(
        self,
        conversation: Conversation,
        target: str,
        event_type: str,
        access: AdminAccess,
        ticket: HumanServiceTicket,
        reason: str | None = None,
    ) -> None:
        previous = conversation.conversation_status
        conversation.conversation_status = target
        conversation.version += 1
        self.session.add(
            ConversationStatusLog(
                conversation_id=conversation.id,
                from_status=previous,
                to_status=target,
                event_type=event_type,
                actor_type="human",
                actor_id=access.context.user.id,
                ticket_id=ticket.id,
                reason=reason,
                conversation_version=conversation.version,
                trace_id=request_id_context.get(),
            )
        )

    def _ticket_event(
        self,
        ticket: HumanServiceTicket,
        event_type: str,
        from_status: str | None,
        to_status: str,
        access: AdminAccess,
        *,
        reason_code: str | None = None,
        reason: str | None = None,
        sla_before: datetime | None = None,
    ) -> None:
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        ticket_event_no = new_prefixed_ulid("hte_")
        self.session.add(
            HumanServiceTicketEvent(
                event_no=ticket_event_no,
                ticket_id=ticket.id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                actor_type="human",
                actor_user_id=access.context.user.id,
                reason_code=reason_code,
                reason=reason,
                sla_due_at_before=sla_before,
                sla_due_at_after=ticket.sla_due_at,
                ticket_version=ticket.version,
                request_id=request_id,
                trace_id=request_id,
            )
        )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="support.ticket.status_changed.v1",
                aggregate_type="human_service_ticket",
                aggregate_no=ticket.ticket_no,
                aggregate_version=ticket.version,
                payload={"ticket_id": ticket.ticket_no, "ticket_event_id": ticket_event_no},
                event_status="pending",
                available_at=utc_now(),
                attempt_count=0,
                trace_id=request_id,
            )
        )

    async def _system_message(self, conversation: Conversation, text: str) -> None:
        now = utc_now().replace(microsecond=0)
        conversation.last_sequence_no += 1
        conversation.last_message_at = now
        conversation.version += 1
        message = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=conversation.last_sequence_no,
            client_message_no=None,
            sender_type="system",
            sender_id=None,
            message_type="system",
            text_content=text,
            content_payload=None,
            message_status="sent",
            moderation_status="not_required",
            sent_at=now,
        )
        self.session.add(message)
        await self.session.flush()
        conversation.last_message_id = message.id
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        self.session.add(
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
                trace_id=request_id,
            )
        )


def _support_message_view(message: Message) -> MessageView:
    return _support_visible_message_view(message)


def _ticket_item(
    ticket: HumanServiceTicket, conversation: Conversation, assignee: User | None
) -> SupportTicketItem:
    return SupportTicketItem(
        ticket_id=ticket.ticket_no,
        conversation_id=conversation.conversation_no,
        queue_type=cast(Literal["store", "platform"], ticket.queue_type),
        queue_code=ticket.queue_code,
        ticket_type=ticket.ticket_type,
        priority=cast(Literal["low", "normal", "high", "urgent"], ticket.priority),
        ticket_status=cast(
            Literal["queued", "assigned", "active", "waiting_user", "resolved", "closed"],
            ticket.ticket_status,
        ),
        assigned_user_id=assignee.user_no if assignee is not None else None,
        handoff_summary=ticket.handoff_summary,
        sla_due_at=ticket.sla_due_at,
        waiting_reason_code=ticket.waiting_reason_code,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        version=ticket.version,
    )


def _support_visible_message_view(message: Message) -> MessageView:
    return MessageView(
        message_id=message.message_no,
        sequence_no=message.sequence_no,
        sender_type=cast(Literal["user", "agent", "human", "system", "tool"], message.sender_type),
        message_type=message.message_type,
        text=message.text_content,
        message_status=message.message_status,
        moderation_status=message.moderation_status,
        content=message.content_payload,
        sent_at=message.sent_at,
    )


def _context_view(item: ConversationContext) -> ConversationContextView:
    return ConversationContextView(
        context_id=item.context_no,
        context_type=cast(
            Literal["product", "order", "shipment", "refund", "store", "checkout_store_group"],
            item.context_type,
        ),
        resource_id=item.resource_no,
        resource_version=item.resource_version,
        context_version=item.context_version,
        status=cast(Literal["active", "inactive", "expired"], item.context_status),
        display_snapshot=item.display_snapshot,
        expires_at=item.expires_at,
    )


def _author_no(authors: dict[int, User], user_id: int) -> str:
    author = authors.get(user_id)
    return author.user_no if author is not None else "unknown"


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404, code="RESOURCE_NOT_FOUND", title="Resource not found", detail="工单不存在。"
    )


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Support state conflict", detail=detail)


def _require_version(ticket: HumanServiceTicket, expected_version: int) -> None:
    if ticket.version != expected_version:
        raise ApplicationError(
            status=412,
            code="RESOURCE_VERSION_CONFLICT",
            title="Version conflict",
            detail="工单已经变化，请刷新后重试。",
        )
