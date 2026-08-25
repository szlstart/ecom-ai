import os
import secrets
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.modules.agent_runtime.models import AiFeedback
from app.modules.identity.models import AuthSession, User
from app.modules.messaging.models import Conversation, Message

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_ai_feedback_is_owned_versioned_idempotent_and_agent_only(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    async for session in mysql_session():
        user, auth_session = _identity(security, suffix, now)
        other, other_session = _identity(security, f"other_{suffix}", now)
        session.add_all([user, other])
        await session.flush()
        auth_session.user_id = user.id
        other_session.user_id = other.id
        conversation = Conversation(
            conversation_no=new_prefixed_ulid("con_"),
            user_id=user.id,
            store_id=None,
            conversation_type="exclusive",
            is_fixed=True,
            conversation_status="active",
            last_sequence_no=2,
            last_message_at=now,
        )
        session.add_all([auth_session, other_session, conversation])
        await session.flush()
        agent_message = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=1,
            client_message_no=None,
            sender_type="agent",
            sender_id=None,
            message_type="text",
            text_content="可评价的 AI 回复",
            content_payload=None,
            ai_run_no=new_prefixed_ulid("run_"),
            message_status="sent",
            moderation_status="passed",
            sent_at=now,
        )
        human_message = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=2,
            client_message_no=None,
            sender_type="human",
            sender_id=None,
            message_type="text",
            text_content="不可评价的人工回复",
            content_payload=None,
            message_status="sent",
            moderation_status="passed",
            sent_at=now,
        )
        session.add_all([agent_message, human_message])
        await session.commit()
        conversation_no = conversation.conversation_no
        agent_message_no = agent_message.message_no
        human_message_no = human_message.message_no
        user_token, _ = security.create_access_token(
            user_no=user.user_no,
            session_no=auth_session.session_no,
            audience="user",
            permission_version=user.permission_version,
        )
        other_token, _ = security.create_access_token(
            user_no=other.user_no,
            session_no=other_session.session_no,
            audience="user",
            permission_version=other.permission_version,
        )

    path = f"/api/v1/conversations/{conversation_no}/messages/{agent_message_no}"
    headers = {"Authorization": f"Bearer {user_token}"}
    for reaction in ("thumb_up", "thumb_down", "thumb_up"):
        response = await client.put(
            f"{path}/reaction", headers=headers, json={"reaction": reaction}
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["feedback_type"] == reaction
    history = await client.get(f"/api/v1/conversations/{conversation_no}/messages", headers=headers)
    agent_view = next(
        item for item in history.json()["data"]["items"] if item["message_id"] == agent_message_no
    )
    assert agent_view["viewer_reaction"] == "thumb_up"
    deleted = await client.delete(f"{path}/reaction", headers=headers)
    deleted_again = await client.delete(f"{path}/reaction", headers=headers)
    assert deleted.status_code == deleted_again.status_code == 200
    assert deleted_again.json()["data"]["feedback_id"] is None
    history = await client.get(f"/api/v1/conversations/{conversation_no}/messages", headers=headers)
    agent_view = next(
        item for item in history.json()["data"]["items"] if item["message_id"] == agent_message_no
    )
    assert agent_view["viewer_reaction"] is None

    report_headers = {**headers, "Idempotency-Key": f"feedback-report-{suffix}-001"}
    report_payload = {"reason_code": "FACT_ERROR", "comment": "订单状态解释与页面不一致"}
    report = await client.post(f"{path}/reports", headers=report_headers, json=report_payload)
    report_replay = await client.post(
        f"{path}/reports", headers=report_headers, json=report_payload
    )
    assert report.status_code == report_replay.status_code == 201
    assert report.json()["data"]["feedback_id"] == report_replay.json()["data"]["feedback_id"]
    correction = await client.post(
        f"{path}/corrections",
        headers={**headers, "Idempotency-Key": f"feedback-correction-{suffix}-001"},
        json={"reason_code": "USER_CORRECTION", "comment": "正确说法应以订单详情页状态为准"},
    )
    assert correction.status_code == 201

    foreign = await client.put(
        f"{path}/reaction",
        headers={"Authorization": f"Bearer {other_token}"},
        json={"reaction": "thumb_up"},
    )
    human = await client.put(
        f"/api/v1/conversations/{conversation_no}/messages/{human_message_no}/reaction",
        headers=headers,
        json={"reaction": "thumb_up"},
    )
    assert foreign.status_code == human.status_code == 404

    async for session in mysql_session():
        active_reactions = int(
            await session.scalar(
                select(func.count(AiFeedback.id)).where(
                    AiFeedback.message_id == agent_message.id,
                    AiFeedback.feedback_type.in_(("thumb_up", "thumb_down")),
                    AiFeedback.feedback_status == "submitted",
                )
            )
            or 0
        )
        details = int(
            await session.scalar(
                select(func.count(AiFeedback.id)).where(
                    AiFeedback.message_id == agent_message.id,
                    AiFeedback.feedback_type.in_(("report", "correction")),
                )
            )
            or 0
        )
        assert active_reactions == 0
        assert details == 2


def _identity(security: SecurityService, suffix: str, now: datetime) -> tuple[User, AuthSession]:
    user = User(
        user_no=new_prefixed_ulid("usr_"),
        username=f"feedback_{suffix}",
        username_normalized=f"feedback_{suffix}",
        nickname="Feedback User",
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
        device_name="AI feedback integration",
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
