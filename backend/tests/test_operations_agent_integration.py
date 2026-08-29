import os
import secrets

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.bootstrap.ai_runtime import seed_ai_runtime
from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.database.postgres import postgres_session
from app.modules.agent_runtime.checkpoints import AgentCheckpointStore
from app.modules.agent_runtime.models import (
    AgentDefinition,
    AgentDelegation,
    AgentRun,
    AgentToolAudit,
    AgentVersion,
)
from app.modules.agent_runtime.operations_agent import process_operations_run
from app.modules.identity.models import User
from app.modules.messaging.models import Conversation, Message
from app.modules.rbac.models import Role, UserRole

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_admin_copilot_runs_bounded_parallel_read_only_specialists(
    client: AsyncClient,
) -> None:
    del client
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    async for session in mysql_session():
        await seed_ai_runtime(session)
        definition = await session.scalar(
            select(AgentDefinition).where(AgentDefinition.agent_code == "admin_copilot")
        )
        assert definition is not None
        version = await session.scalar(
            select(AgentVersion)
            .where(
                AgentVersion.agent_id == definition.id,
                AgentVersion.version_status == "published",
            )
            .order_by(AgentVersion.version_no.desc())
        )
        assert version is not None and version.version_no >= 2
        user = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"multi_admin_{suffix}",
            username_normalized=f"multi_admin_{suffix}",
            nickname="Multi-agent admin",
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            permission_version=1,
            registered_at=now,
        )
        session.add(user)
        await session.flush()
        role = await session.scalar(select(Role).where(Role.role_code == "platform_super_admin"))
        assert role is not None
        session.add(
            UserRole(
                user_id=user.id,
                role_id=role.id,
                grant_no=new_prefixed_ulid("grt_"),
                scope_type="platform",
                scope_id=0,
                grant_status="active",
                active_grant_key=security.keyed_hash(
                    "active-role-grant", f"{user.id}:{role.id}:platform:0"
                ),
                granted_by=user.id,
                granted_at=now,
                grant_reason="multi_agent_integration_admin",
            )
        )
        conversation = Conversation(
            conversation_no=new_prefixed_ulid("cv_"),
            user_id=user.id,
            conversation_type="exclusive",
            is_fixed=True,
            conversation_status="active",
            last_sequence_no=1,
            last_message_at=now,
        )
        session.add(conversation)
        await session.flush()
        trigger = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=1,
            sender_type="user",
            sender_id=user.id,
            message_type="text",
            text_content="分析用户、店铺、订单和运行故障",
            message_status="sent",
            moderation_status="passed",
            sent_at=now,
        )
        session.add(trigger)
        await session.flush()
        run = AgentRun(
            run_no=new_prefixed_ulid("run_"),
            conversation_id=conversation.id,
            trigger_message_id=trigger.id,
            agent_version_id=version.id,
            run_status="queued",
            current_phase="queued",
            trace_id=new_prefixed_ulid("trc_"),
            context_snapshot=[],
        )
        session.add(run)
        await session.commit()
        run_no = run.run_no
        trigger_no = trigger.message_no
        break

    async for session in mysql_session():
        loaded_run = await session.scalar(select(AgentRun).where(AgentRun.run_no == run_no))
        assert loaded_run is not None
        async for postgres in postgres_session():
            await process_operations_run(
                session,
                loaded_run,
                checkpoint_store=AgentCheckpointStore(postgres),
                model_gateway=None,
            )
            break
        await session.commit()
        assert loaded_run.run_status == "completed"
        response = await session.scalar(
            select(Message)
            .where(
                Message.conversation_id == loaded_run.conversation_id,
                Message.sender_type == "agent",
                Message.sequence_no > 1,
            )
            .order_by(Message.sequence_no.desc())
        )
        assert response is not None and response.content_payload is not None
        trace = response.content_payload["execution_trace"]
        assert isinstance(trace, dict)
        assert trace["orchestration_mode"] == "multi_agent"
        steps = trace["steps"]
        assert isinstance(steps, list)
        delegated = [item for item in steps if item.get("kind") == "delegation"]
        assert len(delegated) == 4
        assert all(item.get("status") == "succeeded" for item in delegated)
        delegation_count = int(
            await session.scalar(
                select(func.count(AgentDelegation.id)).where(
                    AgentDelegation.run_id == loaded_run.id
                )
            )
            or 0
        )
        audit_count = int(
            await session.scalar(
                select(func.count(AgentToolAudit.id)).where(
                    AgentToolAudit.run_id == loaded_run.id
                )
            )
            or 0
        )
        assert delegation_count == 4
        assert audit_count == 4
        assert trigger_no not in str(response.content_payload)
        break
