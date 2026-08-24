from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import SecurityService, canonical_request_hash, utc_now
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.models import AdminApprovalEvent, AdminApprovalRequest
from app.modules.rbac.repository import RbacRepository
from app.modules.rbac.schemas import ApprovalRequiredView


@dataclass(frozen=True)
class ApprovalRequestSpec:
    approval_type: str
    action_code: str
    target_type: str
    target_no: str
    scope_type: str
    scope_id: int
    command_payload: dict[str, object]
    display_snapshot: dict[str, object]
    resource_versions: dict[str, object]
    policy_snapshot: dict[str, object]
    required_approval_count: int
    reason: str


class AdminApprovalRequestService:
    """Creates executable approval resources only from trusted domain commands."""

    def __init__(self, session: AsyncSession, security: SecurityService) -> None:
        self.session = session
        self.security = security
        self.repository = RbacRepository(session)
        self.idempotency = IdempotencyService(session)

    async def create(
        self,
        access: AdminAccess,
        spec: ApprovalRequestSpec,
        *,
        idempotency_key: str,
        ttl_minutes: int = 30,
    ) -> ApprovalRequiredView:
        claim = await self.idempotency.begin(
            scope_key=(
                f"admin:approval-request:{spec.action_code}:"
                f"{spec.target_no}:{access.context.user.user_no}"
            ),
            idempotency_key=idempotency_key,
            payload={
                "command_payload": spec.command_payload,
                "resource_versions": spec.resource_versions,
            },
            resource_type="admin_approval",
        )
        if claim.replayed:
            item = (
                await self.repository.approval_by_no(claim.record.resource_no)
                if claim.record.resource_no is not None
                else None
            )
            if item is None:
                raise RuntimeError("idempotent approval resource is missing")
            return self._view(item)

        now = utc_now()
        trace_id = request_id_context.get() or new_prefixed_ulid("req_")
        item = AdminApprovalRequest(
            approval_request_no=new_prefixed_ulid("aar_"),
            approval_type=spec.approval_type,
            action_code=spec.action_code,
            initiator_user_id=access.context.user.id,
            scope_type=spec.scope_type,
            scope_id=spec.scope_id,
            required_permission_code=access.permission.permission_code,
            target_type=spec.target_type,
            target_no=spec.target_no,
            command_schema_version=1,
            command_payload_ciphertext=self.security.encrypt(
                "admin-approval-command",
                json.dumps(
                    spec.command_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
            command_arguments_hash=canonical_request_hash(spec.command_payload),
            display_snapshot=spec.display_snapshot,
            resource_versions=spec.resource_versions,
            approval_policy_snapshot=spec.policy_snapshot,
            required_approval_count=spec.required_approval_count,
            approved_count=0,
            request_status="pending",
            idempotency_key=idempotency_key,
            expires_at=now + timedelta(minutes=ttl_minutes),
            trace_id=trace_id,
            key_version=1,
        )
        self.session.add(item)
        await self.session.flush()
        self.session.add(
            AdminApprovalEvent(
                event_no=new_prefixed_ulid("aae_"),
                approval_request_id=item.id,
                event_type="request_created",
                from_status=None,
                to_status="pending",
                actor_type="admin",
                actor_id=access.context.user.id,
                snapshot_redacted=spec.display_snapshot,
                request_version=item.version,
                request_id=trace_id,
                trace_id=trace_id,
            )
        )
        record_admin_operation(
            self.session,
            access,
            action="approval.request",
            target_type=spec.target_type,
            target_no=spec.target_no,
            reason=spec.reason,
            after={
                "approval_request_id": item.approval_request_no,
                "action_code": spec.action_code,
                "required_approval_count": spec.required_approval_count,
            },
            scope_type=spec.scope_type,
            scope_id=spec.scope_id,
        )
        result = self._view(item)
        self.idempotency.complete(
            claim,
            response_status=202,
            resource_no=item.approval_request_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    @staticmethod
    def _view(item: AdminApprovalRequest) -> ApprovalRequiredView:
        return ApprovalRequiredView(
            command_status="approval_required",
            approval_request_id=item.approval_request_no,
            required_approval_count=item.required_approval_count,
            approved_count=item.approved_count,
            expires_at=item.expires_at,
        )
