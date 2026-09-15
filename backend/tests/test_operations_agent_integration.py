import os
import secrets
from typing import Any, cast

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
from app.modules.agent_runtime.provider_gateway import (
    OperationsSupervisorGoal,
    OperationsSupervisorPlan,
    OperationsSupervisorSubtask,
)
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


class _PromptWritePlanningGateway:
    model_name = "acceptance-planner"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def plan_tasks(self, user_text: str, agent_kind: str) -> OperationsSupervisorPlan:
        self.calls.append((user_text, agent_kind))
        return OperationsSupervisorPlan(
            tasks=(
                OperationsSupervisorSubtask(
                    "task_1",
                    "ai_governance",
                    "为 admin_copilot 创建新的系统 Prompt 草稿，不直接发布",
                ),
            ),
            confidence=0.98,
            goal_ledger=(
                OperationsSupervisorGoal(
                    "goal_1",
                    "创建版本化 Prompt 草稿并保持线上版本不变",
                    "task_1",
                ),
            ),
            coverage_complete=True,
        )


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

        trace_conversation = await session.get(Conversation, loaded_run.conversation_id)
        assert trace_conversation is not None
        trace_conversation.last_sequence_no += 1
        trace_trigger = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=trace_conversation.id,
            sequence_no=trace_conversation.last_sequence_no,
            sender_type="user",
            sender_id=trace_conversation.user_id,
            message_type="text",
            text_content=f"查看 {run_no} 的执行链路",
            message_status="sent",
            moderation_status="passed",
            sent_at=utc_now(),
        )
        session.add(trace_trigger)
        await session.flush()
        trace_run = AgentRun(
            run_no=new_prefixed_ulid("run_"),
            conversation_id=trace_conversation.id,
            trigger_message_id=trace_trigger.id,
            agent_version_id=loaded_run.agent_version_id,
            run_status="queued",
            current_phase="queued",
            trace_id=new_prefixed_ulid("trc_"),
            context_snapshot=[],
        )
        session.add(trace_run)
        await session.commit()
        async for postgres in postgres_session():
            await process_operations_run(
                session,
                trace_run,
                checkpoint_store=AgentCheckpointStore(postgres),
                model_gateway=None,
            )
            break
        await session.commit()
        trace_response = await session.scalar(
            select(Message)
            .where(Message.ai_run_no == trace_run.run_no, Message.sender_type == "agent")
            .order_by(Message.id.desc())
        )
        assert trace_response is not None and trace_response.content_payload is not None
        trace_payload = cast(dict[str, Any], trace_response.content_payload)
        tool_calls = trace_payload["execution_trace"]["tool_calls"]
        assert tool_calls[0]["tool_code"] == "observability.traces.get"
        detail_cards = trace_payload["detail_cards"]
        assert detail_cards[0]["kind"] == "admin_agent_trace"
        assert run_no in detail_cards[0]["summary"]

        trace_conversation.last_sequence_no += 1
        governance_trigger = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=trace_conversation.id,
            sequence_no=trace_conversation.last_sequence_no,
            sender_type="user",
            sender_id=trace_conversation.user_id,
            message_type="text",
            text_content="列出 Skill 的发布版本、工具绑定、确认策略和调用预算",
            message_status="sent",
            moderation_status="passed",
            sent_at=utc_now(),
        )
        session.add(governance_trigger)
        await session.flush()
        governance_run = AgentRun(
            run_no=new_prefixed_ulid("run_"),
            conversation_id=trace_conversation.id,
            trigger_message_id=governance_trigger.id,
            agent_version_id=loaded_run.agent_version_id,
            run_status="queued",
            current_phase="queued",
            trace_id=new_prefixed_ulid("trc_"),
            context_snapshot=[],
        )
        session.add(governance_run)
        await session.commit()
        async for postgres in postgres_session():
            await process_operations_run(
                session,
                governance_run,
                checkpoint_store=AgentCheckpointStore(postgres),
                model_gateway=None,
            )
            break
        await session.commit()
        governance_response = await session.scalar(
            select(Message)
            .where(Message.ai_run_no == governance_run.run_no, Message.sender_type == "agent")
            .order_by(Message.id.desc())
        )
        assert governance_response is not None and governance_response.content_payload is not None
        governance_payload = cast(dict[str, Any], governance_response.content_payload)
        assert governance_payload["execution_trace"]["tool_calls"][0]["tool_code"] == (
            "governance.ai.skills.list"
        )
        governance_cards = governance_payload["detail_cards"]
        assert governance_cards[0]["kind"] == "admin_ai_governance"
        skill_cards = [
            card for card in governance_cards if card["kind"] == "admin_skill_definition"
        ]
        assert skill_cards
        assert any(row["label"] == "绑定工具" for row in skill_cards[0]["rows"])
        break


async def test_operations_write_is_model_planned_before_safe_prompt_draft_preview(
    client: AsyncClient,
) -> None:
    del client
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    gateway = _PromptWritePlanningGateway()
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
        assert version is not None
        user = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"write_planner_{suffix}",
            username_normalized=f"write_planner_{suffix}",
            nickname="Write planner admin",
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
                grant_reason="model_planned_write_integration",
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
        prompt = "你是平台治理助手，所有实时结论必须来自授权工具，所有写入必须等待确认。"
        trigger = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=1,
            sender_type="user",
            sender_id=user.id,
            message_type="text",
            text_content=f"把 Agent admin_copilot 的系统提示词改为：{prompt}",
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
        break

    async for session in mysql_session():
        loaded_run = await session.scalar(select(AgentRun).where(AgentRun.run_no == run_no))
        assert loaded_run is not None
        async for postgres in postgres_session():
            await process_operations_run(
                session,
                loaded_run,
                checkpoint_store=AgentCheckpointStore(postgres),
                model_gateway=cast(Any, gateway),
                security=security,
            )
            break
        await session.commit()
        assert gateway.calls and gateway.calls[0][1] == "admin_copilot"
        assert loaded_run.run_status == "waiting"
        response = await session.scalar(
            select(Message).where(Message.ai_run_no == loaded_run.run_no)
        )
        assert response is not None and response.message_type == "agent_action_approval"
        payload = cast(dict[str, Any], response.content_payload)
        trace = cast(dict[str, Any], payload["execution_trace"])
        assert trace["status"] == "waiting_confirmation"
        assert trace["planning_source"] == "provider_model_supervisor"
        assert trace["goal_ledger"] == [
            {
                "goal_key": "goal_1",
                "description": "创建版本化 Prompt 草稿并保持线上版本不变",
                "assigned_task_key": "task_1",
            }
        ]
        assert trace["planned_tasks"][0]["intent"] == "ai_governance"
        assert trace["steps"][0]["provider_request_sent"] is True
        assert trace["steps"][-1]["status"] == "waiting"
        assert payload["tool_code"] == "governance.ai.agents.prompt_draft.create.commit"
