from __future__ import annotations

import base64
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PaginationMeta
from app.core.config import Settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.pagination import CursorCodec
from app.core.security import SecurityService, utc_now
from app.modules.after_sale.models import (
    RefundAppeal,
    RefundAppealEvent,
    RefundApplication,
    RefundEvent,
    RefundItem,
    RefundPaymentRecord,
    RefundShipment,
)
from app.modules.after_sale.repository import AfterSaleRepository
from app.modules.after_sale.schemas import (
    AdminRefundAppealDecisionRequest,
    AdminRefundAppealDecisionResult,
    AdminRefundAppealList,
    AdminRefundDecisionRequest,
    AdminRefundDecisionResult,
    AdminRefundList,
    FakeRefundWebhook,
    RefundAppealCreateRequest,
    RefundAppealEventList,
    RefundAppealEventView,
    RefundAppealView,
    RefundApplicationCreateRequest,
    RefundApplicationItemView,
    RefundApplicationList,
    RefundApplicationView,
    RefundEligibilityCheck,
    RefundEligibilityItem,
    RefundEligibilityRequest,
    RefundEventList,
    RefundEventView,
    RefundPaymentCallbackAck,
    RefundReturnShipmentRequest,
    RefundReturnShipmentView,
)
from app.modules.catalog.schemas import Money
from app.modules.identity.models import User
from app.modules.orders.models import Order, OrderItem, TradeOrder
from app.modules.payments.models import Payment
from app.modules.rbac.approval_service import AdminApprovalRequestService, ApprovalRequestSpec
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.schemas import ApprovalRequiredView
from app.modules.system.models import OutboxEvent


