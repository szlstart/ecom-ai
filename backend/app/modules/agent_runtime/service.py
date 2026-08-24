from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import utc_now
from app.modules.agent_runtime.models import (
    AgentDefinition,
    AgentRun,
    AgentVersion,
    UserAgentConsent,
)
from app.modules.agent_runtime.schemas import (
    AgentConsentGrantRequest,
    AgentConsentList,
    AgentConsentStatus,
    AgentConsentView,
    AgentRunStatus,
    AgentRunView,
)
from app.modules.identity.models import User
from app.modules.messaging.models import Conversation, Message


class AgentRuntimeService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.idempotency = IdempotencyService(session)

    async def get(self, user: User, run_no: str) -> AgentRunView:
        row = (
            await self.session.execute(
                select(AgentRun, Conversation)
                .join(Conversation, Conversation.id == AgentRun.conversation_id)
                .where(AgentRun.run_no == run_no, Conversation.user_id == user.id)
            )
        ).one_or_none()
        if row is None:
            raise _not_found()
        return _view(row[0], row[1])

    async def enqueue_for_message(
        self,
        conversation: Conversation,
        message: Message,
        trace_id: str,
        *,
        context_snapshot: Sequence[Mapping[str, Any]] = (),
    ) -> AgentRun:
        existing = await self.session.scalar(
            select(AgentRun).where(AgentRun.trigger_message_id == message.id)
        )
        if existing is not None:
            return existing
        agent_code = (
            "exclusive_support"
            if conversation.conversation_type == "exclusive"
            else "store_support"
        )
        row = (
            await self.session.execute(
                select(AgentVersion, AgentDefinition)
                .join(AgentDefinition, AgentDefinition.id == AgentVersion.agent_id)
                .where(
                    AgentDefinition.agent_code == agent_code,
                    AgentDefinition.agent_status == "active",
                    AgentVersion.version_status == "published",
                    self._definition_scope(conversation),
                )
                .order_by(AgentVersion.version_no.desc())
            )
        ).first()
        if row is None:
            raise ApplicationError(
                status=503,
                code="AGENT_VERSION_UNAVAILABLE",
                title="Agent unavailable",
                detail="智能客服暂不可用，可转人工客服。",
                retryable=True,
            )
        version, _definition = row
        refs = _normalize_context_snapshot(context_snapshot)
        run = AgentRun(
            run_no=new_prefixed_ulid("run_"),
            conversation_id=conversation.id,
            trigger_message_id=message.id,
            agent_version_id=version.id,
            run_status="queued",
            current_phase="queued",
            trace_id=trace_id,
            context_snapshot=refs,
        )
        if refs:
            run.context_no = str(refs[-1]["context_id"])
            context_version = refs[-1]["context_version"]
            if not isinstance(context_version, int) or isinstance(context_version, bool):
                raise AssertionError("normalized context version must be an integer")
            run.context_version = context_version
        self.session.add(run)
        return run

    @staticmethod
    def _definition_scope(conversation: Conversation) -> ColumnElement[bool]:
        if conversation.conversation_type == "store":
            return or_(
                and_(
                    AgentDefinition.scope_type == "store",
                    AgentDefinition.store_id == conversation.store_id,
                ),
                and_(
                    AgentDefinition.scope_type == "platform",
                    AgentDefinition.store_id.is_(None),
                    AgentDefinition.strategy_reuse_approved.is_(True),
                ),
            )
        return and_(AgentDefinition.scope_type == "platform", AgentDefinition.store_id.is_(None))

    async def list_consents(self, user: User) -> AgentConsentList:
        rows = list(
            (
                await self.session.scalars(
                    select(UserAgentConsent)
                    .where(UserAgentConsent.user_id == user.id)
                    .order_by(UserAgentConsent.created_at.desc(), UserAgentConsent.id.desc())
                )
            ).all()
        )
        return AgentConsentList(items=[_consent_view(item) for item in rows])

    async def grant_consent(
        self, user: User, payload: AgentConsentGrantRequest, idempotency_key: str
    ) -> AgentConsentView:
        claim = await self.idempotency.begin(
            scope_key=f"agent-consent:grant:{user.user_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="agent_consent",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.session.scalar(
                select(UserAgentConsent).where(
                    UserAgentConsent.user_id == user.id,
                    UserAgentConsent.consent_no == claim.record.resource_no,
                )
            )
            if existing is not None:
                return _consent_view(existing)
        if payload.expires_at is not None and payload.expires_at <= utc_now():
            raise ApplicationError(
                status=422,
                code="AI_CONSENT_EXPIRY_INVALID",
                title="Invalid expiry",
                detail="授权到期时间必须晚于当前时间。",
            )
        consent = UserAgentConsent(
            consent_no=new_prefixed_ulid("con_"),
            user_id=user.id,
            consent_type=payload.consent_type,
            scope_type=payload.scope_type,
            scope_no=payload.scope_id,
            policy_version=payload.policy_version,
            consent_status="active",
            expires_at=payload.expires_at,
        )
        self.session.add(consent)
        await self.session.flush()
        self.idempotency.complete(claim, response_status=201, resource_no=consent.consent_no)
        await self.session.commit()
        return _consent_view(consent)

    async def consent_command(self, user: User, consent_no: str, command: str) -> AgentConsentView:
        consent = await self.session.scalar(
            select(UserAgentConsent)
            .where(UserAgentConsent.user_id == user.id, UserAgentConsent.consent_no == consent_no)
            .with_for_update()
        )
        if consent is None:
            raise _not_found()
        transitions = {
            "pause": ({"active"}, "paused"),
            "resume": ({"paused"}, "active"),
            "revoke": ({"active", "paused"}, "revoked"),
        }
        allowed, target = transitions[command]
        if consent.consent_status not in allowed:
            raise ApplicationError(
                status=409,
                code="AI_CONSENT_STATE_CONFLICT",
                title="Consent state conflict",
                detail="当前授权状态不允许该操作。",
            )
        consent.consent_status = target
        consent.revoked_at = utc_now() if target == "revoked" else None
        consent.version += 1
        await self.session.commit()
        return _consent_view(consent)


