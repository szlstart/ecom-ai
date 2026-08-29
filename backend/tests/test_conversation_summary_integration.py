import os
import secrets

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.database.postgres import postgres_session
from app.modules.agent_runtime.context_window import ContextWindowBuilder
from app.modules.agent_runtime.conversation_summary import ConversationSummaryRuntime
from app.modules.identity.models import User
from app.modules.messaging.models import Conversation, Message

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_conversation_summary_is_encrypted_bounded_and_scope_bound(
    client: AsyncClient,
) -> None:
    del client
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    async for mysql in mysql_session():
        user = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"summary_{suffix}",
            username_normalized=f"summary_{suffix}",
            nickname="Summary User",
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            permission_version=1,
            registered_at=now,
        )
        mysql.add(user)
        await mysql.flush()
        conversation = Conversation(
            conversation_no=new_prefixed_ulid("cnv_"),
            user_id=user.id,
            store_id=None,
            conversation_type="exclusive",
            is_fixed=True,
            conversation_status="active",
            last_sequence_no=19,
            last_message_at=now,
        )
        mysql.add(conversation)
        await mysql.flush()
        messages: list[Message] = []
        for sequence in range(1, 20):
            message = Message(
                message_no=new_prefixed_ulid("msg_"),
                conversation_id=conversation.id,
                sequence_no=sequence,
                sender_type="user" if sequence % 2 else "agent",
                sender_id=user.id if sequence % 2 else None,
                message_type="text",
                text_content=(
                    "我的邮箱 summary-secret@example.com，偏好安静键盘"
                    if sequence == 1
                    else f"第 {sequence} 轮继续比较键盘"
                ),
                message_status="sent",
                moderation_status="passed",
                sent_at=now,
            )
            messages.append(message)
        mysql.add_all(messages)
        await mysql.commit()

        trigger = messages[-1]
        window = await ContextWindowBuilder(mysql).build(conversation, trigger)
        assert len(window.recent_turns) == 10
        async for postgres in postgres_session():
            summary = await ConversationSummaryRuntime(mysql, postgres, security).load_or_update(
                conversation,
                trigger,
                user_no=user.user_no,
                store_no=None,
            )
            assert summary is not None
            assert summary.message_count == 9
            assert "summary-secret@example.com" not in summary.text
            assert "[邮箱已隐藏]" in summary.text
            row = (
                (
                    await postgres.execute(
                        text(
                            """SELECT user_no,store_no,summary_ciphertext,quality_flags
                        FROM memory.summaries WHERE summary_no=:summary_no"""
                        ),
                        {"summary_no": summary.summary_no},
                    )
                )
                .mappings()
                .one()
            )
            assert row["user_no"] == user.user_no
            assert row["store_no"] is None
            assert b"summary-secret" not in row["summary_ciphertext"]
            assert row["quality_flags"]["business_fact_authoritative"] is False
            assert (
                security.decrypt("ai-conversation-summary", row["summary_ciphertext"])
                == summary.text
            )
            break
        break
