from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, canonical_request_hash, utc_now
from app.modules.identity.models import User
from app.modules.rbac.models import AdminApprovalEvent, AdminApprovalRequest
from app.modules.rbac.repository import RbacRepository


@dataclass(frozen=True)
class ApprovalExecutionResult:
    resource_type: str
    resource_no: str


ApprovalHandler = Callable[
    [dict[str, object], AdminApprovalRequest],
    Awaitable[ApprovalExecutionResult],
]


class AdminApprovalExecutor:
    """Executes only registered domain commands after revalidating approval facts.

    A handler is registered by a domain module; there is deliberately no generic
    database mutation or arbitrary command execution fallback.
    """

    def __init__(
        self,
        session: AsyncSession,
        security: SecurityService,
        handlers: dict[str, ApprovalHandler],
    ) -> None:
        self.session = session
        self.security = security
        self.handlers = handlers
        self.repository = RbacRepository(session)

    async def execute(self, approval_request_no: str, execution_no: str) -> None:
        item = await self.repository.approval_by_no(approval_request_no, for_update=True)
        if item is None or item.execution_no != execution_no:
            return
        if item.request_status in {"succeeded", "failed", "outcome_unknown"}:
            return
        if item.request_status != "approved" or item.expires_at <= utc_now():
            await self._fail(item, "APPROVAL_NOT_EXECUTABLE")
            return
        decisions = await self.repository.approval_decisions(item.id)
        approver_ids = {
            decision.approver_user_id for decision in decisions if decision.decision == "approve"
        }
        if (
            len(approver_ids) < item.required_approval_count
            or item.initiator_user_id in approver_ids
        ):
            await self._fail(item, "APPROVAL_SEPARATION_OF_DUTIES_INVALID")
            return
        if not await self._participants_remain_authorized(item, approver_ids):
            await self._fail(item, "APPROVAL_AUTHORIZATION_CHANGED")
            return
        try:
            raw_payload = self.security.decrypt(
                "admin-approval-command",
                item.command_payload_ciphertext,
            )
            payload = json.loads(raw_payload)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            await self._fail(item, "APPROVAL_COMMAND_INVALID")
            return
        if not isinstance(payload, dict) or not hmac.compare_digest(
            item.command_arguments_hash,
            canonical_request_hash(payload),
        ):
            await self._fail(item, "APPROVAL_COMMAND_HASH_MISMATCH")
            return
        handler = self.handlers.get(item.action_code)
        if handler is None:
            await self._fail(item, "APPROVAL_HANDLER_NOT_REGISTERED")
            return

        previous = item.request_status
        item.request_status = "executing"
        item.execution_started_at = utc_now()
        item.version += 1
        self._add_event(item, "execution_started", previous, "executing")
        await self.session.commit()

        try:
            result = await handler(payload, item)
        except ApplicationError as exc:
            await self.session.rollback()
            current = await self.repository.approval_by_no(
                approval_request_no,
                for_update=True,
            )
            if current is not None and current.request_status == "executing":
                await self._fail(current, exc.code)
            return
        except Exception:
            await self.session.rollback()
            current = await self.repository.approval_by_no(
                approval_request_no,
                for_update=True,
            )
            if current is not None and current.request_status == "executing":
                current.request_status = "outcome_unknown"
                current.error_code = "APPROVAL_HANDLER_OUTCOME_UNKNOWN"
                current.completed_at = utc_now()
                current.version += 1
                self._add_event(
                    current,
                    "execution_outcome_unknown",
                    "executing",
                    "outcome_unknown",
                )
                await self.session.commit()
            return

        current = await self.repository.approval_by_no(approval_request_no, for_update=True)
        if current is None or current.request_status != "executing":
            return
        current.request_status = "succeeded"
        current.result_resource_type = result.resource_type
        current.result_resource_no = result.resource_no
        current.completed_at = utc_now()
        current.version += 1
        self._add_event(current, "execution_succeeded", "executing", "succeeded")
        await self.session.commit()

    async def _participants_remain_authorized(
        self,
        item: AdminApprovalRequest,
        approver_ids: set[int],
    ) -> bool:
        participant_ids = approver_ids | {item.initiator_user_id}
        now = utc_now()
        decisions = await self.repository.approval_decisions(item.id)
        decision_by_user = {decision.approver_user_id: decision for decision in decisions}
        recent_cutoff = timedelta(seconds=self.security.settings.admin_recent_auth_seconds)
        for user_id in participant_ids:
            user = await self.session.get(User, user_id)
            if user is None or user.user_status != "active":
                return False
            rows = await self.repository.permissions_for_user(user_id, now)
            domain_authorized = any(
                permission.permission_code == item.required_permission_code
                and (
                    (grant.scope_type, grant.scope_id) == ("platform", 0)
                    or (grant.scope_type, grant.scope_id) == (item.scope_type, item.scope_id)
                )
                for permission, grant, _ in rows
            )
            if not domain_authorized:
                return False
            if user_id in approver_ids:
                approval_authorized = any(
                    permission.permission_code == "admin_approvals:decide"
                    and (
                        (grant.scope_type, grant.scope_id) == ("platform", 0)
                        or (grant.scope_type, grant.scope_id) == (item.scope_type, item.scope_id)
                    )
                    for permission, grant, _ in rows
                )
                decision = decision_by_user.get(user_id)
                if (
                    not approval_authorized
                    or decision is None
                    or decision.assurance_level not in {"aal2", "aal3"}
                    or decision.authenticated_at < decision.decided_at - recent_cutoff
                ):
                    return False
        policy = item.approval_policy_snapshot
        authenticated_at_raw = policy.get("initiator_authenticated_at")
        if policy.get("initiator_assurance_level") not in {"aal2", "aal3"} or not isinstance(
            authenticated_at_raw, str
        ):
            return False
        try:
            initiator_authenticated_at = datetime.fromisoformat(authenticated_at_raw)
        except ValueError:
            return False
        if initiator_authenticated_at < item.created_at - recent_cutoff:
            return False
        return True

    async def _fail(self, item: AdminApprovalRequest, error_code: str) -> None:
        previous = item.request_status
        item.request_status = "failed"
        item.error_code = error_code
        item.completed_at = utc_now()
        item.version += 1
        self._add_event(item, "execution_failed", previous, "failed")
        await self.session.commit()

    def _add_event(
        self,
        item: AdminApprovalRequest,
        event_type: str,
        from_status: str,
        to_status: str,
    ) -> None:
        self.session.add(
            AdminApprovalEvent(
                event_no=new_prefixed_ulid("aae_"),
                approval_request_id=item.id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                actor_type="worker",
                actor_id=None,
                snapshot_redacted={"error_code": item.error_code},
                request_version=item.version,
                request_id=None,
                trace_id=item.trace_id,
            )
        )
