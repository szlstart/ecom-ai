from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import new_prefixed_ulid
from app.modules.agent_runtime.exclusive_context import TrustedExclusiveAgentContext
from app.modules.agent_runtime.operations_context import TrustedOperationsContext
from app.modules.agent_runtime.store_context import TrustedStoreAgentContext


class AgentCheckpointStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def initialize(self, context: TrustedStoreAgentContext) -> None:
        await self._initialize(
            run_no=context.run.run_no,
            conversation_no=context.conversation.conversation_no,
            trigger_message_no=context.trigger.message_no,
            user_no=context.user.user_no,
            store_no=context.store.store_no,
            agent_version_no=str(context.agent_version.version_no),
            graph_version="store-agent-v1",
            trace_id=context.run.trace_id,
        )

    async def initialize_exclusive(self, context: TrustedExclusiveAgentContext) -> None:
        await self._initialize(
            run_no=context.run.run_no,
            conversation_no=context.conversation.conversation_no,
            trigger_message_no=context.trigger.message_no,
            user_no=context.user.user_no,
            store_no=None,
            agent_version_no=str(context.agent_version.version_no),
            graph_version="exclusive-agent-v1",
            trace_id=context.run.trace_id,
        )

    async def initialize_operations(self, context: TrustedOperationsContext) -> None:
        await self._initialize(
            run_no=context.run.run_no,
            conversation_no=context.conversation.conversation_no,
            trigger_message_no=context.trigger.message_no,
            user_no=context.user.user_no,
            store_no=context.store.store_no if context.store else None,
            agent_version_no=str(context.agent_version.version_no),
            graph_version=f"{context.agent_definition.agent_code}-v1",
            trace_id=context.run.trace_id,
        )

    async def _initialize(
        self,
        *,
        run_no: str,
        conversation_no: str,
        trigger_message_no: str,
        user_no: str,
        store_no: str | None,
        agent_version_no: str,
        graph_version: str,
        trace_id: str,
    ) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO agent_runtime.run_state_refs (
                    run_no, thread_id, conversation_no, trigger_message_no,
                    user_no, store_no, agent_version_no, graph_version,
                    status, current_phase, trace_id
                ) VALUES (
                    :run_no, :thread_id, :conversation_no, :trigger_message_no,
                    :user_no, :store_no, :agent_version_no, :graph_version,
                    'running', 'planning', :trace_id
                )
                ON CONFLICT (run_no) DO UPDATE SET
                    status = CASE
                        WHEN agent_runtime.run_state_refs.status IN
                            ('completed', 'failed', 'cancelled')
                        THEN agent_runtime.run_state_refs.status
                        ELSE EXCLUDED.status
                    END,
                    current_phase = CASE
                        WHEN agent_runtime.run_state_refs.status IN
                            ('completed', 'failed', 'cancelled')
                        THEN agent_runtime.run_state_refs.current_phase
                        ELSE EXCLUDED.current_phase
                    END,
                    updated_at = now()
                """
            ),
            {
                "run_no": run_no,
                "thread_id": conversation_no,
                "conversation_no": conversation_no,
                "trigger_message_no": trigger_message_no,
                "user_no": user_no,
                "store_no": store_no,
                "agent_version_no": agent_version_no,
                "graph_version": graph_version,
                "trace_id": trace_id,
            },
        )
        await self.session.commit()

    async def write(
        self,
        run_no: str,
        phase: str,
        state: Mapping[str, object],
        *,
        status: str = "running",
    ) -> str:
        safe_state = _safe_state(state)
        state_json = json.dumps(
            safe_state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        state_hash = hashlib.sha256(state_json.encode()).digest()
        checkpoint_id = new_prefixed_ulid("acp_")
        # The MySQL run row prevents normal duplicate execution. This PostgreSQL
        # transaction-scoped lock also serializes recovery workers for one run.
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:run_no))"),
            {"run_no": run_no},
        )
        sequence = int(
            await self.session.scalar(
                text(
                    "SELECT COALESCE(MAX(checkpoint_seq), 0) + 1 "
                    "FROM agent_runtime.checkpoints WHERE run_no = :run_no"
                ),
                {"run_no": run_no},
            )
            or 1
        )
        await self.session.execute(
            text(
                """
                INSERT INTO agent_runtime.checkpoints (
                    checkpoint_id, run_no, checkpoint_seq, phase, state_json, state_hash
                ) VALUES (
                    :checkpoint_id, :run_no, :checkpoint_seq, :phase,
                    CAST(:state_json AS JSONB), :state_hash
                )
                """
            ),
            {
                "checkpoint_id": checkpoint_id,
                "run_no": run_no,
                "checkpoint_seq": sequence,
                "phase": phase,
                "state_json": state_json,
                "state_hash": state_hash,
            },
        )
        await self.session.execute(
            text(
                """
                INSERT INTO agent_runtime.checkpoint_writes (
                    write_no, checkpoint_id, run_no, write_type,
                    write_status, payload_hash
                ) VALUES (
                    :write_no, :checkpoint_id, :run_no, 'state_snapshot',
                    'completed', :payload_hash
                )
                """
            ),
            {
                "write_no": new_prefixed_ulid("cpw_"),
                "checkpoint_id": checkpoint_id,
                "run_no": run_no,
                "payload_hash": state_hash,
            },
        )
        await self.session.execute(
            text(
                """
                UPDATE agent_runtime.run_state_refs
                SET status = :status,
                    current_phase = :phase,
                    last_checkpoint_id = :checkpoint_id,
                    updated_at = now()
                WHERE run_no = :run_no
                """
            ),
            {
                "status": status,
                "phase": phase,
                "checkpoint_id": checkpoint_id,
                "run_no": run_no,
            },
        )
        await self.session.commit()
        return checkpoint_id


_FORBIDDEN_STATE_KEYS = frozenset(
    {
        "text",
        "content",
        "safe_text",
        "system_prompt",
        "token",
        "password",
        "address",
        "phone",
        "email",
    }
)


def _safe_state(value: Mapping[str, object]) -> dict[str, object]:
    _reject_forbidden_state(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(encoded.encode()) > 16_384:
        raise ValueError("checkpoint state exceeds size limit")
    return dict(value)


def _reject_forbidden_state(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).casefold() in _FORBIDDEN_STATE_KEYS:
                raise ValueError("checkpoint state contains forbidden content")
            _reject_forbidden_state(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_forbidden_state(nested)
