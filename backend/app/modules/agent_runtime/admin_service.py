from __future__ import annotations

from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import utc_now
from app.modules.agent_runtime.models import (
    AgentDefinition,
    AgentRefundDraft,
    AgentRun,
    AgentToolApproval,
    AgentVersion,
)
from app.modules.agent_runtime.schemas import AdminAgentRunView, AgentRunStatus
from app.modules.messaging.models import Conversation
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.system.models import OutboxEvent


class AdminAgentRuntimeService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.idempotency = IdempotencyService(session)

    async def get(self, access: AdminAccess, run_no: str) -> AdminAgentRunView:
        _ = access
        row = await self._run(run_no, lock=False)
        if row is None:
            raise _not_found()
        return _view(*row)

    async def cancel(
        self,
        access: AdminAccess,
        run_no: str,
        reason: str,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminAgentRunView:
        claim = await self.idempotency.begin(
            scope_key=f"admin:agent-run-cancel:{run_no}:{access.context.user.user_no}",
            idempotency_key=idempotency_key,
            payload={"reason": reason, "expected_version": expected_version},
            resource_type="agent_run",
        )
        if claim.replayed and claim.record.response_body is not None:
            return AdminAgentRunView.model_validate(claim.record.response_body)
        row = await self._run(run_no, lock=True)
        if row is None:
            raise _not_found()
        run, conversation, version, definition = row
        if run.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="Agent Run 已发生变化，请刷新后重试。",
            )
        if run.run_status not in {"queued", "waiting"}:
            raise ApplicationError(
                status=409,
                code="AGENT_RUN_CANCEL_NOT_ALLOWED",
                title="Agent Run cannot be cancelled",
                detail="仅尚未执行或等待用户确认的 Run 可安全取消，运行中或终态 Run 不执行强杀。",
            )
        approvals = list(
            (
                await self.session.scalars(
                    select(AgentToolApproval)
                    .where(
                        AgentToolApproval.run_id == run.id,
                        AgentToolApproval.approval_status == "pending",
                    )
                    .with_for_update()
                )
            ).all()
        )
        for approval in approvals:
            approval.approval_status = "expired"
            approval.version += 1
        drafts = list(
            (
                await self.session.scalars(
                    select(AgentRefundDraft)
                    .where(
                        AgentRefundDraft.run_id == run.id,
                        AgentRefundDraft.draft_status == "active",
                    )
                    .with_for_update()
                )
            ).all()
        )
        for draft in drafts:
            draft.draft_status = "invalidated"
            draft.version += 1
        before: dict[str, object] = {
            "status": run.run_status,
            "phase": run.current_phase,
        }
        run.run_status = "cancelled"
        run.current_phase = "cancelled"
        run.error_code = "ADMIN_CANCELLED"
        run.version += 1
        record_admin_operation(
            self.session,
            access,
            action="agent.run.cancel",
            target_type="agent_run",
            target_no=run.run_no,
            reason=reason,
            before=before,
            after={"status": run.run_status, "expired_approvals": len(approvals)},
        )
        now = utc_now()
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="agent.run.cancelled.v1",
                aggregate_type="agent_run",
                aggregate_no=run.run_no,
                aggregate_version=run.version,
                payload={
                    "run_id": run.run_no,
                    "conversation_id": conversation.conversation_no,
                    "reason_code": "ADMIN_CANCELLED",
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=request_id_context.get() or run.trace_id,
            )
        )
        await self.session.flush()
        await self.session.refresh(run, attribute_names=["updated_at"])
        result = _view(run, conversation, version, definition)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=run.run_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def _run(
        self, run_no: str, *, lock: bool
    ) -> tuple[AgentRun, Conversation, AgentVersion, AgentDefinition] | None:
        statement = (
            select(AgentRun, Conversation, AgentVersion, AgentDefinition)
            .join(Conversation, Conversation.id == AgentRun.conversation_id)
            .join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)
            .join(AgentDefinition, AgentDefinition.id == AgentVersion.agent_id)
            .where(AgentRun.run_no == run_no)
        )
        if lock:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1], row[2], row[3]) if row is not None else None


def _view(
    run: AgentRun,
    conversation: Conversation,
    version: AgentVersion,
    definition: AgentDefinition,
) -> AdminAgentRunView:
    return AdminAgentRunView(
        run_id=run.run_no,
        status=cast(AgentRunStatus, run.run_status),
        current_phase=run.current_phase,
        agent_code=definition.agent_code,
        agent_version_no=version.version_no,
        conversation_type=cast(Literal["exclusive", "store"], conversation.conversation_type),
        trace_id=run.trace_id,
        context_ref_count=len(run.context_snapshot),
        error_code=run.error_code,
        degraded_reason=run.degraded_reason,
        available_actions=["cancel"] if run.run_status in {"queued", "waiting"} else [],
        created_at=run.created_at,
        updated_at=run.updated_at,
        version=run.version,
    )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="Agent Run 不存在。",
    )
