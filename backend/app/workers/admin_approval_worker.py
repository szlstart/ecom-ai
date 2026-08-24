from __future__ import annotations

import asyncio
import signal
from datetime import timedelta
from typing import cast

import structlog
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthContext
from app.core.config import Settings, get_settings
from app.core.exceptions import ApplicationError
from app.core.logging import configure_logging
from app.core.security import SecurityService, TokenClaims, utc_now
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.modules.after_sale.schemas import (
    AdminRefundAppealDecisionRequest,
    AdminRefundDecisionRequest,
)
from app.modules.after_sale.service import AfterSaleService
from app.modules.catalog import models as catalog_models  # noqa: F401
from app.modules.checkout import models as checkout_models  # noqa: F401
from app.modules.identity.models import AuthSession, User
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.models import AdminApprovalRequest, Permission
from app.modules.stores import models as store_models  # noqa: F401
from app.modules.system.models import OutboxEvent
from app.workers.admin_approval_executor import (
    AdminApprovalExecutor,
    ApprovalExecutionResult,
)

logger = structlog.get_logger(__name__)
_DICT = TypeAdapter(dict[str, object])


class AdminApprovalWorker:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        security: SecurityService,
    ) -> None:
        self.session = session
        self.settings = settings
        self.security = security
        self.executor = AdminApprovalExecutor(
            session,
            security,
            {
                "after_sale.refund.decide.v1": self._execute_refund_decision,
                "after_sale.refund_appeal.decide.v1": self._execute_appeal_decision,
            },
        )

    async def process_one(self) -> bool:
        now = utc_now()
        event = await self.session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type == "rbac.admin_approval_ready.v1",
                OutboxEvent.event_status == "pending",
                OutboxEvent.available_at <= now,
            )
            .order_by(OutboxEvent.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if event is None:
            return False
        event.attempt_count += 1
        event.available_at = now + timedelta(seconds=30)
        payload = _DICT.validate_python(event.payload)
        approval_no = payload.get("approval_request_id")
        execution_no = payload.get("execution_id")
        event_no = event.event_no
        await self.session.commit()
        if not isinstance(approval_no, str) or not isinstance(execution_no, str):
            await self._finish_event(event_no, error_code="APPROVAL_EVENT_INVALID")
            return True
        try:
            await self.executor.execute(approval_no, execution_no)
        except Exception:
            await self.session.rollback()
            await self._retry_event(event_no, "APPROVAL_EXECUTOR_UNAVAILABLE")
            logger.exception("admin_approval_execution_failed", approval_request_id=approval_no)
            return True
        await self._finish_event(event_no)
        return True

    async def _execute_refund_decision(
        self,
        raw_payload: dict[str, object],
        approval: AdminApprovalRequest,
    ) -> ApprovalExecutionResult:
        refund_no = raw_payload.get("refund_id")
        expected_version = raw_payload.get("expected_version")
        decision_raw = raw_payload.get("decision")
        if not isinstance(refund_no, str) or not isinstance(expected_version, int):
            raise _invalid_command()
        try:
            decision = AdminRefundDecisionRequest.model_validate(decision_raw)
        except ValidationError as exc:
            raise _invalid_command() from exc
        access = await self._initiator_access(approval)
        result = await AfterSaleService(
            self.session,
            self.settings,
            self.security,
        ).decide(
            access,
            refund_no,
            decision,
            expected_version,
            approval.execution_no or "missing-execution-no",
        )
        return ApprovalExecutionResult(
            resource_type="refund_application",
            resource_no=result.refund_id,
        )

    async def _execute_appeal_decision(
        self,
        raw_payload: dict[str, object],
        approval: AdminApprovalRequest,
    ) -> ApprovalExecutionResult:
        appeal_no = raw_payload.get("appeal_id")
        expected_version = raw_payload.get("expected_version")
        decision_raw = raw_payload.get("decision")
        if not isinstance(appeal_no, str) or not isinstance(expected_version, int):
            raise _invalid_command()
        try:
            decision = AdminRefundAppealDecisionRequest.model_validate(decision_raw)
        except ValidationError as exc:
            raise _invalid_command() from exc
        access = await self._initiator_access(approval)
        result = await AfterSaleService(
            self.session,
            self.settings,
            self.security,
        ).admin_decide_appeal(
            access,
            appeal_no,
            decision,
            expected_version,
            approval.execution_no or "missing-execution-no",
        )
        return ApprovalExecutionResult(
            resource_type="refund_appeal",
            resource_no=result.appeal_id,
        )

    async def _initiator_access(self, approval: AdminApprovalRequest) -> AdminAccess:
        now = utc_now()
        user = await self.session.get(User, approval.initiator_user_id)
        permission = cast(
            Permission | None,
            await self.session.scalar(
                select(Permission).where(
                    Permission.permission_code == approval.required_permission_code,
                    Permission.permission_status == "active",
                )
            ),
        )
        auth_session = cast(
            AuthSession | None,
            await self.session.scalar(
                select(AuthSession)
                .where(
                    AuthSession.user_id == approval.initiator_user_id,
                    AuthSession.audience == "admin",
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                )
                .order_by(AuthSession.authenticated_at.desc(), AuthSession.id.desc())
                .limit(1)
            ),
        )
        if user is None or permission is None or auth_session is None:
            raise ApplicationError(
                status=409,
                code="APPROVAL_INITIATOR_CONTEXT_UNAVAILABLE",
                title="Approval initiator unavailable",
                detail="审批发起人的执行身份已经失效。",
            )
        claims = TokenClaims(
            subject=user.user_no,
            session_id=auth_session.session_no,
            audience="admin",
            permission_version=user.permission_version,
            expires_at=auth_session.expires_at,
        )
        return AdminAccess(
            context=AuthContext(user=user, session=auth_session, claims=claims),
            permission=permission,
            scopes=((approval.scope_type, approval.scope_id),),
        )

    async def _finish_event(self, event_no: str, error_code: str | None = None) -> None:
        event = await self.session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_no == event_no).with_for_update()
        )
        if event is None:
            return
        event.event_status = "published" if error_code is None else "failed"
        event.last_error_code = error_code
        event.published_at = utc_now()
        await self.session.commit()

    async def _retry_event(self, event_no: str, error_code: str) -> None:
        event = await self.session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_no == event_no).with_for_update()
        )
        if event is None:
            return
        if event.attempt_count >= 5:
            event.event_status = "failed"
            event.published_at = utc_now()
        else:
            event.event_status = "pending"
            event.available_at = utc_now() + timedelta(seconds=min(60, 2**event.attempt_count))
        event.last_error_code = error_code
        await self.session.commit()


def _invalid_command() -> ApplicationError:
    return ApplicationError(
        status=409,
        code="APPROVAL_COMMAND_INVALID",
        title="Approval command invalid",
        detail="审批命令参数不完整或版本不兼容。",
    )


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_mysql(settings.mysql_dsn)
    security = SecurityService(settings)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)
    logger.info("admin_approval_worker_started")
    try:
        while not stopping.is_set():
            processed = False
            try:
                async for session in mysql_session():
                    processed = await AdminApprovalWorker(
                        session,
                        settings,
                        security,
                    ).process_one()
            except Exception:
                logger.exception("admin_approval_worker_batch_failed")
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    stopping.wait(),
                    timeout=settings.admin_approval_worker_poll_seconds,
                )
            except TimeoutError:
                pass
    finally:
        await close_mysql()
        logger.info("admin_approval_worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
