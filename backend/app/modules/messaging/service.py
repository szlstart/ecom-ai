from __future__ import annotations

from datetime import timedelta
from typing import Literal, cast

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.pagination import CursorCodec
from app.core.security import utc_now
from app.modules.agent_runtime.models import AiFeedback
from app.modules.files.models import FileObject
from app.modules.identity.models import User
from app.modules.messaging.content_safety import blocks_message
from app.modules.messaging.human_schemas import HumanHandoffRequest, HumanTicketView
from app.modules.messaging.models import (
    Conversation,
    ConversationContext,
    ConversationStatusLog,
    HumanServiceTicket,
    HumanServiceTicketEvent,
    Message,
    MessageRead,
)
from app.modules.messaging.repository import MessagingRepository
from app.modules.messaging.schemas import (
    ConversationArchiveView,
    ConversationContextClearView,
    ConversationContextRequest,
    ConversationContextView,
    ConversationList,
    ConversationView,
    MessageCreateRequest,
    MessageList,
    MessageView,
    ReadCursorRequest,
    ReadCursorView,
)
from app.modules.stores.models import Store
from app.modules.system.models import OutboxEvent


class MessagingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = MessagingRepository(session)
        self.idempotency = IdempotencyService(session)
        self.cursor = CursorCodec(get_settings().security_hmac_secret.get_secret_value())

    async def get_or_create_exclusive(self, user: User) -> ConversationView:
        conversation = await self.repository.exclusive(user.id)
        if conversation is None:
            conversation = Conversation(
                conversation_no=new_prefixed_ulid("conv_"),
                user_id=user.id,
                store_id=None,
                conversation_type="exclusive",
                is_fixed=True,
                conversation_status="active",
                last_sequence_no=0,
            )
            self.session.add(conversation)
            await self.session.commit()
        return await self._view(conversation, "专属客服", None)

    async def get_or_create_store(self, user: User, store_no: str) -> ConversationView:
        store = await self.session.scalar(
            select(Store).where(Store.store_no == store_no, Store.store_status == "active")
        )
        if store is None:
            raise _not_found()
        conversation = await self.repository.store_conversation(user.id, store.id, for_update=True)
        if conversation is None:
            conversation = Conversation(
                conversation_no=new_prefixed_ulid("conv_"),
                user_id=user.id,
                store_id=store.id,
                conversation_type="store",
                is_fixed=False,
                conversation_status="active",
                last_sequence_no=0,
            )
            self.session.add(conversation)
            await self.session.commit()
        elif conversation.user_hidden_at is not None:
            conversation.user_hidden_at = None
            conversation.version += 1
            self._conversation_event(
                conversation,
                conversation.conversation_status,
                "restored",
                "user",
                user.id,
                conversation.human_ticket_id,
                "USER_REOPENED_STORE_CONVERSATION",
            )
            await self.session.commit()
        return await self._view(conversation, store.store_name, store.store_no)

    async def list_mine(self, user: User) -> ConversationList:
        rows = await self.repository.conversations(user.id)
        return ConversationList(
            items=[
                await self._view(
                    conversation,
                    "专属客服"
                    if conversation.conversation_type == "exclusive"
                    else store.store_name
                    if store is not None
                    else "店铺客服",
                    store.store_no if store else None,
                )
                for conversation, store in rows
            ]
        )

    async def request_human(
        self,
        user: User,
        conversation_no: str,
        payload: HumanHandoffRequest,
        idempotency_key: str,
    ) -> HumanTicketView:
        return await self._request_human(
            user,
            conversation_no,
            payload,
            idempotency_key,
            source="user",
        )

    async def request_human_from_agent(
        self,
        user: User,
        conversation_no: str,
        payload: HumanHandoffRequest,
        run_no: str,
    ) -> HumanTicketView:
        return await self._request_human(
            user,
            conversation_no,
            payload,
            f"agent-handoff-{run_no}",
            source="agent",
        )

    async def _request_human(
        self,
        user: User,
        conversation_no: str,
        payload: HumanHandoffRequest,
        idempotency_key: str,
        *,
        source: str,
    ) -> HumanTicketView:
        claim = await self.idempotency.begin(
            scope_key=f"user:{user.user_no}:conversation:{conversation_no}:human-service",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="human_service_ticket",
        )
        if claim.replayed and claim.record.resource_no:
            replay = await self.repository.support_ticket(claim.record.resource_no)
            if replay is not None and replay[1].user_id == user.id:
                return _ticket_view(replay[0], replay[1])
            raise _not_found()
        conversation = await self.repository.by_no(user.id, conversation_no, for_update=True)
        if conversation is None:
            raise _not_found()
        existing = await self.repository.active_ticket(conversation.id)
        if existing is not None:
            self.idempotency.complete(claim, response_status=200, resource_no=existing.ticket_no)
            await self.session.commit()
            return _ticket_view(existing, conversation)
        refs: list[dict[str, object]] = []
        seen_refs: set[str] = set()
        for message_no in payload.message_refs:
            if message_no in seen_refs:
                continue
            message = await self.repository.message_by_no(conversation.id, message_no)
            if message is None or message.message_status == "hidden":
                raise _not_found()
            seen_refs.add(message_no)
            refs.append(
                {
                    "message_id": message.message_no,
                    "sequence_no": message.sequence_no,
                    "purpose": "handoff",
                }
            )
        now = utc_now().replace(microsecond=0)
        queue_type = "platform" if conversation.conversation_type == "exclusive" else "store"
        queue_code = (
            "platform.general"
            if queue_type == "platform"
            else f"store.{conversation.store_id}.general"
        )
        ticket = HumanServiceTicket(
            ticket_no=new_prefixed_ulid("tkt_"),
            conversation_id=conversation.id,
            user_id=user.id,
            store_id=conversation.store_id,
            queue_type=queue_type,
            queue_code=queue_code,
            ticket_type=payload.ticket_type,
            priority="normal",
            ticket_status="queued",
            active_key=1,
            handoff_summary=payload.summary,
            handoff_message_refs=refs,
            handoff_policy_version="handoff-v1",
            source=source,
            sla_due_at=now + timedelta(minutes=15),
        )
        self.session.add(ticket)
        await self.session.flush()
        conversation.conversation_status = "human_pending"
        conversation.human_ticket_id = ticket.id
        conversation.version += 1
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        ticket_event_no = new_prefixed_ulid("hte_")
        self.session.add(
            HumanServiceTicketEvent(
                event_no=ticket_event_no,
                ticket_id=ticket.id,
                event_type="created",
                from_status=None,
                to_status="queued",
                actor_type=source,
                actor_user_id=user.id if source == "user" else None,
                reason_code="USER_REQUESTED" if source == "user" else "AGENT_HANDOFF",
                reason=None,
                sla_due_at_before=None,
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
                available_at=now,
                attempt_count=0,
                trace_id=request_id,
            )
        )
        self._conversation_event(
            conversation,
            "active",
            "human_requested",
            source,
            user.id if source == "user" else None,
            ticket.id,
            None,
        )
        self.idempotency.complete(claim, response_status=201, resource_no=ticket.ticket_no)
        await self.session.commit()
        return _ticket_view(ticket, conversation)

    async def current_human_ticket(self, user: User, conversation_no: str) -> HumanTicketView:
        conversation = await self.repository.by_no(user.id, conversation_no)
        if conversation is None:
            raise _not_found()
        ticket = await self.repository.active_ticket(conversation.id)
        if ticket is None:
            raise _not_found()
        position = None
        if ticket.ticket_status == "queued":
            position = int(
                await self.session.scalar(
                    select(func.count(HumanServiceTicket.id)).where(
                        HumanServiceTicket.queue_code == ticket.queue_code,
                        HumanServiceTicket.ticket_status == "queued",
                        HumanServiceTicket.created_at <= ticket.created_at,
                    )
                )
                or 0
            )
        return _ticket_view(ticket, conversation).model_copy(update={"queue_position": position})

    async def cancel_human_ticket(
        self, user: User, ticket_no: str, idempotency_key: str
    ) -> HumanTicketView:
        claim = await self.idempotency.begin(
            scope_key=f"user:{user.user_no}:human-service-ticket:{ticket_no}:cancel",
            idempotency_key=idempotency_key,
            payload={},
            resource_type="human_service_ticket",
        )
        row = await self.repository.support_ticket(ticket_no, for_update=True)
        if row is None or row[0].user_id != user.id:
            raise _not_found()
        ticket, conversation = row
        if claim.replayed:
            return _ticket_view(ticket, conversation)
        if ticket.ticket_status != "queued":
            raise ApplicationError(
                status=409,
                code="HUMAN_SERVICE_CANCELLATION_NOT_ALLOWED",
                title="Cancellation not allowed",
                detail="当前人工服务请求已被处理，不能取消。",
            )
        now = utc_now().replace(microsecond=0)
        ticket.ticket_status = "closed"
        ticket.active_key = None
        ticket.closed_at = now
        ticket.resolution_code = "USER_CANCELLED"
        ticket.resolution_summary = "用户已取消人工服务请求"
        ticket.version += 1
        previous_conversation_status = conversation.conversation_status
        conversation.conversation_status = "active"
        conversation.human_ticket_id = None
        conversation.version += 1
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        ticket_event_no = new_prefixed_ulid("hte_")
        self.session.add(
            HumanServiceTicketEvent(
                event_no=ticket_event_no,
                ticket_id=ticket.id,
                event_type="cancelled",
                from_status="queued",
                to_status="closed",
                actor_type="user",
                actor_user_id=user.id,
                reason_code="USER_CANCELLED",
                reason=None,
                sla_due_at_before=ticket.sla_due_at,
                sla_due_at_after=None,
                ticket_version=ticket.version,
                request_id=request_id,
                trace_id=request_id,
            )
        )
        self._conversation_event(
            conversation,
            previous_conversation_status,
            "human_cancelled",
            "user",
            user.id,
            ticket.id,
            None,
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
                available_at=now,
                attempt_count=0,
                trace_id=request_id,
            )
        )
        self.idempotency.complete(claim, response_status=200, resource_no=ticket.ticket_no)
        await self.session.commit()
        return _ticket_view(ticket, conversation)

    async def detail(self, user: User, conversation_no: str) -> ConversationView:
        conversation = await self.repository.by_no(user.id, conversation_no)
        if conversation is None:
            raise _not_found()
        title = "专属客服"
        store_no = None
        if conversation.store_id is not None:
            store = await self.session.get(Store, conversation.store_id)
            if store is None:
                raise _not_found()
            title, store_no = store.store_name, store.store_no
        return await self._view(conversation, title, store_no, include_contexts=True)

    async def messages(
        self,
        user: User,
        conversation_no: str,
        limit: int,
        after_sequence: int = 0,
        cursor: str | None = None,
    ) -> MessageList:
        conversation = await self.repository.by_no(user.id, conversation_no)
        if conversation is None:
            raise _not_found()
        if after_sequence and cursor is not None:
            raise ApplicationError(
                status=400,
                code="MESSAGE_PAGINATION_CONFLICT",
                title="Message pagination conflict",
                detail="实时补拉位置和历史分页游标不能同时使用。",
            )
        filter_key = f"messages:{conversation.conversation_no}"
        position = self.cursor.decode(cursor, filter_key=filter_key)
        if position is not None:
            try:
                if position.direction != "previous" or len(position.values) != 1:
                    raise ValueError
                before_sequence = int(position.values[0])
                if before_sequence < 1:
                    raise ValueError
            except ValueError as exc:
                raise ApplicationError(
                    status=400,
                    code="PAGINATION_CURSOR_INVALID",
                    title="Invalid pagination cursor",
                    detail="消息分页位置无效，请重新加载会话。",
                ) from exc
            rows = await self.repository.messages_before(conversation.id, before_sequence, limit)
        elif after_sequence:
            rows = await self.repository.messages_after(conversation.id, after_sequence, limit)
        else:
            rows = await self.repository.messages(conversation.id, limit)
        reactions: dict[int, str] = {}
        if rows:
            reactions = {
                message_id: feedback_type
                for message_id, feedback_type in (
                    await self.session.execute(
                        select(AiFeedback.message_id, AiFeedback.feedback_type).where(
                            AiFeedback.user_id == user.id,
                            AiFeedback.message_id.in_([item.id for item in rows]),
                            AiFeedback.feedback_type.in_(("thumb_up", "thumb_down")),
                            AiFeedback.feedback_status == "submitted",
                        )
                    )
                ).all()
            }
        previous_cursor = None
        if (
            not after_sequence
            and rows
            and await self.repository.has_message_before(conversation.id, rows[0].sequence_no)
        ):
            previous_cursor = self.cursor.encode(
                filter_key=filter_key,
                values=(str(rows[0].sequence_no),),
                direction="previous",
            )
        return MessageList(
            items=[_message_view(message, reactions.get(message.id)) for message in rows],
            previous_cursor=previous_cursor,
        )

    async def set_context(
        self,
        user: User,
        conversation_no: str,
        context_type: str,
        payload: ConversationContextRequest,
        expected_version: int,
    ) -> ConversationContextView:
        conversation = await self.repository.by_no(user.id, conversation_no, for_update=True)
        if conversation is None:
            raise _not_found()
        if conversation.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="会话已经变化，请刷新后重试。",
            )
        snapshot: dict[str, object] = {}
        expires_at = None
        if context_type == "order":
            from app.modules.orders.models import Order

            order_filters = [
                Order.order_no == payload.resource_id,
                Order.user_id == user.id,
            ]
            if conversation.store_id is not None:
                order_filters.append(Order.store_id == conversation.store_id)
            resource = await self.session.scalar(select(Order).where(*order_filters))
            if resource is None:
                raise _not_found()
            snapshot = {"order_id": resource.order_no, "status": resource.order_status}
        elif context_type == "product":
            from app.modules.catalog.models import Product

            product_filters = [Product.product_no == payload.resource_id]
            if conversation.store_id is None:
                product_filters.append(Product.product_status == "on_sale")
            else:
                product_filters.append(Product.store_id == conversation.store_id)
            resource = await self.session.scalar(select(Product).where(*product_filters))
            if resource is None:
                raise _not_found()
            snapshot = {"product_id": resource.product_no, "name": resource.product_name}
        elif context_type == "checkout_store_group":
            from app.modules.checkout.models import CheckoutSession, CheckoutSnapshot

            checkout = await self.session.scalar(
                select(CheckoutSession).where(
                    CheckoutSession.checkout_no == payload.resource_id,
                    CheckoutSession.user_id == user.id,
                    CheckoutSession.checkout_status == "active",
                )
            )
            if (
                checkout is None
                or checkout.expires_at <= utc_now()
                or payload.resource_version != checkout.version
            ):
                raise _not_found()
            checkout_snapshot = await self.session.scalar(
                select(CheckoutSnapshot).where(
                    CheckoutSnapshot.checkout_session_id == checkout.id,
                    CheckoutSnapshot.snapshot_version == checkout.version,
                )
            )
            if checkout_snapshot is None:
                raise _not_found()
            view = checkout_snapshot.snapshot_payload.get("view", {})
            groups = view.get("store_groups", []) if isinstance(view, dict) else []
            store = await self.session.scalar(
                select(Store).where(Store.id == conversation.store_id)
            )
            if store is None:
                raise _not_found()
            store_group = next(
                (
                    group
                    for group in groups
                    if isinstance(group, dict) and group.get("store_id") == store.store_no
                ),
                None,
            )
            if store_group is None:
                raise _not_found()
            snapshot = {
                "store_id": store_group.get("store_id"),
                "items": store_group.get("items", []),
            }
            expires_at = checkout.expires_at
        elif context_type == "shipment":
            from app.modules.logistics.models import Shipment

            shipment_filters = [Shipment.shipment_no == payload.resource_id]
            if conversation.store_id is not None:
                shipment_filters.append(Shipment.store_id == conversation.store_id)
            resource = await self.session.scalar(select(Shipment).where(*shipment_filters))
            if resource is None:
                raise _not_found()
            from app.modules.orders.models import Order

            order = await self.session.scalar(
                select(Order).where(Order.id == resource.order_id, Order.user_id == user.id)
            )
            if order is None:
                raise _not_found()
            snapshot = {
                "shipment_id": resource.shipment_no,
                "status": resource.shipment_status,
                "tracking_no_masked": resource.tracking_no_masked,
            }
        elif context_type == "refund":
            from app.modules.after_sale.models import RefundApplication

            refund_filters = [
                RefundApplication.refund_no == payload.resource_id,
                RefundApplication.user_id == user.id,
            ]
            if conversation.store_id is not None:
                refund_filters.append(RefundApplication.store_id == conversation.store_id)
            resource = await self.session.scalar(select(RefundApplication).where(*refund_filters))
            if resource is None:
                raise _not_found()
            snapshot = {
                "refund_id": resource.refund_no,
                "status": resource.refund_status,
                "approved_amount": resource.approved_amount,
                "currency": resource.currency,
            }
        elif context_type == "store":
            store_filters = [
                Store.store_no == payload.resource_id,
                Store.store_status == "active",
            ]
            if conversation.store_id is not None:
                store_filters.append(Store.id == conversation.store_id)
            store = await self.session.scalar(select(Store).where(*store_filters))
            if store is None:
                raise _not_found()
            snapshot = {"store_id": store.store_no, "store_name": store.store_name}
        else:
            raise ApplicationError(
                status=422,
                code="CONVERSATION_CONTEXT_TYPE_UNSUPPORTED",
                title="Unsupported context",
                detail="当前上下文类型暂不支持设置。",
            )
        previous = await self.repository.active_context(
            conversation.id, context_type, for_update=True
        )
        next_version = (previous.context_version + 1) if previous else 1
        if previous is not None:
            previous.context_status = "inactive"
            previous.active_context_key = None
            previous.version += 1
        item = ConversationContext(
            context_no=new_prefixed_ulid("ctx_"),
            conversation_id=conversation.id,
            context_type=context_type,
            resource_no=payload.resource_id,
            resource_version=payload.resource_version,
            context_version=next_version,
            context_status="active",
            active_context_key=f"{conversation.id}:{context_type}",
            display_snapshot=snapshot,
            expires_at=expires_at,
        )
        self.session.add(item)
        conversation.version += 1
        await self.session.commit()
        return _context_view(item)

    async def clear_context(
        self,
        user: User,
        conversation_no: str,
        context_type: str,
        expected_version: int,
    ) -> ConversationContextClearView:
        conversation = await self.repository.by_no(user.id, conversation_no, for_update=True)
        if conversation is None:
            raise _not_found()
        if conversation.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="会话已经变化，请刷新后重试。",
            )
        item = await self.repository.active_context(conversation.id, context_type, for_update=True)
        cleared = item is not None
        if item is not None:
            item.context_status = "inactive"
            item.active_context_key = None
            item.version += 1
            conversation.version += 1
            await self.session.commit()
        return ConversationContextClearView(
            conversation_id=conversation.conversation_no,
            context_type=context_type,
            cleared=cleared,
            version=conversation.version,
        )

    async def archive(
        self,
        user: User,
        conversation_no: str,
        expected_version: int,
        idempotency_key: str,
    ) -> ConversationArchiveView:
        claim = await self.idempotency.begin(
            scope_key=f"user:{user.user_no}:conversation:{conversation_no}:archive",
            idempotency_key=idempotency_key,
            payload={"expected_version": expected_version},
            resource_type="conversation_archive",
        )
        conversation = await self.repository.by_no(user.id, conversation_no, for_update=True)
        if conversation is None:
            raise _not_found()
        if conversation.conversation_type == "exclusive":
            raise ApplicationError(
                status=409,
                code="EXCLUSIVE_CONVERSATION_CANNOT_BE_ARCHIVED",
                title="Conversation cannot be archived",
                detail="专属客服会话固定显示，不能隐藏。",
            )
        if claim.replayed:
            if conversation.user_hidden_at is None:
                raise ApplicationError(
                    status=409,
                    code="IDEMPOTENCY_RESOURCE_STATE_CHANGED",
                    title="Resource state changed",
                    detail="会话已重新显示，不能重放此前的隐藏结果。",
                )
            return ConversationArchiveView(
                conversation_id=conversation.conversation_no,
                archived_at=conversation.user_hidden_at,
                version=conversation.version,
            )
        if conversation.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="会话已经变化，请刷新后重试。",
            )
        now = utc_now().replace(microsecond=0)
        conversation.user_hidden_at = now
        conversation.version += 1
        self._conversation_event(
            conversation,
            conversation.conversation_status,
            "hidden",
            "user",
            user.id,
            conversation.human_ticket_id,
            "USER_ARCHIVED_STORE_CONVERSATION",
        )
        self.idempotency.complete(
            claim, response_status=201, resource_no=conversation.conversation_no
        )
        await self.session.commit()
        return ConversationArchiveView(
            conversation_id=conversation.conversation_no,
            archived_at=now,
            version=conversation.version,
        )

    async def send(
        self, user: User, conversation_no: str, payload: MessageCreateRequest
    ) -> MessageView:
        conversation = await self.repository.by_no(user.id, conversation_no, for_update=True)
        if conversation is None:
            raise _not_found()
        existing = await self.repository.client_message(conversation.id, payload.client_message_id)
        if existing is not None:
            return _message_view(existing)
        now = utc_now().replace(microsecond=0)
        previous_status = conversation.conversation_status
        active_ticket = await self.repository.active_ticket(conversation.id)
        resumed_ticket = False
        sla_before = None
        message_type, text_content, content_payload = await self._message_content(
            user, conversation, payload
        )
        blocked = bool(text_content and blocks_message(text_content))
        if not blocked:
            if conversation.user_hidden_at is not None:
                conversation.user_hidden_at = None
                conversation.version += 1
                self._conversation_event(
                    conversation,
                    conversation.conversation_status,
                    "restored",
                    "user",
                    user.id,
                    conversation.human_ticket_id,
                    "USER_SENT_MESSAGE",
                )
            if conversation.conversation_status == "closed":
                conversation.conversation_status = "active"
            if active_ticket is not None and active_ticket.ticket_status == "waiting_user":
                sla_before = active_ticket.sla_due_at
                active_ticket.ticket_status = "active"
                active_ticket.waiting_started_at = None
                active_ticket.sla_due_at = now + timedelta(
                    seconds=active_ticket.sla_remaining_seconds or 0
                )
                active_ticket.sla_remaining_seconds = None
                active_ticket.waiting_reason_code = None
                active_ticket.version += 1
                conversation.conversation_status = "human_active"
                resumed_ticket = True
        conversation.last_sequence_no += 1
        conversation.version += 1
        message = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=conversation.last_sequence_no,
            client_message_no=payload.client_message_id,
            sender_type="user",
            sender_id=user.id,
            message_type=message_type,
            text_content=text_content if not blocked else None,
            content_payload=content_payload if not blocked else None,
            message_status="hidden" if blocked else "sent",
            moderation_status="blocked" if blocked else "passed",
            sent_at=now,
        )
        self.session.add(message)
        await self.session.flush()
        if not blocked:
            conversation.last_message_at = now
            conversation.last_message_id = message.id
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        if previous_status == "closed" and not blocked:
            self._conversation_event(
                conversation, "closed", "reopened", "user", user.id, None, None
            )
        if active_ticket is not None and resumed_ticket:
            ticket_event_no = new_prefixed_ulid("hte_")
            self.session.add(
                HumanServiceTicketEvent(
                    event_no=ticket_event_no,
                    ticket_id=active_ticket.id,
                    event_type="resumed",
                    from_status="waiting_user",
                    to_status="active",
                    actor_type="user",
                    actor_user_id=user.id,
                    reason_code="USER_REPLIED",
                    reason=None,
                    sla_due_at_before=sla_before,
                    sla_due_at_after=active_ticket.sla_due_at,
                    ticket_version=active_ticket.version,
                    request_id=request_id,
                    trace_id=request_id,
                )
            )
            self.session.add(
                OutboxEvent(
                    event_no=new_prefixed_ulid("evt_"),
                    event_type="support.ticket.status_changed.v1",
                    aggregate_type="human_service_ticket",
                    aggregate_no=active_ticket.ticket_no,
                    aggregate_version=active_ticket.version,
                    payload={
                        "ticket_id": active_ticket.ticket_no,
                        "ticket_event_id": ticket_event_no,
                    },
                    event_status="pending",
                    available_at=now,
                    attempt_count=0,
                    trace_id=request_id,
                )
            )
        if blocked:
            await self.session.commit()
            return _message_view(message)
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
        if active_ticket is None and conversation.conversation_status == "active":
            context_refs = [
                {
                    "context_id": item.context_no,
                    "context_type": item.context_type,
                    "context_version": item.context_version,
                    "resource_id": item.resource_no,
                    "resource_version": item.resource_version,
                    "expires_at": item.expires_at.isoformat() if item.expires_at else None,
                }
                for item in await self.repository.active_contexts(conversation.id)
            ]
            self.session.add(
                OutboxEvent(
                    event_no=new_prefixed_ulid("evt_"),
                    event_type="message.response.requested.v1",
                    aggregate_type="conversation",
                    aggregate_no=conversation.conversation_no,
                    aggregate_version=conversation.version,
                    payload={
                        "conversation_id": conversation.conversation_no,
                        "message_id": message.message_no,
                        "context_refs": context_refs,
                    },
                    event_status="pending",
                    available_at=now,
                    attempt_count=0,
                    trace_id=request_id,
                )
            )
        await self.session.commit()
        return _message_view(message)

    async def _message_content(
        self, user: User, conversation: Conversation, payload: MessageCreateRequest
    ) -> tuple[str, str | None, dict[str, object] | None]:
        content = payload.content
        if content.type == "text":
            return "text", content.text, None
        if content.type == "product_card":
            from app.modules.catalog.models import Product, ProductImage, ProductSku
            from app.modules.inventory.models import Inventory

            statement = select(Product).where(
                Product.product_no == content.product_id,
                Product.product_status == "on_sale",
            )
            if conversation.store_id is not None:
                statement = statement.where(Product.store_id == conversation.store_id)
            product = await self.session.scalar(statement)
            if product is None:
                raise _not_found()
            store = await self.session.get(Store, product.store_id)
            if store is None:
                raise _not_found()
            sku = None
            if content.sku_id is not None:
                sku = await self.session.scalar(
                    select(ProductSku).where(
                        ProductSku.sku_no == content.sku_id,
                        ProductSku.product_id == product.id,
                        ProductSku.sku_status == "active",
                    )
                )
                if sku is None:
                    raise _not_found()
            else:
                sku = await self.session.scalar(
                    select(ProductSku)
                    .where(
                        ProductSku.product_id == product.id,
                        ProductSku.sku_status == "active",
                    )
                    .order_by(
                        case((ProductSku.id == product.default_sku_id, 0), else_=1),
                        ProductSku.id,
                    )
                    .limit(1)
                )
            inventory = (
                await self.session.scalar(select(Inventory).where(Inventory.sku_id == sku.id))
                if sku
                else None
            )
            image_file = (
                await self.session.scalar(
                    select(FileObject)
                    .join(ProductImage, ProductImage.file_id == FileObject.id)
                    .where(
                        ProductImage.product_id == product.id,
                        ProductImage.image_status == "active",
                        FileObject.file_status == "active",
                        FileObject.scan_status == "safe",
                    )
                    .order_by(
                        case((ProductImage.sku_id == sku.id, 0), else_=1),
                        ProductImage.sort_order,
                        ProductImage.id,
                    )
                    .limit(1)
                )
                if sku
                else None
            )
            logo_file = await self._public_file_by_object_key(store.logo_object_key)
            available_quantity = max(
                0,
                (inventory.on_hand_quantity - inventory.reserved_quantity)
                if inventory and inventory.inventory_status == "active"
                else 0,
            )
            return (
                "product_card",
                None,
                {
                    "schema_version": 2,
                    "product_id": product.product_no,
                    "product_name": product.product_name,
                    "product_status": product.product_status,
                    "sku_id": sku.sku_no if sku else None,
                    "sku_name": sku.sku_name if sku else None,
                    "price": (
                        {"minor_units": str(sku.sale_price_amount), "currency": sku.currency}
                        if sku
                        else None
                    ),
                    "image_url": self._file_url(image_file, thumbnail=True),
                    "available_quantity": available_quantity,
                    "stock_status": "available" if available_quantity > 0 else "sold_out",
                    "sales_count": product.sales_count,
                    "store": {
                        "store_id": store.store_no,
                        "store_name": store.store_name,
                        "store_status": store.store_status,
                        "logo_url": self._file_url(logo_file),
                    },
                },
            )
        from app.modules.orders.models import Order, OrderItem

        order_statement = select(Order).where(
            Order.order_no == content.order_id,
            Order.user_id == user.id,
        )
        if conversation.store_id is not None:
            order_statement = order_statement.where(Order.store_id == conversation.store_id)
        order = await self.session.scalar(order_statement)
        if order is None:
            raise _not_found()
        store = await self.session.get(Store, order.store_id)
        items = list(
            (
                await self.session.scalars(
                    select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
                )
            ).all()
        )
        object_keys = {
            value
            for value in [store.logo_object_key if store else None]
            + [item.image_object_key for item in items[:2]]
            if value
        }
        file_rows = (
            list(
                (
                    await self.session.scalars(
                        select(FileObject).where(
                            FileObject.object_key.in_(object_keys),
                            FileObject.file_status == "active",
                            FileObject.scan_status == "safe",
                        )
                    )
                ).all()
            )
            if object_keys
            else []
        )
        files = {item.object_key: item for item in file_rows}
        return (
            "order_card",
            None,
            {
                "schema_version": 2,
                "order_id": order.order_no,
                "display_order_id": self._masked_order_no(order.order_no),
                "order_status": order.order_status,
                "payment_status": order.payment_status,
                "fulfillment_status": order.fulfillment_status,
                "after_sale_status": order.after_sale_status,
                "store": {
                    "store_id": store.store_no if store else None,
                    "store_name": store.store_name if store else "店铺",
                    "logo_url": self._file_url(
                        files.get(store.logo_object_key or "") if store else None
                    ),
                },
                "items": [
                    {
                        "product_id": item.product_no,
                        "sku_id": item.sku_no,
                        "product_name": item.product_name,
                        "sku_name": item.sku_name,
                        "quantity": item.quantity,
                        "image_url": self._file_url(
                            files.get(item.image_object_key or ""), thumbnail=True
                        ),
                    }
                    for item in items[:2]
                ],
                "item_count": len(items),
                "total_quantity": sum(item.quantity for item in items),
                "payable_amount": {
                    "minor_units": str(order.payable_amount),
                    "currency": order.currency,
                },
                "created_at": order.created_at.isoformat(),
            },
        )

    async def _public_file_by_object_key(self, object_key: str | None) -> FileObject | None:
        if not object_key:
            return None
        return cast(
            FileObject | None,
            await self.session.scalar(
                select(FileObject).where(
                    FileObject.object_key == object_key,
                    FileObject.file_status == "active",
                    FileObject.scan_status == "safe",
                )
            ),
        )

    @staticmethod
    def _file_url(file_object: FileObject | None, *, thumbnail: bool = False) -> str | None:
        if file_object is None:
            return None
        suffix = "?variant=thumbnail" if thumbnail else ""
        return f"/api/v1/files/{file_object.file_no}{suffix}"

    @staticmethod
    def _masked_order_no(order_no: str) -> str:
        if len(order_no) <= 10:
            return order_no
        return f"{order_no[:6]}…{order_no[-4:]}"

    async def read(
        self, user: User, conversation_no: str, payload: ReadCursorRequest
    ) -> ReadCursorView:
        conversation = await self.repository.by_no(user.id, conversation_no, for_update=True)
        if conversation is None:
            raise _not_found()
        message = await self.repository.message_by_no(conversation.id, payload.last_read_message_id)
        if message is None or message.sequence_no != payload.last_read_sequence_no:
            raise _not_found()
        cursor = await self.repository.read_cursor(conversation.id, user.id, for_update=True)
        now = utc_now().replace(microsecond=0)
        cursor_advanced = False
        if cursor is None:
            cursor = MessageRead(
                conversation_id=conversation.id,
                reader_type="user",
                reader_id=user.id,
                last_read_message_id=message.id,
                last_read_sequence_no=message.sequence_no,
                last_read_at=now,
                # SQLAlchemy applies mapped defaults during flush. The outbox
                # event below is assembled before that flush, so an explicit
                # initial version is required to avoid publishing NULL into a
                # non-null aggregate_version column on a user's first read.
                version=0,
            )
            self.session.add(cursor)
            cursor_advanced = True
        elif message.sequence_no > cursor.last_read_sequence_no:
            cursor.last_read_message_id = message.id
            cursor.last_read_sequence_no = message.sequence_no
            cursor.last_read_at = now
            cursor.version += 1
            cursor_advanced = True
        if cursor_advanced:
            self.session.add(
                OutboxEvent(
                    event_no=new_prefixed_ulid("evt_"),
                    event_type="message.read_cursor.updated.v1",
                    aggregate_type="conversation",
                    aggregate_no=conversation.conversation_no,
                    aggregate_version=cursor.version,
                    payload={
                        "conversation_id": conversation.conversation_no,
                        "last_read_message_id": message.message_no,
                        "last_read_sequence_no": message.sequence_no,
                        "cursor_version": cursor.version,
                    },
                    event_status="pending",
                    available_at=now,
                    attempt_count=0,
                    trace_id=request_id_context.get() or new_prefixed_ulid("req_"),
                )
            )
        await self.session.commit()
        return ReadCursorView(
            conversation_id=conversation.conversation_no,
            last_read_message_id=message.message_no,
            last_read_sequence_no=cursor.last_read_sequence_no,
            unread_count=await self.repository.unread_count(
                conversation.id, cursor.last_read_sequence_no
            ),
            total_unread_count=await self._total_unread(user.id),
            cursor_version=cursor.version,
        )

    async def _view(
        self,
        conversation: Conversation,
        title: str,
        store_no: str | None,
        *,
        include_contexts: bool = False,
    ) -> ConversationView:
        cursor = await self.repository.read_cursor(conversation.id, conversation.user_id)
        last_message = await self.repository.last_visible_message(conversation)
        last_read = cursor.last_read_sequence_no if cursor else 0
        return ConversationView(
            conversation_id=conversation.conversation_no,
            conversation_type=cast(Literal["exclusive", "store"], conversation.conversation_type),
            conversation_status=cast(
                Literal["active", "human_pending", "human_active", "closed"],
                conversation.conversation_status,
            ),
            store_id=store_no,
            title=title,
            is_fixed=conversation.is_fixed,
            fixed_rank=0 if conversation.is_fixed else None,
            last_message_preview=_message_preview(last_message),
            last_message_at=conversation.last_message_at,
            last_sequence_no=conversation.last_sequence_no,
            unread_count=await self.repository.unread_count(conversation.id, last_read),
            version=conversation.version,
            active_contexts=(
                [
                    _context_view(item)
                    for item in await self.repository.active_contexts(conversation.id)
                ]
                if include_contexts
                else []
            ),
        )

    async def _total_unread(self, user_id: int) -> int:
        conversations = await self.repository.conversations(user_id)
        total = 0
        for conversation, _store in conversations:
            cursor = await self.repository.read_cursor(conversation.id, user_id)
            total += await self.repository.unread_count(
                conversation.id, cursor.last_read_sequence_no if cursor else 0
            )
        return total

    def _conversation_event(
        self,
        conversation: Conversation,
        from_status: str,
        event_type: str,
        actor_type: str,
        actor_id: int | None,
        ticket_id: int | None,
        reason: str | None,
    ) -> None:
        trace_id = request_id_context.get()
        self.session.add(
            ConversationStatusLog(
                conversation_id=conversation.id,
                from_status=from_status,
                to_status=conversation.conversation_status,
                event_type=event_type,
                actor_type=actor_type,
                actor_id=actor_id,
                ticket_id=ticket_id,
                reason=reason,
                conversation_version=conversation.version,
                trace_id=trace_id,
            )
        )