_CONTEXT_TYPES = frozenset(
    {"product", "order", "checkout_store_group", "shipment", "refund", "store"}
)


def _normalize_context_snapshot(
    values: Sequence[Mapping[str, Any]],
) -> list[dict[str, object]]:
    if len(values) > 8:
        raise ApplicationError(
            status=422,
            code="AGENT_CONTEXT_SNAPSHOT_INVALID",
            title="Invalid agent context",
            detail="会话上下文数量超出安全限制。",
        )
    result: list[dict[str, object]] = []
    seen_types: set[str] = set()
    for value in values:
        context_id = value.get("context_id")
        context_type = value.get("context_type")
        context_version = value.get("context_version")
        resource_id = value.get("resource_id")
        resource_version = value.get("resource_version")
        expires_at = value.get("expires_at")
        if (
            not isinstance(context_id, str)
            or not context_id.startswith("ctx_")
            or context_type not in _CONTEXT_TYPES
            or context_type in seen_types
            or not isinstance(context_version, int)
            or isinstance(context_version, bool)
            or context_version < 1
            or not isinstance(resource_id, str)
            or not resource_id
            or (resource_version is not None and not isinstance(resource_version, int))
            or (expires_at is not None and not isinstance(expires_at, str))
        ):
            raise ApplicationError(
                status=422,
                code="AGENT_CONTEXT_SNAPSHOT_INVALID",
                title="Invalid agent context",
                detail="会话上下文快照无效。",
            )
        seen_types.add(str(context_type))
        result.append(
            {
                "context_id": context_id,
                "context_type": str(context_type),
                "context_version": context_version,
                "resource_id": resource_id,
                "resource_version": resource_version,
                "expires_at": expires_at,
            }
        )
    return result


def _view(run: AgentRun, conversation: Conversation) -> AgentRunView:
    return AgentRunView(
        run_id=run.run_no,
        conversation_id=conversation.conversation_no,
        status=cast(AgentRunStatus, run.run_status),
        current_phase=run.current_phase,
        output=run.public_output,
        error_code=run.error_code,
        degraded_reason=run.degraded_reason,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _consent_view(consent: UserAgentConsent) -> AgentConsentView:
    return AgentConsentView(
        consent_id=consent.consent_no,
        consent_type=consent.consent_type,
        scope_type=consent.scope_type,
        scope_id=consent.scope_no,
        policy_version=consent.policy_version,
        status=cast(AgentConsentStatus, consent.consent_status),
        expires_at=consent.expires_at,
        revoked_at=consent.revoked_at,
        created_at=consent.created_at,
        version=consent.version,
    )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="Agent Run 不存在。",
    )
