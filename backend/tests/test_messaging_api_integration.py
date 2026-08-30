import json
import os
import secrets
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.api.dependencies import AuthContext
from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, TokenClaims, utc_now
from app.database.mysql import mysql_session
from app.database.redis import get_redis
from app.modules.identity.models import AuthSession, User
from app.modules.messaging.models import (
    Conversation,
    HumanServiceAssignment,
    HumanServiceInternalNote,
    HumanServiceTicket,
    HumanServiceTicketEvent,
    Message,
)
from app.modules.messaging.support_schemas import (
    SupportInternalNoteRequest,
    SupportMessageRequest,
    SupportResolveRequest,
    SupportTransferRequest,
    SupportWaitRequest,
)
from app.modules.messaging.support_service import SupportService
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.models import Permission, Role, RolePermission, UserRole
from app.modules.realtime.channels import user_channel
from app.modules.realtime.relay import RealtimeOutboxRelay
from app.modules.realtime.tickets import RealtimeTicketService
from app.modules.stores.models import Store
from app.modules.system.models import OutboxEvent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_conversation_uniqueness_message_replay_moderation_and_human_handoff(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    async for session in mysql_session():
        user, user_session = _identity(security, suffix, now)
        other_user, other_session = _identity(security, f"other_{suffix}", now)
        merchant_user, _merchant_session = _identity(security, f"merchant_{suffix}", now)
        target_user, target_admin_session = _identity(security, f"support_{suffix}", now)
        target_admin_session.audience = "admin"
        target_admin_session.authentication_methods = ["password", "totp"]
        target_admin_session.assurance_level = "aal2"
        session.add_all([user, other_user, merchant_user, target_user])
        await session.flush()
        consumer_role = await session.scalar(select(Role).where(Role.role_code == "user"))
        assert consumer_role is not None
        session.add_all(
            [
                UserRole(
                    user_id=consumer.id,
                    role_id=consumer_role.id,
                    grant_no=new_prefixed_ulid("grt_"),
                    scope_type="platform",
                    scope_id=0,
                    grant_status="active",
                    active_grant_key=security.keyed_hash(
                        "active-role-grant",
                        f"{consumer.id}:{consumer_role.id}:platform:0",
                    ),
                    granted_by=user.id,
                    granted_at=now,
                    grant_reason="Messaging integration consumer",
                )
                for consumer in (user, other_user)
            ]
        )
        user_session.user_id = user.id
        other_session.user_id = other_user.id
        target_admin_session.user_id = target_user.id
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=merchant_user.id,
            store_name=f"消息店铺 {suffix}",
            store_name_normalized=f"messaging-store-{suffix}",
            store_status="active",
            rating_score=Decimal("0.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=now,
        )
        session.add_all([user_session, other_session, target_admin_session, store])
        await session.commit()
        user_token, _ = security.create_access_token(
            user_no=user.user_no,
            session_no=user_session.session_no,
            audience="user",
            permission_version=user.permission_version,
        )
        other_token, _ = security.create_access_token(
            user_no=other_user.user_no,
            session_no=other_session.session_no,
            audience="user",
            permission_version=other_user.permission_version,
        )
        store_no = store.store_no
        user_no = user.user_no
        target_user_no = target_user.user_no
        break

    headers = {"Authorization": f"Bearer {user_token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}
    exclusive_first = await client.put("/api/v1/users/me/exclusive-conversation", headers=headers)
    exclusive_second = await client.put("/api/v1/users/me/exclusive-conversation", headers=headers)
    assert exclusive_first.status_code == exclusive_second.status_code == 200
    exclusive = exclusive_first.json()["data"]
    assert exclusive_second.json()["data"]["conversation_id"] == exclusive["conversation_id"]
    assert exclusive["title"] == "专属客服"
    assert exclusive["is_fixed"] is True

    realtime_ticket_response = await client.post("/api/v1/realtime/tickets", headers=headers)
    assert realtime_ticket_response.status_code == 200
    assert realtime_ticket_response.headers["cache-control"] == "no-store"
    realtime_ticket = realtime_ticket_response.json()["data"]
    assert realtime_ticket["websocket_path"] == "/ws/v1"
    assert realtime_ticket["ticket"] not in realtime_ticket["websocket_path"]
    ticket_service = RealtimeTicketService(get_redis(), get_settings())
    assert await ticket_service.consume(realtime_ticket["ticket"]) is not None
    assert await ticket_service.consume(realtime_ticket["ticket"]) is None

    store_first = await client.put(
        f"/api/v1/stores/{store_no}/customer-service-conversation", headers=headers
    )
    store_second = await client.put(
        f"/api/v1/stores/{store_no}/customer-service-conversation", headers=headers
    )
    assert store_first.status_code == store_second.status_code == 200
    assert (
        store_first.json()["data"]["conversation_id"]
        == store_second.json()["data"]["conversation_id"]
    )
    store_conversation = store_first.json()["data"]
    store_conversation_no = store_conversation["conversation_id"]
    context = await client.put(
        f"/api/v1/conversations/{store_conversation_no}/contexts/store",
        headers={**headers, "If-Match": f'"v{store_conversation["version"]}"'},
        json={"resource_id": store_no, "resource_version": None},
    )
    assert context.status_code == 200
    store_detail = await client.get(
        f"/api/v1/conversations/{store_conversation_no}", headers=headers
    )
    assert store_detail.status_code == 200
    assert store_detail.json()["data"]["active_contexts"] == [context.json()["data"]]
    current_store_version = store_detail.json()["data"]["version"]
    cleared = await client.delete(
        f"/api/v1/conversations/{store_conversation_no}/contexts/store",
        headers={**headers, "If-Match": f'"v{current_store_version}"'},
    )
    assert cleared.status_code == 200
    assert cleared.json()["data"]["cleared"] is True
    archived = await client.post(
        f"/api/v1/conversations/{store_conversation_no}/archivals",
        headers={
            **headers,
            "If-Match": f'"v{cleared.json()["data"]["version"]}"',
            "Idempotency-Key": f"archive-{suffix}-0001",
        },
    )
    assert archived.status_code == 201
    hidden_list = await client.get("/api/v1/conversations", headers=headers)
    assert store_conversation_no not in {
        item["conversation_id"] for item in hidden_list.json()["data"]["items"]
    }
    restored = await client.put(
        f"/api/v1/stores/{store_no}/customer-service-conversation", headers=headers
    )
    assert restored.status_code == 200
    assert restored.json()["data"]["conversation_id"] == store_conversation_no
    visible_list = await client.get("/api/v1/conversations", headers=headers)
    assert store_conversation_no in {
        item["conversation_id"] for item in visible_list.json()["data"]["items"]
    }

    conversations = await client.get("/api/v1/conversations", headers=headers)
    assert conversations.status_code == 200
    assert conversations.json()["data"]["items"][0]["conversation_type"] == "exclusive"

    conversation_no = exclusive["conversation_id"]
    client_message_id = f"cmsg_{secrets.token_hex(13).upper()}"
    payload = {"client_message_id": client_message_id, "content": {"type": "text", "text": "你好"}}
    sent = await client.post(
        f"/api/v1/conversations/{conversation_no}/messages", headers=headers, json=payload
    )
    replay = await client.post(
        f"/api/v1/conversations/{conversation_no}/messages", headers=headers, json=payload
    )
    assert sent.status_code == replay.status_code == 201
    assert replay.json()["data"] == sent.json()["data"]

    first_read = await client.put(
        f"/api/v1/conversations/{conversation_no}/read-cursor",
        headers=headers,
        json={
            "last_read_message_id": sent.json()["data"]["message_id"],
            "last_read_sequence_no": sent.json()["data"]["sequence_no"],
        },
    )
    assert first_read.status_code == 200, first_read.text
    assert first_read.json()["data"]["cursor_version"] == 0
    async for session in mysql_session():
        read_event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_no == conversation_no,
                OutboxEvent.event_type == "message.read_cursor.updated.v1",
            )
        )
        assert read_event is not None
        assert read_event.aggregate_version == 0
        assert read_event.payload["cursor_version"] == 0
        break

    blocked = await client.post(
        f"/api/v1/conversations/{conversation_no}/messages",
        headers=headers,
        json={
            "client_message_id": f"cmsg_{secrets.token_hex(13).upper()}",
            "content": {"type": "text", "text": "<script>alert(1)</script>"},
        },
    )
    assert blocked.status_code == 201
    assert blocked.json()["data"]["moderation_status"] == "blocked"
    assert blocked.json()["data"]["message_status"] == "hidden"

    foreign = await client.get(
        f"/api/v1/conversations/{conversation_no}/messages", headers=other_headers
    )
    assert foreign.status_code == 404

    handoff_payload = {
        "ticket_type": "general",
        "summary": "希望转人工继续处理",
        "message_refs": [sent.json()["data"]["message_id"]],
    }
    handoff_headers = {**headers, "Idempotency-Key": f"handoff-{suffix}-0001"}
    handoff = await client.post(
        f"/api/v1/conversations/{conversation_no}/human-service-tickets",
        headers=handoff_headers,
        json=handoff_payload,
    )
    handoff_replay = await client.post(
        f"/api/v1/conversations/{conversation_no}/human-service-tickets",
        headers=handoff_headers,
        json=handoff_payload,
    )
    assert handoff.status_code == handoff_replay.status_code == 201
    assert handoff_replay.json()["data"]["ticket_id"] == handoff.json()["data"]["ticket_id"]
    assert handoff.json()["data"]["queue_type"] == "platform"
    handoff_messages = await client.get(
        f"/api/v1/conversations/{conversation_no}/messages?limit=20",
        headers=headers,
    )
    assert handoff_messages.status_code == 200
    connecting = [
        item
        for item in handoff_messages.json()["data"]["items"]
        if item["sender_type"] == "system"
        and item["content"].get("event") == "human_handoff_connecting"
    ]
    assert len(connecting) == 1
    assert connecting[0]["text"].startswith("正在接入人工客服")

    after_handoff = await client.post(
        f"/api/v1/conversations/{conversation_no}/messages",
        headers=headers,
        json={
            "client_message_id": f"cmsg_{secrets.token_hex(13).upper()}",
            "content": {"type": "text", "text": "补充一条信息"},
        },
    )
    assert after_handoff.status_code == 201

    recent_page = await client.get(
        f"/api/v1/conversations/{conversation_no}/messages?limit=1",
        headers=headers,
    )
    assert recent_page.status_code == 200, recent_page.text
    assert (
        recent_page.json()["data"]["items"][0]["message_id"]
        == (after_handoff.json()["data"]["message_id"])
    )
    previous_cursor = recent_page.json()["data"]["previous_cursor"]
    assert previous_cursor
    older_page = await client.get(
        f"/api/v1/conversations/{conversation_no}/messages",
        headers=headers,
        params={"limit": 1, "cursor": previous_cursor},
    )
    assert older_page.status_code == 200, older_page.text
    assert older_page.json()["data"]["items"][0]["content"]["event"] == "human_handoff_connecting"
    oldest_page = await client.get(
        f"/api/v1/conversations/{conversation_no}/messages",
        headers=headers,
        params={
            "limit": 1,
            "cursor": older_page.json()["data"]["previous_cursor"],
        },
    )
    assert oldest_page.status_code == 200, oldest_page.text
    assert oldest_page.json()["data"]["items"][0]["message_id"] == sent.json()["data"]["message_id"]
    pagination_conflict = await client.get(
        f"/api/v1/conversations/{conversation_no}/messages",
        headers=headers,
        params={"after_sequence": 1, "cursor": previous_cursor},
    )
    assert pagination_conflict.status_code == 400
    assert pagination_conflict.json()["code"] == "MESSAGE_PAGINATION_CONFLICT"

    async for session in mysql_session():
        ticket = await session.scalar(
            select(HumanServiceTicket).where(
                HumanServiceTicket.ticket_no == handoff.json()["data"]["ticket_id"]
            )
        )
        assert ticket is not None
        assert ticket.handoff_policy_version == "handoff-v1"
        assert ticket.handoff_message_refs == [
            {
                "message_id": sent.json()["data"]["message_id"],
                "sequence_no": sent.json()["data"]["sequence_no"],
                "purpose": "handoff",
            }
        ]
        blocked_row = await session.scalar(
            select(Message).where(Message.message_no == blocked.json()["data"]["message_id"])
        )
        assert blocked_row is not None and blocked_row.text_content is None
        response_requests = int(
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.aggregate_no == conversation_no,
                    OutboxEvent.event_type == "message.response.requested.v1",
                )
            )
            or 0
        )
        assert response_requests == 1
        permission = await session.scalar(
            select(Permission).where(Permission.permission_code == "support:claim")
        )
        assert permission is not None
        target = await session.scalar(select(User).where(User.user_no == target_user_no))
        assert target is not None
        support_role = Role(
            role_no=new_prefixed_ulid("rol_"),
            role_code=f"support_transfer_target_{suffix}",
            role_name="Support transfer target",
            scope_type="platform",
            role_type="custom",
            description="Messaging integration role",
            role_status="active",
        )
        session.add(support_role)
        await session.flush()
        session.add_all(
            [
                RolePermission(
                    role_id=support_role.id,
                    permission_id=permission.id,
                    condition_config=None,
                    granted_by=user.id,
                ),
                UserRole(
                    user_id=target.id,
                    role_id=support_role.id,
                    grant_no=new_prefixed_ulid("grt_"),
                    scope_type="platform",
                    scope_id=0,
                    grant_status="active",
                    active_grant_key=security.keyed_hash(
                        "active-role-grant", f"{target.id}:{support_role.id}:platform:0"
                    ),
                    granted_by=user.id,
                    granted_at=now,
                    grant_reason="Messaging integration",
                ),
            ]
        )
        loaded_target_session = await session.scalar(
            select(AuthSession).where(
                AuthSession.user_id == target.id, AuthSession.audience == "admin"
            )
        )
        assert loaded_target_session is not None
        admin_session = AuthSession(
            session_no=new_prefixed_ulid("ses_"),
            user_id=user.id,
            refresh_token_hash=security.keyed_hash("refresh-token", secrets.token_urlsafe()),
            token_family_no=new_prefixed_ulid("tfa_"),
            device_no=new_prefixed_ulid("dev_"),
            device_name="Support integration",
            client_type="web",
            audience="admin",
            csrf_token_hash=security.keyed_hash("csrf-token", secrets.token_urlsafe()),
            authenticated_at=now,
            authentication_methods=["password", "totp"],
            assurance_level="aal2",
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
        )
        session.add(admin_session)
        await session.commit()
        access = AdminAccess(
            context=AuthContext(
                user=user,
                session=admin_session,
                claims=TokenClaims(
                    subject=user.user_no,
                    session_id=admin_session.session_no,
                    audience="admin",
                    permission_version=user.permission_version,
                    expires_at=admin_session.expires_at,
                ),
            ),
            permission=permission,
            scopes=(("platform", 0),),
        )
        target_access = AdminAccess(
            context=AuthContext(
                user=target,
                session=loaded_target_session,
                claims=TokenClaims(
                    subject=target.user_no,
                    session_id=loaded_target_session.session_no,
                    audience="admin",
                    permission_version=target.permission_version,
                    expires_at=loaded_target_session.expires_at,
                ),
            ),
            permission=permission,
            scopes=(("platform", 0),),
        )
        await session.commit()
        ticket_version = ticket.version
        break

    async for session in mysql_session():
        support = SupportService(session)
        claimed = await support.claim(
            access,
            handoff.json()["data"]["ticket_id"],
            ticket_version,
            f"claim-{suffix}-0001",
        )
        assert claimed.ticket_status == "active"
        claim_replay = await support.claim(
            access,
            handoff.json()["data"]["ticket_id"],
            ticket_version,
            f"claim-{suffix}-0001",
        )
        assert claim_replay.version == claimed.version
        waiting = await support.wait(
            access,
            handoff.json()["data"]["ticket_id"],
            SupportWaitRequest(reason_code="NEED_MORE_INFO", reason="请补充问题细节"),
            claimed.version,
            f"wait-{suffix}-0001",
        )
        assert waiting.ticket_status == "waiting_user"
        wait_replay = await support.wait(
            access,
            handoff.json()["data"]["ticket_id"],
            SupportWaitRequest(reason_code="NEED_MORE_INFO", reason="请补充问题细节"),
            claimed.version,
            f"wait-{suffix}-0001",
        )
        assert wait_replay.version == waiting.version
        break

    resume_message = await client.post(
        f"/api/v1/conversations/{conversation_no}/messages",
        headers=headers,
        json={
            "client_message_id": f"cmsg_{secrets.token_hex(13).upper()}",
            "content": {"type": "text", "text": "这是补充信息"},
        },
    )
    assert resume_message.status_code == 201

    async for session in mysql_session():
        support = SupportService(session)
        ticket = await session.scalar(
            select(HumanServiceTicket).where(
                HumanServiceTicket.ticket_no == handoff.json()["data"]["ticket_id"]
            )
        )
        assert ticket is not None and ticket.ticket_status == "active"
        support_payload = SupportMessageRequest(
            client_message_id=f"cmsg_{secrets.token_hex(13).upper()}",
            text="您好，我来协助处理。",
        )
        support_message = await support.send(
            access,
            ticket.ticket_no,
            support_payload,
        )
        support_replay = await support.send(access, ticket.ticket_no, support_payload)
        assert support_replay == support_message
        blocked_support = await support.send(
            access,
            ticket.ticket_no,
            SupportMessageRequest(
                client_message_id=f"cmsg_{secrets.token_hex(13).upper()}",
                text="javascript:alert(1)",
            ),
        )
        assert blocked_support.moderation_status == "blocked"
        transferred = await support.transfer(
            access,
            ticket.ticket_no,
            SupportTransferRequest(
                assigned_user_id=target_user_no,
                reason="由目标客服继续跟进",
            ),
            ticket.version,
            f"transfer-{suffix}-0001",
        )
        assert transferred.ticket_status == "assigned"
        with pytest.raises(ApplicationError) as old_assignee_denied:
            await support.send(
                access,
                ticket.ticket_no,
                SupportMessageRequest(
                    client_message_id=f"cmsg_{secrets.token_hex(13).upper()}",
                    text="旧客服不应继续发送",
                ),
            )
        assert old_assignee_denied.value.code == "SUPPORT_TICKET_NOT_ASSIGNED"
        accepted = await support.claim(
            target_access,
            ticket.ticket_no,
            transferred.version,
            f"accept-{suffix}-0001",
        )
        assert accepted.ticket_status == "active"
        await support.note(
            target_access,
            ticket.ticket_no,
            SupportInternalNoteRequest(text="仅坐席可见的处理记录", note_type="handling"),
            accepted.version,
            f"note-{suffix}-0001",
        )
        await session.refresh(ticket)
        resolved = await support.resolve(
            target_access,
            ticket.ticket_no,
            SupportResolveRequest(
                resolution_code="ANSWERED",
                summary="问题已经解释清楚",
                internal_note="已核对必要信息",
            ),
            ticket.version,
            f"resolve-{suffix}-0001",
        )
        assert resolved.ticket_status == "resolved"
        break

    async for session in mysql_session():
        ticket = await session.scalar(
            select(HumanServiceTicket).where(
                HumanServiceTicket.ticket_no == handoff.json()["data"]["ticket_id"]
            )
        )
        assert ticket is not None
        conversation = await session.get(Conversation, ticket.conversation_id)
        assignments = list(
            (
                await session.scalars(
                    select(HumanServiceAssignment)
                    .where(HumanServiceAssignment.ticket_id == ticket.id)
                    .order_by(HumanServiceAssignment.id)
                )
            ).all()
        )
        notes = list(
            (
                await session.scalars(
                    select(HumanServiceInternalNote)
                    .where(HumanServiceInternalNote.ticket_id == ticket.id)
                    .order_by(HumanServiceInternalNote.id)
                )
            ).all()
        )
        events = list(
            (
                await session.scalars(
                    select(HumanServiceTicketEvent)
                    .where(HumanServiceTicketEvent.ticket_id == ticket.id)
                    .order_by(HumanServiceTicketEvent.id)
                )
            ).all()
        )
        assert conversation is not None and conversation.conversation_status == "active"
        assert conversation.human_ticket_id is None
        assert [item.assignment_status for item in assignments] == ["released", "completed"]
        assert ticket.resolution_note is None
        assert len(notes) == 2
        assert (
            security.decrypt(f"support-note:{ticket.ticket_no}", notes[0].content_ciphertext)
            == "仅坐席可见的处理记录"
        )
        assert notes[1].note_type == "resolution"
        assert (
            security.decrypt(f"support-note:{ticket.ticket_no}", notes[1].content_ciphertext)
            == "已核对必要信息"
        )
        assert [item.event_type for item in events] == [
            "created",
            "claimed",
            "waiting_user",
            "resumed",
            "transferred",
            "accepted",
            "resolved",
        ]
        visible_messages = list(
            (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.sequence_no)
                )
            ).all()
        )
        assert [item.message_type for item in visible_messages[-2:]] == [
            "system",
            "resolution_check",
        ]
        assert visible_messages[-2].text_content == "人工服务已结束。如有新问题，请继续发送消息。"
        resolution_check_no = visible_messages[-1].message_no
        agent_requests = int(
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.aggregate_no == conversation_no,
                    OutboxEvent.event_type == "message.response.requested.v1",
                )
            )
            or 0
        )
        assert agent_requests == 1
        break

    resolution_feedback = await client.post(
        f"/api/v1/conversations/{conversation_no}/messages/"
        f"{resolution_check_no}/resolution-responses",
        headers=headers,
        json={
            "client_message_id": f"cmsg_{secrets.token_hex(13).upper()}",
            "resolved": True,
        },
    )
    assert resolution_feedback.status_code == 201
    assert [item["sender_type"] for item in resolution_feedback.json()["data"]["items"]] == [
        "user",
        "agent",
    ]
    assert resolution_feedback.json()["data"]["items"][1]["text"].startswith("谢谢你的确认")

    cancellable_headers = {**headers, "Idempotency-Key": f"handoff-cancel-{suffix}-0001"}
    cancellable = await client.post(
        f"/api/v1/conversations/{store_conversation_no}/human-service-tickets",
        headers=cancellable_headers,
        json={"ticket_type": "general", "summary": "稍后再咨询", "message_refs": []},
    )
    assert cancellable.status_code == 201
    current_ticket = await client.get(
        f"/api/v1/conversations/{store_conversation_no}/human-service-ticket",
        headers=headers,
    )
    assert current_ticket.status_code == 200
    assert current_ticket.json()["data"]["can_cancel"] is True
    cancellation_headers = {
        **headers,
        "Idempotency-Key": f"cancel-human-{suffix}-0001",
    }
    cancellation_path = (
        f"/api/v1/human-service-tickets/{cancellable.json()['data']['ticket_id']}/cancellations"
    )
    cancelled = await client.post(cancellation_path, headers=cancellation_headers)
    cancelled_replay = await client.post(cancellation_path, headers=cancellation_headers)
    assert cancelled.status_code == cancelled_replay.status_code == 200
    assert cancelled.json()["data"]["ticket_status"] == "closed"
    assert cancelled_replay.json()["data"] == cancelled.json()["data"]

    pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
    channel = user_channel(get_settings().environment, user_no)
    await pubsub.subscribe(channel)
    try:
        async for session in mysql_session():
            processed = await RealtimeOutboxRelay(
                session, get_redis(), get_settings()
            ).process_batch(1000)
            assert processed > 0
            break
        frames: list[dict[str, object]] = []
        for _ in range(200):
            item = await pubsub.get_message(timeout=0.05)
            if item and isinstance(item.get("data"), str):
                frames.append(json.loads(item["data"]))
            if any(
                _is_realtime_frame(frame, "support.status.updated", conversation_no)
                for frame in frames
            ):
                break
        assert any(
            _is_realtime_frame(frame, "message.created", conversation_no) for frame in frames
        )
        assert any(
            _is_realtime_frame(frame, "support.status.updated", conversation_no) for frame in frames
        )
    finally:
        await pubsub.aclose()  # type: ignore[no-untyped-call]

    deleted = await client.delete(
        f"/api/v1/conversations/{conversation_no}",
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["conversation_id"] == conversation_no
    assert deleted.json()["data"]["memory_cleared"] is True
    missing_after_delete = await client.get(
        f"/api/v1/conversations/{conversation_no}",
        headers=headers,
    )
    assert missing_after_delete.status_code == 404
    recreated = await client.put(
        "/api/v1/users/me/exclusive-conversation",
        headers=headers,
    )
    assert recreated.status_code == 200, recreated.text
    assert recreated.json()["data"]["conversation_id"] != conversation_no


def _is_realtime_frame(frame: dict[str, object], event_type: str, conversation_no: str) -> bool:
    data = frame.get("data")
    return (
        frame.get("type") == event_type
        and isinstance(data, dict)
        and data.get("conversation_id") == conversation_no
    )


def _identity(security: SecurityService, suffix: str, now: datetime) -> tuple[User, AuthSession]:
    user = User(
        user_no=new_prefixed_ulid("usr_"),
        username=f"message_{suffix}",
        username_normalized=f"message_{suffix}",
        nickname="Message User",
        user_status="active",
        locale="zh-CN",
        timezone="Asia/Shanghai",
        permission_version=1,
        registered_at=now,
    )
    auth_session = AuthSession(
        session_no=new_prefixed_ulid("ses_"),
        user_id=0,
        refresh_token_hash=security.keyed_hash("refresh-token", secrets.token_urlsafe()),
        token_family_no=new_prefixed_ulid("tfa_"),
        device_no=new_prefixed_ulid("dev_"),
        device_name="Messaging integration",
        client_type="web",
        audience="user",
        csrf_token_hash=security.keyed_hash("csrf-token", secrets.token_urlsafe()),
        authenticated_at=now,
        authentication_methods=["password"],
        assurance_level="aal1",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        last_seen_at=now,
    )
    return user, auth_session