def _message_view(message: Message, viewer_reaction: str | None = None) -> MessageView:
    return MessageView(
        message_id=message.message_no,
        sequence_no=message.sequence_no,
        sender_type=cast(Literal["user", "agent", "human", "system", "tool"], message.sender_type),
        message_type=message.message_type,
        text=message.text_content,
        message_status=message.message_status,
        moderation_status=message.moderation_status,
        content=message.content_payload,
        viewer_reaction=cast(Literal["thumb_up", "thumb_down"] | None, viewer_reaction),
        sent_at=message.sent_at,
    )


def _message_preview(message: Message | None) -> str | None:
    if message is None:
        return None
    if message.text_content:
        return message.text_content[:80]
    return {
        "product_card": "[商品卡片]",
        "order_card": "[订单卡片]",
        "system": "[系统消息]",
    }.get(message.message_type, "[新消息]")


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


def _ticket_view(ticket: HumanServiceTicket, conversation: Conversation) -> HumanTicketView:
    return HumanTicketView(
        ticket_id=ticket.ticket_no,
        conversation_id=conversation.conversation_no,
        queue_type=cast(Literal["store", "platform"], ticket.queue_type),
        ticket_status=cast(
            Literal["queued", "assigned", "active", "waiting_user", "resolved", "closed"],
            ticket.ticket_status,
        ),
        # The user projection never exposes the internal numeric administrator identifier.
        assigned_user_id=None,
        resolution_summary=ticket.resolution_summary,
        estimated_response_at=ticket.sla_due_at if ticket.ticket_status == "queued" else None,
        can_cancel=ticket.ticket_status == "queued",
    )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="会话或消息不存在。",
    )
