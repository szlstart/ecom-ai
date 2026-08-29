from __future__ import annotations

import hashlib
import time
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import utc_now
from app.modules.agent_runtime.delegation import (
    DelegationPacket,
    DelegationStatus,
    SpecialistResult,
)
from app.modules.agent_runtime.models import AgentDelegation, AgentRun


class SQLDelegationLedger:
    """Durable idempotency and trace ledger using an isolated session per child task."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def get(self, packet: DelegationPacket) -> SpecialistResult | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(AgentDelegation).where(
                    AgentDelegation.fingerprint == bytes.fromhex(packet.fingerprint),
                    AgentDelegation.delegation_status.in_(("succeeded", "partial")),
                )
            )
            if row is None:
                return None
            return SpecialistResult(
                specialist_code=row.specialist_code,
                status=cast(DelegationStatus, row.delegation_status),
                safe_data=row.result_snapshot or {},
                tokens_used=row.tokens_used,
                tool_calls=row.tool_calls,
                model_calls=row.model_calls,
                scope=packet.trusted_scope,
            )

    async def start(
        self,
        packet: DelegationPacket,
        *,
        dependency_nos: tuple[str, ...],
    ) -> None:
        async with self.session_factory() as session, session.begin():
            run = await session.scalar(
                select(AgentRun).where(AgentRun.run_no == packet.parent_run_no)
            )
            if run is None:
                raise LookupError("parent Agent Run does not exist")
            fingerprint = bytes.fromhex(packet.fingerprint)
            row = await session.scalar(
                select(AgentDelegation)
                .where(AgentDelegation.fingerprint == fingerprint)
                .with_for_update()
            )
            if row is None:
                row = _new_row(packet, run, dependency_nos)
                session.add(row)
            if row.delegation_status not in {"succeeded", "partial"}:
                row.delegation_status = "running"
                row.started_at = row.started_at or utc_now()
                row.version += 1

    async def put(
        self,
        packet: DelegationPacket,
        result: SpecialistResult,
        *,
        dependency_nos: tuple[str, ...],
    ) -> None:
        async with self.session_factory() as session, session.begin():
            run = await session.scalar(
                select(AgentRun).where(AgentRun.run_no == packet.parent_run_no)
            )
            if run is None:
                raise LookupError("parent Agent Run does not exist")
            fingerprint = bytes.fromhex(packet.fingerprint)
            row = await session.scalar(
                select(AgentDelegation)
                .where(AgentDelegation.fingerprint == fingerprint)
                .with_for_update()
            )
            if row is not None and row.delegation_status in {"succeeded", "partial"}:
                return
            now = utc_now()
            if row is None:
                row = _new_row(packet, run, dependency_nos)
                session.add(row)
            row.delegation_status = (
                "succeeded" if result.status == "reused" else result.status
            )
            row.result_snapshot = dict(result.safe_data) if result.safe_data else None
            row.tokens_used = result.tokens_used
            row.tool_calls = result.tool_calls
            row.model_calls = result.model_calls
            row.error_code = result.error_code
            row.finished_at = now
            row.version += 1


def _new_row(
    packet: DelegationPacket,
    run: AgentRun,
    dependency_nos: tuple[str, ...],
) -> AgentDelegation:
    return AgentDelegation(
        delegation_no=packet.delegation_no,
        run_id=run.id,
        subtask_key=packet.subtask_key,
        specialist_code=packet.specialist_code,
        specialist_version=packet.specialist_version,
        fingerprint=bytes.fromhex(packet.fingerprint),
        depth=packet.depth,
        delegation_status="queued",
        objective_hash=hashlib.sha256(packet.objective.encode()).digest(),
        scope_snapshot=packet.trusted_scope.safe_snapshot(),
        resource_refs=[
            {
                "resource_type": item.resource_type,
                "resource_ref": _stable_ref(item.resource_no),
                "version": item.version,
            }
            for item in packet.resource_refs
        ],
        dependency_nos=list(dependency_nos),
        allowed_tools_snapshot=sorted(packet.allowed_tools),
        budget_snapshot={
            "deadline_remaining_ms": max(
                0, int((packet.budget.deadline_monotonic - time.monotonic()) * 1000)
            ),
            "token_limit": packet.budget.token_limit,
            "tool_call_limit": packet.budget.tool_call_limit,
            "model_call_limit": packet.budget.model_call_limit,
        },
        trace_id=run.trace_id,
        span_id=_span_id(packet),
        started_at=utc_now(),
        tokens_used=0,
        tool_calls=0,
        model_calls=0,
        version=0,
    )


def _stable_ref(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _span_id(packet: DelegationPacket) -> str:
    value = f"{packet.parent_run_no}:{packet.delegation_no}".encode()
    return hashlib.sha256(value).hexdigest()[:16]
