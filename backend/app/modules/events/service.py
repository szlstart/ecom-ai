from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, canonical_request_hash, utc_now
from app.modules.events.repository import DeadLetterRepository
from app.modules.events.schemas import (
    DeadLetterIgnoreRequest,
    DeadLetterList,
    DeadLetterReplayPreview,
    DeadLetterReplayRequest,
    DeadLetterView,
)
from app.modules.rbac.approval_service import AdminApprovalRequestService, ApprovalRequestSpec
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.models import AdminApprovalRequest
from app.modules.rbac.schemas import ApprovalRequiredView
from app.modules.system.models import DeadLetterEvent, OutboxEvent


class DeadLetterService:
    def __init__(self, session: AsyncSession, security: SecurityService) -> None:
        self.session = session
        self.security = security
        self.repository = DeadLetterRepository(session)
        self.approvals = AdminApprovalRequestService(session, security)

    async def list(
        self,
        access: AdminAccess,
        *,
        status: str | None,
        event_type: str | None,
        limit: int,
    ) -> DeadLetterList:
        rows = await self.repository.list(
            scopes=access.scopes,
            status=status,
            event_type=event_type,
            limit=limit,
        )
        return DeadLetterList(items=[_view(item) for item in rows])

    async def detail(self, access: AdminAccess, dead_letter_no: str) -> DeadLetterView:
        item = await self.repository.by_no(dead_letter_no)
        if item is None:
            raise _not_found()
        access.require_scope(item.scope_type, item.scope_id)
        return _view(item)

    async def preview(self, access: AdminAccess, dead_letter_no: str) -> DeadLetterReplayPreview:
        item = await self.repository.by_no(dead_letter_no)
        if item is None:
            raise _not_found()
        access.require_scope(item.scope_type, item.scope_id)
        source = (
            await self.repository.outbox_by_no(item.source_no)
            if item.source_type == "outbox"
            else None
        )
        blockers = _replay_blockers(item, source)
        expires_at = utc_now() + timedelta(minutes=10)
        token_payload = {
            "dead_letter_id": item.dead_letter_no,
            "dead_letter_version": item.version,
            "source_id": item.source_no,
            "payload_hash": item.payload_hash.hex(),
            "replayable": not blockers,
            "expires_at": expires_at.isoformat(),
        }
        token = base64.urlsafe_b64encode(
            self.security.encrypt(
                "dead-letter-preview",
                json.dumps(token_payload, separators=(",", ":"), sort_keys=True),
            )
        ).decode()
        record_admin_operation(
            self.session,
            access,
            action="preview_dead_letter_replay",
            target_type="dead_letter_event",
            target_no=item.dead_letter_no,
            after={
                "replayable": not blockers,
                "blockers": blockers,
                "payload_hash": item.payload_hash.hex(),
                "expires_at": expires_at.isoformat(),
            },
            scope_type=item.scope_type,
            scope_id=item.scope_id,
        )
        await self.session.commit()
        return DeadLetterReplayPreview(
            dead_letter=_view(item),
            replayable=not blockers,
            blockers=blockers,
            source_status=source.event_status if source else None,
            immutable_payload_hash=item.payload_hash.hex(),
            impact_summary=[
                "恢复原 Outbox 事件为待处理状态，不创建或编辑业务 Payload。",
                "消费者仍须执行自身幂等、权限和资源状态校验。",
                "执行需要两名不同管理员批准，发起人不能自批。",
            ],
            required_approval_count=2,
            preview_token=token,
            expires_at=expires_at,
        )

    async def request_replay(
        self,
        access: AdminAccess,
        dead_letter_no: str,
        payload: DeadLetterReplayRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> ApprovalRequiredView:
        item = await self.repository.by_no(dead_letter_no)
        if item is None:
            raise _not_found()
        access.require_scope(item.scope_type, item.scope_id)
        preview = self._decode_preview(payload.preview_token)
        if (
            preview.get("dead_letter_id") != item.dead_letter_no
            or preview.get("dead_letter_version") != expected_version
            or preview.get("source_id") != item.source_no
            or preview.get("payload_hash") != item.payload_hash.hex()
            or preview.get("replayable") is not True
        ):
            raise _conflict("DEAD_LETTER_PREVIEW_STALE", "重放预览与当前死信不一致，请重新预览。")
        expires_at = preview.get("expires_at")
        try:
            expired = (
                not isinstance(expires_at, str) or datetime.fromisoformat(expires_at) <= utc_now()
            )
        except ValueError as exc:
            raise _conflict("DEAD_LETTER_PREVIEW_INVALID", "重放预览无效。") from exc
        if expired:
            raise _conflict("DEAD_LETTER_PREVIEW_EXPIRED", "重放预览已过期，请重新预览。")
        source = await self.repository.outbox_by_no(item.source_no)
        blockers = _replay_blockers(item, source)
        if blockers:
            raise _conflict("DEAD_LETTER_NOT_REPLAYABLE", "当前死信不允许重放，请重新预览。")
        return await self.approvals.create(
            access,
            ApprovalRequestSpec(
                approval_type="dead_letter_replay",
                action_code="events.dead_letter.replay.v1",
                target_type="dead_letter_event",
                target_no=item.dead_letter_no,
                scope_type=item.scope_type,
                scope_id=item.scope_id,
                command_payload={
                    "dead_letter_id": item.dead_letter_no,
                    "expected_version": expected_version,
                    "source_id": item.source_no,
                    "payload_hash": item.payload_hash.hex(),
                    "reason_code": payload.reason_code,
                    "reason": payload.reason,
                },
                display_snapshot={
                    "event_type": item.event_type,
                    "source_type": item.source_type,
                    "source_id": item.source_no,
                    "payload_hash": item.payload_hash.hex(),
                    "failure_count": item.failure_count,
                    "reason_code": payload.reason_code,
                },
                resource_versions={
                    "dead_letter": item.version,
                    "source_aggregate": source.aggregate_version if source else None,
                },
                policy_snapshot={
                    "policy": "dual_control",
                    "required_approval_count": 2,
                    "immutable_payload": True,
                },
                required_approval_count=2,
                reason=payload.reason,
            ),
            idempotency_key=idempotency_key,
        )

    async def execute_replay(
        self,
        access: AdminAccess,
        raw_payload: dict[str, object],
        approval: AdminApprovalRequest,
    ) -> DeadLetterEvent:
        dead_letter_no = raw_payload.get("dead_letter_id")
        expected_version = raw_payload.get("expected_version")
        source_no = raw_payload.get("source_id")
        payload_hash = raw_payload.get("payload_hash")
        reason = raw_payload.get("reason")
        if (
            not isinstance(dead_letter_no, str)
            or not isinstance(expected_version, int)
            or not isinstance(source_no, str)
            or not isinstance(payload_hash, str)
            or not isinstance(reason, str)
        ):
            raise _conflict("APPROVAL_COMMAND_INVALID", "死信重放命令不完整。")
        item = await self.repository.by_no(dead_letter_no, for_update=True)
        source = await self.repository.outbox_by_no(source_no, for_update=True)
        if item is None or source is None:
            raise _not_found()
        access.require_scope(item.scope_type, item.scope_id)
        if (
            approval.resource_versions
            != {
                "dead_letter": item.version,
                "source_aggregate": source.aggregate_version,
            }
            or item.version != expected_version
        ):
            raise _conflict("DEAD_LETTER_VERSION_CONFLICT", "死信或原事件已变化。")
        if payload_hash != item.payload_hash.hex() or _replay_blockers(item, source):
            raise _conflict("DEAD_LETTER_NOT_REPLAYABLE", "死信已不满足安全重放条件。")
        now = utc_now()
        replay_trace = request_id_context.get() or new_prefixed_ulid("req_")
        source.event_status = "pending"
        source.available_at = now
        source.published_at = None
        source.attempt_count = 0
        source.last_error_code = None
        item.dead_status = "replaying"
        item.replay_count += 1
        item.last_replay_at = now
        item.replay_trace_id = replay_trace
        item.version += 1
        record_admin_operation(
            self.session,
            access,
            action="replay_dead_letter",
            target_type="dead_letter_event",
            target_no=item.dead_letter_no,
            reason=reason,
            before={"status": "open", "version": expected_version},
            after={
                "status": "replaying",
                "version": item.version,
                "source_status": "pending",
                "replay_count": item.replay_count,
            },
            scope_type=item.scope_type,
            scope_id=item.scope_id,
        )
        await self.session.flush()
        return item

    async def ignore(
        self,
        access: AdminAccess,
        dead_letter_no: str,
        payload: DeadLetterIgnoreRequest,
        expected_version: int,
    ) -> DeadLetterView:
        item = await self.repository.by_no(dead_letter_no, for_update=True)
        if item is None:
            raise _not_found()
        access.require_scope(item.scope_type, item.scope_id)
        if item.version != expected_version:
            raise _conflict("DEAD_LETTER_VERSION_CONFLICT", "死信版本已变化，请刷新后重试。")
        if item.dead_status != "open":
            raise _conflict("DEAD_LETTER_NOT_OPEN", "只有待处理死信可以标记为忽略。")
        now = utc_now()
        before_version = item.version
        item.dead_status = "ignored"
        item.resolved_by = access.context.user.id
        item.resolved_at = now
        item.resolution_note = f"{payload.reason_code}: {payload.reason}"[:1000]
        item.version += 1
        record_admin_operation(
            self.session,
            access,
            action="ignore_dead_letter",
            target_type="dead_letter_event",
            target_no=item.dead_letter_no,
            reason=payload.reason,
            before={"status": "open", "version": before_version},
            after={
                "status": "ignored",
                "version": item.version,
                "reason_code": payload.reason_code,
            },
            scope_type=item.scope_type,
            scope_id=item.scope_id,
        )
        await self.session.commit()
        return _view(item)

    def _decode_preview(self, value: str) -> dict[str, object]:
        try:
            ciphertext = base64.urlsafe_b64decode(value.encode())
            raw = self.security.decrypt("dead-letter-preview", ciphertext)
            decoded = json.loads(raw)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _conflict("DEAD_LETTER_PREVIEW_INVALID", "重放预览无效。") from exc
        if not isinstance(decoded, dict):
            raise _conflict("DEAD_LETTER_PREVIEW_INVALID", "重放预览无效。")
        return cast(dict[str, object], decoded)


def _replay_blockers(item: DeadLetterEvent, source: OutboxEvent | None) -> list[str]:
    blockers: list[str] = []
    if item.dead_status != "open":
        blockers.append("DEAD_LETTER_NOT_OPEN")
    if item.schema_version != 1:
        blockers.append("SCHEMA_VERSION_UNSUPPORTED")
    if item.source_type != "outbox" or source is None:
        blockers.append("SOURCE_REPLAY_ADAPTER_UNAVAILABLE")
        return blockers
    if source.event_status != "failed":
        blockers.append("SOURCE_STATUS_CHANGED")
    if source.event_type != item.event_type:
        blockers.append("EVENT_TYPE_MISMATCH")
    if canonical_request_hash(source.payload) != item.payload_hash:
        blockers.append("PAYLOAD_HASH_MISMATCH")
    return blockers


def _view(item: DeadLetterEvent) -> DeadLetterView:
    return DeadLetterView(
        dead_letter_id=item.dead_letter_no,
        source_type=item.source_type,
        source_id=item.source_no,
        event_type=item.event_type,
        schema_version=item.schema_version,
        scope_type=item.scope_type,
        scope_id=item.scope_id,
        payload_hash=item.payload_hash.hex(),
        payload_keys=sorted((item.payload_redacted or {}).keys()),
        failure_count=item.failure_count,
        first_failed_at=item.first_failed_at,
        last_failed_at=item.last_failed_at,
        last_error_code=item.last_error_code,
        last_error=item.last_error,
        status=cast(Literal["open", "replaying", "resolved", "ignored"], item.dead_status),
        replay_count=item.replay_count,
        last_replay_at=item.last_replay_at,
        original_trace_id=item.original_trace_id,
        replay_trace_id=item.replay_trace_id,
        available_actions=cast(
            list[Literal["preview_replay", "ignore"]],
            ["preview_replay", "ignore"] if item.dead_status == "open" else [],
        ),
        version=item.version,
    )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="未找到该死信事件。",
    )


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Dead letter conflict", detail=detail)