class AfterSaleService:
    def __init__(
        self, session: AsyncSession, settings: Settings, security: SecurityService
    ) -> None:
        self.session = session
        self.settings = settings
        self.security = security
        self.repository = AfterSaleRepository(session)
        self.idempotency = IdempotencyService(session)
        self.cursor = CursorCodec(settings.security_hmac_secret.get_secret_value())

    async def eligibility(
        self, user: User, payload: RefundEligibilityRequest
    ) -> RefundEligibilityCheck:
        order = await self.repository.order_for_update(user.id, payload.order_id)
        if order is None:
            raise _not_found()
        selected = {item.order_item_id: item.quantity for item in payload.items}
        items = await self.repository.order_items_for_update(order.id, list(selected))
        if len(items) != len(selected):
            raise _not_found()
        active = await self.repository.active_items([item.id for item in items])
        active_by_item: dict[int, tuple[int, int]] = {}
        for refund_item in active:
            active_quantity, active_amount = active_by_item.get(refund_item.order_item_id, (0, 0))
            active_by_item[refund_item.order_item_id] = (
                active_quantity + refund_item.quantity,
                active_amount + refund_item.requested_amount,
            )
        views: list[RefundEligibilityItem] = []
        blocking: list[str] = []
        total = 0
        for item in items:
            active_quantity, active_amount = active_by_item.get(item.id, (0, 0))
            available_qty = max(0, item.quantity - item.refunded_quantity - active_quantity)
            requested_qty = selected[item.order_item_no]
            if requested_qty <= 0 or requested_qty > available_qty:
                blocking.append("REFUND_ITEM_CAPACITY_CHANGED")
            amount = _next_refundable_amount(
                item,
                active_quantity=active_quantity,
                active_amount=active_amount,
                requested_quantity=min(requested_qty, available_qty),
            )
            total += amount
            views.append(
                RefundEligibilityItem(
                    order_item_id=item.order_item_no,
                    purchased_quantity=item.quantity,
                    succeeded_refund_quantity=item.refunded_quantity,
                    active_reserved_quantity=active_quantity,
                    available_quantity=available_qty,
                    available_refundable_amount=_money(
                        max(0, item.payable_amount - item.refunded_amount - active_amount),
                        item.currency,
                    ),
                    available_actions=(
                        (["view_active_after_sale"] if active_quantity else [])
                        + (["apply_after_sale"] if available_qty else [])
                    ),
                )
            )
        if order.payment_status not in {"paid", "partially_refunded"} or order.order_status in {
            "cancelled",
            "closed",
        }:
            blocking.append("ORDER_NOT_REFUNDABLE")
        now = utc_now()
        eligible = not blocking
        token = _eligibility_token(
            self.security,
            {
                "user": user.user_no,
                "order": order.order_no,
                "items": selected,
                "type": payload.requested_type,
                "reason": payload.reason_code,
                "exp": int((now + timedelta(minutes=10)).timestamp()),
            },
        )
        return RefundEligibilityCheck(
            eligible=eligible,
            eligibility_token=token if eligible else None,
            expires_at=now + timedelta(minutes=10),
            allowed_types=["refund_only", "return_and_refund"],
            items=views,
            min_refundable_amount=_money(total, order.currency),
            max_refundable_amount=_money(total, order.currency),
            suggested_refund_amount=_money(total, order.currency),
            blocking_reasons=sorted(set(blocking)),
        )

    async def create(
        self, user: User, payload: RefundApplicationCreateRequest, idempotency_key: str
    ) -> RefundApplicationView:
        claim = await self.idempotency.begin(
            scope_key=f"refund:create:{user.user_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="refund",
        )
        if claim.replayed and claim.record.response_body is not None:
            return RefundApplicationView.model_validate(claim.record.response_body)
        token_data = _read_eligibility_token(self.security, payload.eligibility_token)
        if token_data is None or token_data.get("user") != user.user_no:
            raise ApplicationError(
                status=409,
                code="REFUND_ELIGIBILITY_EXPIRED",
                title="Refund eligibility expired",
                detail="售后资格已失效，请重新检查。",
            )
        token_items = token_data.get("items")
        requested_items = {item.order_item_id: item.quantity for item in payload.items}
        if (
            token_data.get("type") != payload.refund_type
            or token_data.get("reason") != payload.reason_code
            or not isinstance(token_items, dict)
            or token_items != requested_items
        ):
            raise ApplicationError(
                status=409,
                code="REFUND_ELIGIBILITY_MISMATCH",
                title="Refund eligibility mismatch",
                detail="申请内容与售后资格检查不一致，请重新检查。",
            )
        order_item_no = payload.items[0].order_item_id
        order = await self.session.scalar(
            select(Order)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .where(Order.user_id == user.id, OrderItem.order_item_no == order_item_no)
            .with_for_update()
        )
        if order is None:
            raise _not_found()
        if token_data.get("order") != order.order_no:
            raise ApplicationError(
                status=409,
                code="REFUND_ELIGIBILITY_MISMATCH",
                title="Refund eligibility mismatch",
                detail="申请订单与售后资格检查不一致。",
            )
        item_nos = [item.order_item_id for item in payload.items]
        items = await self.repository.order_items_for_update(order.id, item_nos)
        if len(items) != len(item_nos):
            raise _not_found()
        active = await self.repository.active_items([item.id for item in items])
        active_by_item: dict[int, tuple[int, int]] = {}
        for refund_item in active:
            active_quantity, active_amount = active_by_item.get(refund_item.order_item_id, (0, 0))
            active_by_item[refund_item.order_item_id] = (
                active_quantity + refund_item.quantity,
                active_amount + refund_item.requested_amount,
            )
        amount = 0
        now = utc_now()
        refund = RefundApplication(
            refund_no=new_prefixed_ulid("ref_"),
            order_id=order.id,
            user_id=user.id,
            store_id=order.store_id,
            refund_type=payload.refund_type,
            refund_status="submitted",
            reason_code=payload.reason_code,
            reason_detail=payload.reason_detail,
            requested_amount=0,
            approved_amount=0,
            currency=order.currency,
            policy_snapshot={"version": "refund-policy-v1"},
            submitted_at=now,
        )
        self.session.add(refund)
        await self.session.flush()
        request_by_no = {item.order_item_id: item for item in payload.items}
        for item in items:
            request_item = request_by_no[item.order_item_no]
            active_quantity, active_amount = active_by_item.get(item.id, (0, 0))
            available = item.quantity - item.refunded_quantity - active_quantity
            if request_item.quantity > available:
                raise ApplicationError(
                    status=409,
                    code="REFUND_ITEM_CAPACITY_CHANGED",
                    title="Refund capacity changed",
                    detail="订单项可退款数量已变化，请刷新后重试。",
                )
            item_amount = _next_refundable_amount(
                item,
                active_quantity=active_quantity,
                active_amount=active_amount,
                requested_quantity=request_item.quantity,
            )
            amount += item_amount
            self.session.add(
                RefundItem(
                    refund_id=refund.id,
                    order_item_id=item.id,
                    quantity=request_item.quantity,
                    requested_amount=item_amount,
                    refund_status="active",
                )
            )
            item.after_sale_status = "in_progress"
            item.version += 1
        refund.requested_amount = amount
        refund.approved_amount = amount
        if (
            payload.requested_amount.currency != order.currency
            or int(payload.requested_amount.minor_units) != amount
        ):
            raise ApplicationError(
                status=409,
                code="REFUND_AMOUNT_CHANGED",
                title="Refund amount changed",
                detail="退款金额已变化，请重新检查售后资格。",
            )
        order.after_sale_status = "in_progress"
        order.version += 1
        self.session.add(
            RefundEvent(
                event_no=new_prefixed_ulid("rfe_"),
                refund_id=refund.id,
                from_status=None,
                to_status="submitted",
                event_code="refund.submitted",
                actor_type="user",
                actor_user_id=user.id,
                request_id=request_id_context.get(),
            )
        )
        await self.session.flush()
        result = await self._view(refund)
        self.idempotency.complete(
            claim,
            response_status=201,
            resource_no=refund.refund_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def list_mine(
        self, user: User, limit: int = 20
    ) -> tuple[RefundApplicationList, PaginationMeta]:
        rows = await self.repository.applications(user.id, limit)
        return RefundApplicationList(items=await self._views(rows)), PaginationMeta(
            limit=limit, has_next=len(rows) == limit
        )

    async def detail(self, user: User, refund_no: str) -> RefundApplicationView:
        refund = await self.repository.application(user.id, refund_no)
        if refund is None:
            raise _not_found()
        return await self._view(refund)

    async def events(self, user: User, refund_no: str) -> RefundEventList:
        refund = await self.repository.application(user.id, refund_no)
        if refund is None:
            raise _not_found()
        return RefundEventList(
            items=[
                RefundEventView(
                    event_id=e.event_no,
                    from_status=e.from_status,
                    to_status=e.to_status,
                    event_code=e.event_code,
                    occurred_at=e.created_at,
                )
                for e in await self.repository.events(refund.id)
            ]
        )

    async def cancel(
        self, user: User, refund_no: str, idempotency_key: str
    ) -> RefundApplicationView:
        claim = await self.idempotency.begin(
            scope_key=f"refund:cancel:{user.user_no}:{refund_no}",
            idempotency_key=idempotency_key,
            payload={},
            resource_type="refund",
        )
        if claim.replayed and claim.record.response_body is not None:
            return RefundApplicationView.model_validate(claim.record.response_body)
        refund = await self.repository.application(user.id, refund_no, for_update=True)
        if refund is None:
            raise _not_found()
        if refund.refund_status not in {"submitted", "merchant_review"}:
            raise ApplicationError(
                status=409,
                code="REFUND_CANCEL_NOT_ALLOWED",
                title="Refund cannot be cancelled",
                detail="当前售后状态不允许撤销。",
            )
        previous = refund.refund_status
        refund.refund_status = "cancelled"
        refund.version += 1
        for item, _ in await self.repository.items_for_refund(refund.id):
            item.refund_status = "released"
        await self.session.flush()
        await self._refresh_order_projection(refund.order_id)
        self.session.add(
            RefundEvent(
                event_no=new_prefixed_ulid("rfe_"),
                refund_id=refund.id,
                from_status=previous,
                to_status="cancelled",
                event_code="refund.cancelled",
                actor_type="user",
                actor_user_id=user.id,
                request_id=request_id_context.get(),
            )
        )
        await self.session.flush()
        result = await self._view(refund)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=refund.refund_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def admin_detail(self, access: AdminAccess, refund_no: str) -> RefundApplicationView:
        refund = await self.repository.admin_application(refund_no)
        if refund is None:
            raise _not_found()
        access.require_scope("store", refund.store_id)
        return await self._view(refund)

    async def claim_refund(
        self,
        access: AdminAccess,
        refund_no: str,
        expected_version: int,
        idempotency_key: str,
    ) -> RefundApplicationView:
        claim = await self.idempotency.begin(
            scope_key=f"admin:refund-claim:{refund_no}:{access.context.user.user_no}",
            idempotency_key=idempotency_key,
            payload={"expected_version": expected_version},
            resource_type="refund_application",
        )
        refund = await self.repository.admin_application(refund_no, for_update=True)
        if refund is None:
            raise _not_found()
        access.require_scope("store", refund.store_id)
        if claim.replayed:
            return await self._view(refund)
        if refund.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="退款申请已经变化，请刷新后重试。",
            )
        if refund.refund_status not in {"submitted", "merchant_review"} or refund.claimed_by:
            raise ApplicationError(
                status=409,
                code="REFUND_ALREADY_CLAIMED",
                title="Refund already claimed",
                detail="该退款申请已被其他审核员领取或不再可领取。",
            )
        refund.claimed_by = access.context.user.id
        refund.claimed_at = utc_now()
        refund.version += 1
        record_admin_operation(
            self.session,
            access,
            action="refund.claim",
            target_type="refund_application",
            target_no=refund.refund_no,
            after={"claimed_by": access.context.user.id},
            scope_type="store",
            scope_id=refund.store_id,
        )
        result = await self._view(refund)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=refund.refund_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def admin_list(
        self, access: AdminAccess, limit: int, cursor: str | None
    ) -> AdminRefundList:
        filter_key = json.dumps(
            {"scopes": access.scopes}, separators=(",", ":"), sort_keys=True
        )
        position = self.cursor.decode(cursor, filter_key=filter_key)
        rows, has_more = await self.repository.admin_applications(
            limit, access.scopes, position
        )
        return AdminRefundList(
            items=await self._views(rows),
            next_cursor=(
                self.cursor.encode(
                    filter_key=filter_key,
                    values=(rows[-1].created_at.isoformat(), str(rows[-1].id)),
                )
                if rows and has_more
                else None
            ),
        )

    async def admin_appeal_list(self, access: AdminAccess, limit: int) -> AdminRefundAppealList:
        if ("platform", 0) not in access.scopes:
            return AdminRefundAppealList(items=[])
        return AdminRefundAppealList(
            items=[
                _appeal_view(appeal, refund.refund_no)
                for appeal, refund in await self.repository.admin_appeals(limit)
            ]
        )

    async def admin_appeal_detail(self, access: AdminAccess, appeal_no: str) -> RefundAppealView:
        if ("platform", 0) not in access.scopes:
            raise _not_found()
        row = await self.repository.admin_appeal(appeal_no)
        if row is None:
            raise _not_found()
        return _appeal_view(row[0], row[1].refund_no)

    async def claim_appeal(
        self,
        access: AdminAccess,
        appeal_no: str,
        expected_version: int,
        idempotency_key: str,
    ) -> RefundAppealView:
        claim = await self.idempotency.begin(
            scope_key=f"admin:appeal-claim:{appeal_no}:{access.context.user.user_no}",
            idempotency_key=idempotency_key,
            payload={"expected_version": expected_version},
            resource_type="refund_appeal",
        )
        if ("platform", 0) not in access.scopes:
            raise _not_found()
        row = await self.repository.admin_appeal(appeal_no, for_update=True)
        if row is None:
            raise _not_found()
        appeal, refund = row
        if claim.replayed:
            return _appeal_view(appeal, refund.refund_no)
        if appeal.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="申诉已经变化，请刷新后重试。",
            )
        if appeal.appeal_status != "submitted" or appeal.claimed_by is not None:
            raise ApplicationError(
                status=409,
                code="APPEAL_ALREADY_CLAIMED",
                title="Appeal already claimed",
                detail="该申诉已被领取或不再可领取。",
            )
        appeal.claimed_by = access.context.user.id
        appeal.claimed_at = utc_now()
        previous = appeal.appeal_status
        appeal.appeal_status = "reviewing"
        appeal.version += 1
        self.session.add(
            RefundAppealEvent(
                event_no=new_prefixed_ulid("rae_"),
                appeal_id=appeal.id,
                event_type="appeal.claimed",
                from_status=previous,
                to_status=appeal.appeal_status,
                actor_type="admin",
                actor_id=access.context.user.id,
                reason_code="CLAIM",
                appeal_version=appeal.version,
                trace_id=request_id_context.get(),
            )
        )
        record_admin_operation(
            self.session,
            access,
            action="refund_appeal.claim",
            target_type="refund_appeal",
            target_no=appeal.appeal_no,
            after={"claimed_by": access.context.user.id},
            scope_type="platform",
            scope_id=0,
        )
        result = _appeal_view(appeal, refund.refund_no)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=appeal.appeal_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def request_appeal_decision(
        self,
        access: AdminAccess,
        appeal_no: str,
        payload: AdminRefundAppealDecisionRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminRefundAppealDecisionResult:
        if ("platform", 0) not in access.scopes:
            raise _not_found()
        row = await self.repository.admin_appeal(appeal_no, for_update=True)
        if row is None:
            raise _not_found()
        appeal, refund = row
        self._validate_appeal_decision(
            access,
            appeal,
            expected_version=expected_version,
        )
        approval = AdminApprovalRequestService(self.session, self.security)
        return await approval.create(
            access,
            ApprovalRequestSpec(
                approval_type="refund_exception",
                action_code="after_sale.refund_appeal.decide.v1",
                target_type="refund_appeal",
                target_no=appeal.appeal_no,
                scope_type="platform",
                scope_id=0,
                command_payload={
                    "appeal_id": appeal.appeal_no,
                    "expected_version": expected_version,
                    "decision": payload.model_dump(mode="json"),
                },
                display_snapshot={
                    "appeal_id": appeal.appeal_no,
                    "refund_id": refund.refund_no,
                    "decision": payload.decision,
                    "reason": payload.reason,
                    "impact": "平台申诉结论将在双人复核通过后执行",
                },
                resource_versions={"refund_appeal": expected_version},
                policy_snapshot={
                    "policy": "refund_appeal_dual_control_v1",
                    "required_approval_count": 2,
                    "initiator_cannot_approve": True,
                    "initiator_assurance_level": access.context.session.assurance_level,
                    "initiator_authenticated_at": (
                        access.context.session.authenticated_at.isoformat()
                    ),
                },
                required_approval_count=2,
                reason=payload.reason,
            ),
            idempotency_key=idempotency_key,
            ttl_minutes=self.settings.admin_approval_ttl_minutes,
        )

    async def admin_decide_appeal(
        self,
        access: AdminAccess,
        appeal_no: str,
        payload: AdminRefundAppealDecisionRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> RefundAppealView:
        claim = await self.idempotency.begin(
            scope_key=f"admin:appeal-decision:{appeal_no}:{access.context.user.user_no}",
            idempotency_key=idempotency_key,
            payload={
                "expected_version": expected_version,
                "command": payload.model_dump(mode="json"),
            },
            resource_type="refund_appeal",
        )
        if ("platform", 0) not in access.scopes:
            raise _not_found()
        row = await self.repository.admin_appeal(appeal_no, for_update=True)
        if row is None:
            raise _not_found()
        appeal, refund = row
        if claim.replayed:
            return _appeal_view(appeal, refund.refund_no)
        self._validate_appeal_decision(access, appeal, expected_version=expected_version)
        previous = appeal.appeal_status
        appeal.appeal_status = "upheld" if payload.decision == "approve" else "rejected"
        appeal.reviewed_by = access.context.user.id
        appeal.resolution_code = "UPHELD" if payload.decision == "approve" else "REJECTED"
        appeal.resolution_detail = payload.reason
        appeal.decided_at = utc_now()
        appeal.version += 1
        self.session.add(
            RefundAppealEvent(
                event_no=new_prefixed_ulid("rae_"),
                appeal_id=appeal.id,
                event_type="appeal.decided",
                from_status=previous,
                to_status=appeal.appeal_status,
                actor_type="admin",
                actor_id=access.context.user.id,
                reason_code=appeal.resolution_code,
                remark=payload.reason,
                appeal_version=appeal.version,
                trace_id=request_id_context.get(),
            )
        )
        record_admin_operation(
            self.session,
            access,
            action="refund_appeal.decide",
            target_type="refund_appeal",
            target_no=appeal.appeal_no,
            reason=payload.reason,
            before={"status": "submitted"},
            after={"status": appeal.appeal_status},
            scope_type="platform",
            scope_id=0,
        )
        result = _appeal_view(appeal, refund.refund_no)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=appeal.appeal_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def request_refund_decision(
        self,
        access: AdminAccess,
        refund_no: str,
        payload: AdminRefundDecisionRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminRefundDecisionResult:
        refund = await self.repository.admin_application(refund_no, for_update=True)
        if refund is None:
            raise _not_found()
        access.require_scope("store", refund.store_id)
        self._validate_refund_decision(access, refund, expected_version=expected_version)
        amount = (
            int(payload.approved_amount.minor_units)
            if payload.approved_amount is not None
            else refund.requested_amount
        )
        requires_approval = (
            payload.decision == "approve"
            and (
                refund.currency != "CNY"
                or amount >= self.settings.refund_dual_approval_threshold_minor
            )
        )
        if not requires_approval:
            return await self.decide(
                access,
                refund_no,
                payload,
                expected_version,
                idempotency_key,
            )
        approval = AdminApprovalRequestService(self.session, self.security)
        result: ApprovalRequiredView = await approval.create(
            access,
            ApprovalRequestSpec(
                approval_type="refund_exception",
                action_code="after_sale.refund.decide.v1",
                target_type="refund_application",
                target_no=refund.refund_no,
                scope_type="store",
                scope_id=refund.store_id,
                command_payload={
                    "refund_id": refund.refund_no,
                    "expected_version": expected_version,
                    "decision": payload.model_dump(mode="json"),
                },
                display_snapshot={
                    "refund_id": refund.refund_no,
                    "decision": payload.decision,
                    "amount": {"minor_units": str(amount), "currency": refund.currency},
                    "reason_code": payload.reason_code,
                    "reason": payload.reason,
                    "impact": "审批通过后执行原路退款或进入退货流程",
                },
                resource_versions={"refund_application": expected_version},
                policy_snapshot={
                    "policy": "refund_amount_dual_control_v1",
                    "threshold_minor": self.settings.refund_dual_approval_threshold_minor,
                    "threshold_currency": "CNY",
                    "non_configured_currency_fallback": "always_require_approval",
                    "required_approval_count": 2,
                    "initiator_cannot_approve": True,
                    "initiator_assurance_level": access.context.session.assurance_level,
                    "initiator_authenticated_at": (
                        access.context.session.authenticated_at.isoformat()
                    ),
                },
                required_approval_count=2,
                reason=payload.reason,
            ),
            idempotency_key=idempotency_key,
            ttl_minutes=self.settings.admin_approval_ttl_minutes,
        )
        return result

    async def decide(
        self,
        access: AdminAccess,
        refund_no: str,
        payload: AdminRefundDecisionRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> RefundApplicationView:
        claim = await self.idempotency.begin(
            scope_key=f"admin:refund-decision:{refund_no}:{access.context.user.user_no}",
            idempotency_key=idempotency_key,
            payload={
                "expected_version": expected_version,
                "command": payload.model_dump(mode="json"),
            },
            resource_type="refund_application",
        )
        refund = await self.repository.admin_application(refund_no, for_update=True)
        if refund is None:
            raise _not_found()
        access.require_scope("store", refund.store_id)
        if claim.replayed:
            return await self._view(refund)
        self._validate_refund_decision(access, refund, expected_version=expected_version)
        previous = refund.refund_status
        refund.refund_status = "approved" if payload.decision == "approve" else "rejected"
        if payload.approved_amount is not None:
            amount = int(payload.approved_amount.minor_units)
            if (
                payload.approved_amount.currency != refund.currency
                or amount != refund.requested_amount
            ):
                raise ApplicationError(
                    status=409,
                    code="REFUND_AMOUNT_EDIT_NOT_ALLOWED",
                    title="Refund amount cannot be edited",
                    detail="首版退款金额不可由审核员改写，请按申请金额处理。",
                )
            refund.approved_amount = amount
        refund.decided_at = utc_now()
        refund.decided_by = access.context.user.id
        refund.version += 1
        if payload.decision == "reject":
            for item, _ in await self.repository.items_for_refund(refund.id):
                item.refund_status = "released"
            await self.session.flush()
            await self._refresh_order_projection(refund.order_id)
        elif refund.refund_type == "refund_only":
            payment = await self.session.scalar(
                select(Payment)
                .join(Order, Order.trade_order_id == Payment.trade_order_id)
                .where(
                    Order.id == refund.order_id,
                    Payment.payment_status.in_(("succeeded", "partially_refunded")),
                )
                .order_by(Payment.paid_at.desc(), Payment.id.desc())
                .with_for_update()
            )
            if payment is None:
                raise ApplicationError(
                    status=409,
                    code="REFUND_PAYMENT_SOURCE_UNAVAILABLE",
                    title="Refund payment source unavailable",
                    detail="未找到可执行原路退款的支付记录。",
                )
            refund.refund_status = "refunding"
            self.session.add(
                RefundPaymentRecord(
                    refund_payment_no=new_prefixed_ulid("rfp_"),
                    refund_id=refund.id,
                    payment_id=payment.id,
                    provider=payment.provider,
                    amount=refund.approved_amount,
                    currency=refund.currency,
                    payment_status="pending",
                )
            )
        else:
            refund.refund_status = "waiting_return"
        self.session.add(
            RefundEvent(
                event_no=new_prefixed_ulid("rfe_"),
                refund_id=refund.id,
                from_status=previous,
                to_status=refund.refund_status,
                event_code=f"refund.{refund.refund_status}",
                actor_type="admin",
                actor_user_id=access.context.user.id,
                reason=payload.reason,
                request_id=request_id_context.get(),
            )
        )
        record_admin_operation(
            self.session,
            access,
            action="refund.decide",
            target_type="refund_application",
            target_no=refund.refund_no,
            reason=payload.reason,
            before={"status": previous},
            after={"status": refund.refund_status, "approved_amount": refund.approved_amount},
            scope_type="store",
            scope_id=refund.store_id,
        )
        await self.session.flush()
        result = await self._view(refund)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=refund.refund_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    @staticmethod
    def _validate_refund_decision(
        access: AdminAccess,
        refund: RefundApplication,
        *,
        expected_version: int,
    ) -> None:
        if refund.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="售后申请已经变化，请刷新后重试。",
            )
        if refund.refund_status not in {"submitted", "merchant_review"}:
            raise ApplicationError(
                status=409,
                code="REFUND_DECISION_NOT_ALLOWED",
                title="Refund decision not allowed",
                detail="当前售后状态不允许审核。",
            )
        if refund.claimed_by != access.context.user.id:
            raise ApplicationError(
                status=409,
                code="REFUND_CLAIM_REQUIRED",
                title="Refund claim required",
                detail="退款申请必须由当前审核员先领取后才能作出决定。",
            )

    @staticmethod
    def _validate_appeal_decision(
        access: AdminAccess,
        appeal: RefundAppeal,
        *,
        expected_version: int,
    ) -> None:
        if appeal.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="申诉已经变化，请刷新后重试。",
            )
        if appeal.appeal_status not in {"submitted", "reviewing"}:
            raise ApplicationError(
                status=409,
                code="REFUND_APPEAL_DECISION_NOT_ALLOWED",
                title="Appeal conflict",
                detail="当前申诉状态不能审核。",
            )
        if appeal.claimed_by != access.context.user.id:
            raise ApplicationError(
                status=409,
                code="APPEAL_CLAIM_REQUIRED",
                title="Appeal claim required",
                detail="申诉必须由当前复核员先领取后才能作出决定。",
            )

    async def create_appeal(
        self,
        user: User,
        refund_no: str,
        payload: RefundAppealCreateRequest,
        idempotency_key: str,
    ) -> RefundAppealView:
        claim = await self.idempotency.begin(
            scope_key=f"refund:appeal:{user.user_no}:{refund_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="refund_appeal",
        )
        if claim.replayed and claim.record.response_body is not None:
            return RefundAppealView.model_validate(claim.record.response_body)
        refund = await self.repository.application(user.id, refund_no, for_update=True)
        if refund is None:
            raise _not_found()
        if refund.refund_status != "rejected":
            raise ApplicationError(
                status=409,
                code="REFUND_APPEAL_NOT_ALLOWED",
                title="Refund appeal not allowed",
                detail="当前售后状态不允许申诉。",
            )
        appeal = RefundAppeal(
            appeal_no=new_prefixed_ulid("rap_"),
            refund_id=refund.id,
            user_id=user.id,
            store_id=refund.store_id,
            appeal_status="submitted",
            reason=payload.reason,
        )
        self.session.add(appeal)
        await self.session.flush()
        self.session.add(
            RefundAppealEvent(
                event_no=new_prefixed_ulid("rae_"),
                appeal_id=appeal.id,
                event_type="appeal.created",
                from_status=None,
                to_status="submitted",
                actor_type="user",
                actor_id=user.id,
                reason_code="USER_APPEAL",
                remark=payload.reason,
                appeal_version=appeal.version,
                trace_id=request_id_context.get(),
            )
        )
        await self.session.refresh(appeal, attribute_names=["created_at"])
        result = _appeal_view(appeal, refund.refund_no)
        self.idempotency.complete(
            claim,
            response_status=201,
            resource_no=appeal.appeal_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def appeal_detail(self, user: User, appeal_no: str) -> RefundAppealView:
        appeal = await self.repository.appeal(user.id, appeal_no)
        if appeal is None:
            raise _not_found()
        refund = await self.session.get(RefundApplication, appeal.refund_id)
        if refund is None:
            raise _not_found()
        return _appeal_view(appeal, refund.refund_no)

    async def appeal_events(self, user: User, appeal_no: str) -> RefundAppealEventList:
        appeal = await self.repository.appeal(user.id, appeal_no)
        if appeal is None:
            raise _not_found()
        return RefundAppealEventList(
            items=[
                RefundAppealEventView(
                    event_id=event.event_no,
                    event_type=event.event_type,
                    from_status=event.from_status,
                    to_status=event.to_status,
                    actor_type=event.actor_type,
                    reason_code=event.reason_code,
                    remark=event.remark,
                    appeal_version=event.appeal_version,
                    occurred_at=event.created_at,
                )
                for event in await self.repository.appeal_events(appeal.id)
            ]
        )

    async def cancel_appeal(
        self,
        user: User,
        appeal_no: str,
        expected_version: int,
        idempotency_key: str,
    ) -> RefundAppealView:
        claim = await self.idempotency.begin(
            scope_key=f"refund:appeal-cancel:{user.user_no}:{appeal_no}",
            idempotency_key=idempotency_key,
            payload={"expected_version": expected_version},
            resource_type="refund_appeal",
        )
        if claim.replayed and claim.record.response_body is not None:
            return RefundAppealView.model_validate(claim.record.response_body)
        appeal = await self.repository.appeal(user.id, appeal_no, for_update=True)
        if appeal is None:
            raise _not_found()
        if appeal.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="申诉已经变化，请刷新后重试。",
            )
        if appeal.appeal_status != "submitted":
            raise ApplicationError(
                status=409,
                code="REFUND_APPEAL_CANCEL_NOT_ALLOWED",
                title="Appeal cannot be cancelled",
                detail="申诉已被领取、处理或关闭，当前状态不允许撤销。",
            )
        refund = await self.session.get(RefundApplication, appeal.refund_id)
        if refund is None:
            raise _not_found()
        previous = appeal.appeal_status
        appeal.appeal_status = "cancelled"
        appeal.version += 1
        self.session.add(
            RefundAppealEvent(
                event_no=new_prefixed_ulid("rae_"),
                appeal_id=appeal.id,
                event_type="appeal.cancelled",
                from_status=previous,
                to_status=appeal.appeal_status,
                actor_type="user",
                actor_id=user.id,
                reason_code="USER_CANCELLED",
                appeal_version=appeal.version,
                trace_id=request_id_context.get(),
            )
        )
        await self.session.flush()
        result = _appeal_view(appeal, refund.refund_no)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=appeal.appeal_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def process_refund_webhook(
        self, provider: str, raw_body: bytes, signature: str, timestamp: str
    ) -> RefundPaymentCallbackAck:
        if provider != "fake" or len(raw_body) > 65536:
            raise ApplicationError(
                status=400,
                code="REFUND_WEBHOOK_INVALID",
                title="Invalid refund webhook",
                detail="退款回调不可用。",
            )
        try:
            issued_at = datetime.fromtimestamp(int(timestamp), UTC).replace(tzinfo=None)
            payload = FakeRefundWebhook.model_validate(json.loads(raw_body))
        except (ValueError, OverflowError, TypeError, json.JSONDecodeError, ValidationError) as exc:
            raise ApplicationError(
                status=400,
                code="REFUND_WEBHOOK_INVALID",
                title="Invalid refund webhook",
                detail="退款回调报文无效。",
            ) from exc
        now = utc_now()
        expected = self.security.keyed_hash(
            "fake-refund-webhook", timestamp.encode() + b"." + raw_body
        ).hex()
        if abs((now - issued_at).total_seconds()) > 300 or not hmac.compare_digest(
            expected, signature
        ):
            raise ApplicationError(
                status=401,
                code="REFUND_WEBHOOK_SIGNATURE_INVALID",
                title="Invalid signature",
                detail="退款回调签名无效。",
            )
        amount = int(payload.amount_minor_units)
        record = await self.repository.refund_payment_by_no(
            payload.refund_payment_no, for_update=True
        )
        if record is None:
            raise _not_found()
        if (
            record.provider != provider
            or record.amount != amount
            or record.currency != payload.currency
        ):
            raise ApplicationError(
                status=409,
                code="REFUND_WEBHOOK_MISMATCH",
                title="Refund mismatch",
                detail="退款回调金额或币种不匹配。",
            )
        from app.modules.after_sale.models import RefundPaymentEvent

        existing = await self.session.scalar(
            select(RefundPaymentEvent).where(
                RefundPaymentEvent.refund_payment_id == record.id,
                RefundPaymentEvent.provider_event_id == payload.provider_event_id,
            )
        )
        if existing is not None:
            return RefundPaymentCallbackAck(
                accepted=True, duplicate=True, status=record.payment_status
            )
        if record.payment_status == "succeeded":
            return RefundPaymentCallbackAck(accepted=True, duplicate=True, status="succeeded")
        self.session.add(
            RefundPaymentEvent(
                event_no=new_prefixed_ulid("rpe_"),
                refund_payment_id=record.id,
                provider_event_id=payload.provider_event_id,
                provider_status=payload.status,
                amount=amount,
                currency=payload.currency,
                signature_valid=True,
            )
        )
        record.payment_status = payload.status
        if payload.status == "succeeded":
            refund = await self.session.get(
                RefundApplication, record.refund_id, with_for_update=True
            )
            payment = await self.session.get(Payment, record.payment_id, with_for_update=True)
            if refund is None or payment is None:
                raise ApplicationError(
                    status=409,
                    code="REFUND_AMOUNT_EXCEEDS_LIMIT",
                    title="Refund exceeds payment",
                    detail="累计退款金额超过支付金额。",
                )
            items = await self.repository.items_for_refund(refund.id)
            order = await self.session.scalar(
                select(Order).where(Order.id == refund.order_id).with_for_update()
            )
            trade = (
                await self.session.scalar(
                    select(TradeOrder)
                    .where(TradeOrder.id == order.trade_order_id)
                    .with_for_update()
                )
                if order is not None
                else None
            )
            if order is None or trade is None:
                raise ApplicationError(
                    status=409,
                    code="REFUND_ORDER_PROJECTION_UNAVAILABLE",
                    title="Refund projection unavailable",
                    detail="退款对应的订单投影不可用。",
                )
            if (
                payment.refunded_amount + amount > payment.paid_amount
                or order.refunded_amount + amount > order.paid_amount
                or trade.refunded_amount + amount > trade.paid_amount
            ):
                raise ApplicationError(
                    status=409,
                    code="REFUND_AMOUNT_EXCEEDS_LIMIT",
                    title="Refund exceeds payment",
                    detail="累计退款金额超过支付、订单或交易单可退金额。",
                )
            payment.refunded_amount += amount
            payment.payment_status = (
                "refunded"
                if payment.refunded_amount == payment.paid_amount
                else "partially_refunded"
            )
            payment.version += 1
            refund.refund_status = "succeeded"
            refund.version += 1
            record.completed_at = now
            for refund_item, order_item in items:
                if refund_item.refund_status == "active":
                    refund_item.succeeded_amount = refund_item.requested_amount
                    refund_item.refund_status = "succeeded"
                    order_item.refunded_quantity += refund_item.quantity
                    order_item.refunded_amount += refund_item.requested_amount
                    order_item.after_sale_status = "completed"
                    order_item.version += 1
            order.refunded_amount += amount
            order.payment_status = (
                "refunded" if order.refunded_amount == order.paid_amount else "partially_refunded"
            )
            order.after_sale_status = "completed"
            order.version += 1
            trade.refunded_amount += amount
            trade.trade_status = (
                "refunded" if trade.refunded_amount == trade.paid_amount else "partially_refunded"
            )
            trade.version += 1
            self.session.add(
                OutboxEvent(
                    event_no=new_prefixed_ulid("evt_"),
                    event_type="refund.succeeded.v1",
                    aggregate_type="refund_application",
                    aggregate_no=refund.refund_no,
                    aggregate_version=refund.version,
                    payload={
                        "refund_id": refund.refund_no,
                        "payment_id": payment.payment_no,
                        "order_id": order.order_no,
                        "trade_order_id": trade.trade_no,
                        "amount_minor_units": str(amount),
                        "currency": refund.currency,
                    },
                    event_status="pending",
                    available_at=now,
                    attempt_count=0,
                    trace_id=request_id_context.get() or new_prefixed_ulid("req_"),
                )
            )
        await self.session.commit()
        return RefundPaymentCallbackAck(accepted=True, status=payload.status)

    async def upsert_return_shipment(
        self,
        user: User,
        refund_no: str,
        payload: RefundReturnShipmentRequest,
        expected_version: int,
    ) -> RefundReturnShipmentView:
        refund = await self.repository.application(user.id, refund_no, for_update=True)
        if refund is None:
            raise _not_found()
        if refund.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="售后申请已经变化，请刷新后重试。",
            )
        if refund.refund_status not in {"waiting_return", "returning"}:
            raise ApplicationError(
                status=409,
                code="REFUND_RETURN_NOT_ALLOWED",
                title="Return not allowed",
                detail="当前售后状态不能填写退货物流。",
            )
        normalized = "".join(payload.tracking_no.split()).upper()
        now = utc_now()
        shipment = await self.repository.return_shipment(refund.id, for_update=True)
        if shipment is None:
            shipment = RefundShipment(
                refund_id=refund.id,
                carrier_code=payload.carrier_code,
                carrier_name=payload.carrier_code.replace("_", " ").title(),
                tracking_no_ciphertext=self.security.encrypt("refund-tracking-no", normalized),
                tracking_no_hash=self.security.keyed_hash("refund-tracking-no", normalized),
                tracking_no_masked=("*" * max(0, len(normalized) - 4)) + normalized[-4:],
                shipment_status="submitted",
                shipped_at=now,
                key_version=1,
            )
            self.session.add(shipment)
        else:
            shipment.carrier_code = payload.carrier_code
            shipment.tracking_no_ciphertext = self.security.encrypt(
                "refund-tracking-no", normalized
            )
            shipment.tracking_no_hash = self.security.keyed_hash("refund-tracking-no", normalized)
            shipment.tracking_no_masked = ("*" * max(0, len(normalized) - 4)) + normalized[-4:]
            shipment.version += 1
        previous = refund.refund_status
        refund.refund_status = "returning"
        refund.version += 1
        self.session.add(
            RefundEvent(
                event_no=new_prefixed_ulid("rfe_"),
                refund_id=refund.id,
                from_status=previous,
                to_status="returning",
                event_code="refund.return_shipment_submitted",
                actor_type="user",
                actor_user_id=user.id,
                request_id=request_id_context.get(),
            )
        )
        await self.session.commit()
        return RefundReturnShipmentView(
            refund_id=refund.refund_no,
            carrier_code=shipment.carrier_code,
            carrier_name=shipment.carrier_name,
            tracking_no_masked=shipment.tracking_no_masked,
            shipment_status=shipment.shipment_status,
            version=refund.version,
        )

    async def _views(self, refunds: list[RefundApplication]) -> list[RefundApplicationView]:
        orders = {
            order.id: order
            for order in await self.repository.orders_by_ids([item.order_id for item in refunds])
        }
        item_groups: dict[int, list[tuple[RefundItem, OrderItem]]] = {}
        for refund_item, order_item in await self.repository.items_for_refunds(
            [item.id for item in refunds]
        ):
            item_groups.setdefault(refund_item.refund_id, []).append((refund_item, order_item))
        return [
            await self._view(
                refund,
                order=orders.get(refund.order_id),
                items=item_groups.get(refund.id, []),
            )
            for refund in refunds
        ]

    async def _view(
        self,
        refund: RefundApplication,
        *,
        order: Order | None = None,
        items: list[tuple[RefundItem, OrderItem]] | None = None,
    ) -> RefundApplicationView:
        if items is None:
            items = await self.repository.items_for_refund(refund.id)
        actions: list[str] = ["view_events"]
        if refund.refund_status in {"submitted", "merchant_review"}:
            actions.insert(0, "cancel")
        if refund.refund_status == "rejected":
            actions.extend(["create_refund_appeal", "create_new_refund_application"])
        if order is None:
            order = await self.session.get(Order, refund.order_id)
        if order is None:
            raise _not_found()
        return RefundApplicationView(
            refund_id=refund.refund_no,
            order_id=order.order_no,
            refund_type=refund.refund_type,
            refund_status=refund.refund_status,
            reason_code=refund.reason_code,
            reason_detail=refund.reason_detail,
            requested_amount=_money(refund.requested_amount, refund.currency),
            approved_amount=_money(refund.approved_amount, refund.currency),
            items=[
                RefundApplicationItemView(
                    order_item_id=item.order_item_no,
                    quantity=ri.quantity,
                    requested_amount=_money(ri.requested_amount, refund.currency),
                )
                for ri, item in items
            ],
            available_actions=actions,
            submitted_at=refund.submitted_at,
            decided_at=refund.decided_at,
            version=refund.version,
            claimed=refund.claimed_by is not None,
        )

    async def _refresh_order_projection(self, order_id: int) -> None:
        order = await self.session.get(Order, order_id)
        if order is None:
            raise _not_found()
        order_items = await self.repository.order_items_for_update(order_id)
        active = await self.repository.active_items([item.id for item in order_items])
        active_ids = {item.order_item_id for item in active}
        for item in order_items:
            next_status = "in_progress" if item.id in active_ids else "none"
            if item.after_sale_status != next_status:
                item.after_sale_status = next_status
                item.version += 1
        next_order_status = "in_progress" if active_ids else "none"
        if order.after_sale_status != next_order_status:
            order.after_sale_status = next_order_status
            order.version += 1


def _money(amount: int, currency: str) -> Money:
    return Money(minor_units=str(amount), currency=currency)


def _next_refundable_amount(
    item: OrderItem,
    *,
    active_quantity: int,
    active_amount: int,
    requested_quantity: int,
) -> int:
    """Allocate item-level rounding deterministically across partial refunds."""
    consumed_quantity = item.refunded_quantity + active_quantity
    consumed_amount = item.refunded_amount + active_amount
    target_quantity = min(item.quantity, consumed_quantity + requested_quantity)
    target_amount = item.payable_amount * target_quantity // item.quantity
    return max(0, target_amount - consumed_amount)


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404, code="RESOURCE_NOT_FOUND", title="Resource not found", detail="售后资源不存在。"
    )


def _appeal_view(appeal: RefundAppeal, refund_no: str) -> RefundAppealView:
    return RefundAppealView(
        appeal_id=appeal.appeal_no,
        refund_id=refund_no,
        appeal_status=appeal.appeal_status,
        reason=appeal.reason,
        submitted_at=appeal.created_at,
        decided_at=appeal.decided_at,
        version=appeal.version,
        claimed=appeal.claimed_by is not None,
    )


def _eligibility_token(security: SecurityService, payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    signature = security.keyed_hash("refund-eligibility", raw)

    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"{encode(raw)}.{encode(signature)}"


def _read_eligibility_token(security: SecurityService, token: str) -> dict[str, object] | None:
    try:
        encoded_raw, encoded_signature = token.split(".", maxsplit=1)
        padding = "=" * (-len(encoded_raw) % 4)
        raw = base64.urlsafe_b64decode(encoded_raw + padding)
        padding = "=" * (-len(encoded_signature) % 4)
        signature = base64.urlsafe_b64decode(encoded_signature + padding)
        expected = security.keyed_hash("refund-eligibility", raw)
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or int(data.get("exp", 0)) <= int(utc_now().timestamp()):
            return None
        return data
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
