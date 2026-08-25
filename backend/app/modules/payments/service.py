from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import SecurityService, utc_now
from app.integrations.payments import PaymentProviderRequest, payment_provider
from app.modules.catalog.schemas import Money
from app.modules.identity.models import User
from app.modules.inventory.models import InventoryLog
from app.modules.orders.domain import ORDER_TRANSITIONS, require_transition
from app.modules.orders.models import Order, OrderStatusLog, TradeOrder
from app.modules.orders.repository import OrderRepository
from app.modules.payments.domain import require_payment_transition
from app.modules.payments.models import Payment, PaymentCallback, PaymentEvent
from app.modules.payments.repository import PaymentRepository
from app.modules.payments.schemas import (
    AdminPaymentList,
    AdminPaymentReconciliationRequest,
    AdminPaymentReconciliationResult,
    AdminPaymentView,
    FakePaymentWebhook,
    PaymentAction,
    PaymentCreateRequest,
    PaymentEventView,
    PaymentList,
    PaymentStatus,
    PaymentView,
    PaymentWebhookAck,
)
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.system.models import OutboxEvent


class PaymentService:
    def __init__(self, session: AsyncSession, security: SecurityService) -> None:
        self.session = session
        self.security = security
        self.repository = PaymentRepository(session)
        self.order_repository = OrderRepository(session)
        self.idempotency = IdempotencyService(session)

    async def create(
        self,
        user: User,
        payload: PaymentCreateRequest,
        idempotency_key: str,
        client_ip: str,
    ) -> PaymentView:
        claim = await self.idempotency.begin(
            scope_key=(
                f"payment:create:{user.user_no}:{payload.trade_order_id}:{payload.provider}"
            ),
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="payment",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.by_no(user.id, claim.record.resource_no)
            if existing is not None:
                return await self._view(existing[0], existing[1])
            raise _error(409, "IDEMPOTENCY_RESULT_UNAVAILABLE", "原支付结果不可用。")

        trade = await self.repository.user_trade_for_update(user.id, payload.trade_order_id)
        if trade is None:
            raise _not_found()
        now = utc_now()
        if trade.trade_status in {"paid", "partially_refunded", "refunded"}:
            raise _error(409, "TRADE_ORDER_ALREADY_PAID", "该交易单已经支付。")
        if trade.trade_status != "pending_payment":
            raise _error(409, "TRADE_ORDER_STATE_CONFLICT", "该交易单当前不可支付。")
        if trade.expires_at <= now:
            raise _error(410, "PAYMENT_WINDOW_EXPIRED", "支付窗口已过期。")
        active = await self.repository.active_for_trade(trade.id)
        if active is not None:
            raise ApplicationError(
                status=409,
                code="PAYMENT_ATTEMPT_IN_PROGRESS",
                title="Payment attempt in progress",
                detail=f"已有支付尝试 {active.payment_no} 正在确认中。",
                headers={"Location": f"/api/v1/payments/{active.payment_no}"},
            )

        payment = Payment(
            payment_no=new_prefixed_ulid("pay_"),
            trade_order_id=trade.id,
            user_id=user.id,
            provider=payload.provider,
            payment_method=payload.payment_method,
            payment_status="created",
            requested_amount=trade.payable_amount,
            paid_amount=0,
            refunded_amount=0,
            currency=trade.currency,
            client_ip_hash=self.security.keyed_hash("payment-client-ip", client_ip),
            expires_at=trade.expires_at,
        )
        self.session.add(payment)
        await self.session.flush()
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        self.session.add(_event(payment, "created", None, "created", "api", request_id))

        acceptance = await payment_provider(payload.provider).create_payment(
            PaymentProviderRequest(
                payment_no=payment.payment_no,
                trade_order_no=trade.trade_no,
                amount=payment.requested_amount,
                currency=payment.currency,
                payment_method=payload.payment_method,
                return_url_key=payload.return_url_key,
            )
        )
        payment.provider_trade_no = acceptance.provider_trade_no
        payment.provider_request_id = acceptance.provider_request_id
        payment.payment_status = "pending"
        payment.version += 1
        self.session.add(
            _event(
                payment,
                "provider_requested",
                "created",
                "pending",
                "provider",
                acceptance.provider_request_id,
            )
        )
        orders = await self.repository.mark_trade_orders_processing(trade.id)
        for order in orders:
            self.session.add(
                OrderStatusLog(
                    order_id=order.id,
                    state_dimension="payment",
                    from_status="unpaid",
                    to_status="processing",
                    event_code="payment.attempt_started",
                    actor_type="user",
                    actor_id=user.id,
                    reason=None,
                    order_version=order.version,
                    request_id=request_id,
                    trace_id=request_id,
                )
            )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="payment.pending.v1",
                aggregate_type="payment",
                aggregate_no=payment.payment_no,
                aggregate_version=payment.version,
                payload={
                    "payment_id": payment.payment_no,
                    "trade_order_id": trade.trade_no,
                    "status": payment.payment_status,
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=request_id,
            )
        )
        await self.session.flush()
        result = await self._view(
            payment,
            trade,
            action=PaymentAction(type=acceptance.action_type, url=acceptance.action_url),
        )
        self.idempotency.complete(
            claim,
            response_status=201,
            resource_no=payment.payment_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def get(self, user: User, payment_no: str) -> PaymentView:
        row = await self.repository.by_no(user.id, payment_no)
        if row is None:
            raise _not_found()
        return await self._view(row[0], row[1])

    async def close(
        self,
        user: User,
        payment_no: str,
        expected_version: int,
        idempotency_key: str,
    ) -> PaymentView:
        claim = await self.idempotency.begin(
            scope_key=f"payment:close:{user.user_no}:{payment_no}",
            idempotency_key=idempotency_key,
            payload={"version": expected_version},
            resource_type="payment",
        )
        if claim.replayed and claim.record.response_body is not None:
            return PaymentView.model_validate(claim.record.response_body)
        untrusted = await self.repository.by_no(user.id, payment_no)
        if untrusted is None:
            raise _not_found()
        initial, _ = untrusted
        trade = await self.repository.trade_for_update(initial.trade_order_id)
        payment = await self.repository.payment_for_update(initial.id)
        if trade is None or payment is None or payment.user_id != user.id:
            raise _not_found()
        if payment.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="支付单已经变化，请刷新后重试。",
            )
        if payment.provider_trade_no is None:
            raise _error(409, "PAYMENT_PROVIDER_REFERENCE_MISSING", "支付渠道引用不可用。")
        await payment_provider(payment.provider).close_payment(payment.provider_trade_no)
        now = utc_now()
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        await self._close_locked(payment, trade, request_id, now, source_type="api")
        await self.session.flush()
        result = await self._view(payment, trade)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=payment.payment_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def list_for_trade(self, user: User, trade_no: str) -> PaymentList:
        trade = await self.repository.user_trade(user.id, trade_no)
        if trade is None:
            raise _not_found()
        payments = await self.repository.for_trade(user.id, trade_no)
        return PaymentList(items=[await self._view(item, trade) for item in payments])

    async def admin_list(
        self,
        access: AdminAccess,
        *,
        query: str | None,
        payment_status: str | None,
        provider: str | None,
        limit: int,
    ) -> AdminPaymentList:
        normalized_query = query.strip() if query else None
        rows = await self.repository.admin_payments(
            scopes=access.scopes,
            query=normalized_query or None,
            payment_status=payment_status,
            provider=provider,
            limit=limit,
        )
        return AdminPaymentList(
            items=[await self._admin_view(access, payment, trade) for payment, trade in rows]
        )

    async def admin_detail(self, access: AdminAccess, payment_no: str) -> AdminPaymentView:
        row = await self.repository.admin_by_no(payment_no)
        if row is None:
            raise _not_found()
        return await self._admin_view(access, row[0], row[1])

    async def admin_reconcile(
        self,
        access: AdminAccess,
        payment_no: str,
        payload: AdminPaymentReconciliationRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminPaymentReconciliationResult:
        claim = await self.idempotency.begin(
            scope_key=f"admin:payment-reconcile:{access.context.user.user_no}:{payment_no}",
            idempotency_key=idempotency_key,
            payload={"version": expected_version, **payload.model_dump(mode="json")},
            resource_type="payment",
        )
        if claim.replayed and claim.record.response_body is not None:
            return AdminPaymentReconciliationResult.model_validate(claim.record.response_body)
        initial = await self.repository.admin_by_no(payment_no)
        if initial is None:
            raise _not_found()
        initial_payment, initial_trade = initial
        stores = await self.repository.trade_stores(initial_trade.id)
        for store_id, _ in stores:
            access.require_scope("store", store_id)
        if initial_payment.payment_status not in {"created", "pending"}:
            raise _error(409, "PAYMENT_NOT_RECONCILABLE", "只有确认中的支付单可以发起对账。")
        if initial_payment.provider_trade_no is None:
            raise _error(409, "PAYMENT_PROVIDER_REFERENCE_MISSING", "支付渠道引用不可用。")
        try:
            snapshot = await payment_provider(initial_payment.provider).query_payment(
                initial_payment.provider_trade_no,
                amount=initial_payment.requested_amount,
                currency=initial_payment.currency,
            )
        except (TimeoutError, ValueError) as exc:
            raise ApplicationError(
                status=503,
                code="PAYMENT_PROVIDER_QUERY_FAILED",
                title="Payment provider query failed",
                detail="暂时无法取得支付渠道权威状态，请稍后重试。",
                retryable=True,
            ) from exc
        if (
            snapshot.provider_trade_no != initial_payment.provider_trade_no
            or snapshot.amount != initial_payment.requested_amount
            or snapshot.currency != initial_payment.currency
        ):
            raise _error(
                409,
                "PAYMENT_RECONCILIATION_MISMATCH",
                "渠道返回的交易引用、金额或币种与本地支付单不一致，已拒绝自动处理。",
            )
        trade = await self.repository.trade_for_update(initial_trade.id)
        payment = await self.repository.payment_for_update(initial_payment.id)
        if trade is None or payment is None:
            raise _not_found()
        if payment.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="支付单已经变化，请刷新后重试。",
            )
        if payment.payment_status not in {"created", "pending"}:
            raise _error(409, "PAYMENT_NOT_RECONCILABLE", "支付单状态已变化，请刷新。")
        previous_status = payment.payment_status
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        now = utc_now()
        if snapshot.status == "pending":
            payment.version += 1
            self.session.add(
                _event(
                    payment,
                    "reconciliation_observed",
                    previous_status,
                    previous_status,
                    "reconciliation",
                    request_id,
                )
            )
        elif snapshot.status == "closed":
            await self._close_locked(payment, trade, request_id, now, source_type="reconciliation")
        else:
            confirmation = FakePaymentWebhook(
                provider_event_id=f"reconciliation:{request_id}",
                payment_id=payment.payment_no,
                provider_trade_no=snapshot.provider_trade_no,
                status=snapshot.status,
                amount_minor_units=str(snapshot.amount),
                currency=snapshot.currency,
                occurred_at=now.replace(tzinfo=UTC),
                failure_code=("PROVIDER_DECLINED" if snapshot.status == "failed" else None),
            )
            if snapshot.status == "failed":
                await self._record_failed_callback(
                    payment,
                    trade,
                    None,
                    confirmation,
                    now,
                    request_id,
                    source_type="reconciliation",
                )
            else:
                await self._record_succeeded_callback(
                    payment,
                    trade,
                    None,
                    confirmation,
                    now,
                    request_id,
                    source_type="reconciliation",
                )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="payment.reconciled.v1",
                aggregate_type="payment",
                aggregate_no=payment.payment_no,
                aggregate_version=payment.version,
                payload={
                    "payment_id": payment.payment_no,
                    "trade_order_id": trade.trade_no,
                    "previous_status": previous_status,
                    "provider_status": snapshot.status,
                    "local_status": payment.payment_status,
                    "reason_code": payload.reason_code,
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=request_id,
            )
        )
        record_admin_operation(
            self.session,
            access,
            action="reconcile_payment",
            target_type="payment",
            target_no=payment.payment_no,
            reason=payload.reason,
            before={"payment_status": previous_status, "version": expected_version},
            after={
                "payment_status": payment.payment_status,
                "provider_status": snapshot.status,
                "version": payment.version,
                "reason_code": payload.reason_code,
            },
        )
        await self.session.flush()
        result = AdminPaymentReconciliationResult(
            payment=await self._admin_view(access, payment, trade),
            provider_status=snapshot.status,
            result=("no_change" if payment.payment_status == previous_status else "status_updated"),
            reconciled_at=now,
        )
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=payment.payment_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def reconcile_expired(self, *, limit: int = 100) -> int:
        now = utc_now()
        candidates = await self.repository.expired_active(now, limit)
        processed = 0
        for candidate in candidates:
            if candidate.provider_trade_no is None:
                continue
            provider = payment_provider(candidate.provider)
            snapshot = await provider.query_payment(
                candidate.provider_trade_no,
                amount=candidate.requested_amount,
                currency=candidate.currency,
            )
            if (
                snapshot.provider_trade_no != candidate.provider_trade_no
                or snapshot.amount != candidate.requested_amount
                or snapshot.currency != candidate.currency
            ):
                continue
            if snapshot.status != "pending":
                continue
            await provider.close_payment(candidate.provider_trade_no)
            trade = await self.repository.trade_for_update(candidate.trade_order_id)
            payment = await self.repository.payment_for_update(candidate.id)
            if (
                trade is None
                or payment is None
                or payment.payment_status not in {"created", "pending"}
                or payment.expires_at > now
            ):
                continue
            request_id = new_prefixed_ulid("req_")
            self.session.add(
                _event(
                    payment,
                    "provider_queried",
                    payment.payment_status,
                    payment.payment_status,
                    "reconciliation",
                    request_id,
                )
            )
            await self._close_locked(
                payment,
                trade,
                request_id,
                now,
                source_type="reconciliation",
            )
            processed += 1
        await self.session.commit()
        return processed

    async def _close_locked(
        self,
        payment: Payment,
        trade: TradeOrder,
        request_id: str,
        now: datetime,
        *,
        source_type: str,
    ) -> None:
        previous = payment.payment_status
        target = require_payment_transition(previous, "ClosePaymentAttempt")
        payment.payment_status = target
        payment.closed_at = now
        payment.version += 1
        self.session.add(_event(payment, "closed", previous, target, source_type, request_id))
        orders = await self.repository.trade_orders_for_update(trade.id)
        for order in orders:
            if order.payment_status == "processing":
                order.payment_status = "unpaid"
                order.version += 1
                self.session.add(
                    _order_event(
                        order,
                        "payment",
                        "processing",
                        "unpaid",
                        "payment.attempt_closed",
                        request_id,
                        now,
                    )
                )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="payment.closed.v1",
                aggregate_type="payment",
                aggregate_no=payment.payment_no,
                aggregate_version=payment.version,
                payload={
                    "payment_id": payment.payment_no,
                    "trade_order_id": trade.trade_no,
                    "source_type": source_type,
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=request_id,
            )
        )

    async def process_webhook(
        self,
        provider: str,
        raw_body: bytes,
        signature: str,
        timestamp: str,
    ) -> PaymentWebhookAck:
        if provider != "fake":
            raise _not_found()
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        now = utc_now()
        headers_hash = hashlib.sha256(
            f"timestamp:{timestamp}\nsignature:{signature}".encode()
        ).digest()
        payload_hash = hashlib.sha256(raw_body).digest()
        signature_valid = _valid_fake_signature(self.security, raw_body, signature, timestamp, now)
        if not signature_valid:
            callback = PaymentCallback(
                callback_no=new_prefixed_ulid("pcb_"),
                provider=provider,
                provider_event_id=None,
                payment_id=None,
                headers_hash=headers_hash,
                payload_hash=payload_hash,
                payload_redacted=None,
                signature_status="invalid",
                process_status="rejected",
                attempt_count=1,
                processed_at=now,
                error_code="PAYMENT_WEBHOOK_SIGNATURE_INVALID",
                last_error="signature or timestamp window rejected",
                request_id=request_id,
            )
            self.session.add(callback)
            await self.session.commit()
            raise _error(401, "PAYMENT_WEBHOOK_SIGNATURE_INVALID", "支付回调验签失败。")
        try:
            payload = FakePaymentWebhook.model_validate_json(raw_body)
        except ValidationError as exc:
            callback = PaymentCallback(
                callback_no=new_prefixed_ulid("pcb_"),
                provider=provider,
                provider_event_id=None,
                payment_id=None,
                headers_hash=headers_hash,
                payload_hash=payload_hash,
                payload_redacted=None,
                signature_status="valid",
                process_status="rejected",
                attempt_count=1,
                processed_at=now,
                error_code="PAYMENT_WEBHOOK_SCHEMA_INVALID",
                last_error="signed payload failed schema validation",
                request_id=request_id,
            )
            self.session.add(callback)
            await self.session.commit()
            raise ApplicationError(
                status=422,
                code="PAYMENT_WEBHOOK_SCHEMA_INVALID",
                title="Payment webhook schema invalid",
                detail="支付回调字段校验失败。",
            ) from exc

        existing_callback = await self.repository.callback_by_provider_event(
            provider, payload.provider_event_id
        )
        if existing_callback is not None:
            return PaymentWebhookAck(callback_id=existing_callback.callback_no, duplicate=True)
        callback = PaymentCallback(
            callback_no=new_prefixed_ulid("pcb_"),
            provider=provider,
            provider_event_id=payload.provider_event_id,
            payment_id=None,
            headers_hash=headers_hash,
            payload_hash=payload_hash,
            payload_redacted={
                "payment_id": payload.payment_id,
                "provider_trade_no": payload.provider_trade_no,
                "status": payload.status,
                "amount_minor_units": payload.amount_minor_units,
                "currency": payload.currency,
                "occurred_at": payload.occurred_at.isoformat(),
                "failure_code": payload.failure_code,
            },
            signature_status="valid",
            process_status="received",
            attempt_count=1,
            request_id=request_id,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(callback)
                await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            duplicate = await self.repository.callback_by_provider_event(
                provider, payload.provider_event_id
            )
            if duplicate is None:
                raise
            return PaymentWebhookAck(callback_id=duplicate.callback_no, duplicate=True)

        untrusted_payment = await self.repository.untrusted_by_no(payload.payment_id)
        if untrusted_payment is None:
            _reject_callback(callback, now, "PAYMENT_NOT_FOUND", "unknown payment reference")
            await self.session.commit()
            return PaymentWebhookAck(callback_id=callback.callback_no)
        trade = await self.repository.trade_for_update(untrusted_payment.trade_order_id)
        payment = await self.repository.payment_for_update(untrusted_payment.id)
        if trade is None or payment is None:
            _reject_callback(callback, now, "PAYMENT_NOT_FOUND", "payment aggregate unavailable")
            await self.session.commit()
            return PaymentWebhookAck(callback_id=callback.callback_no)
        callback.payment_id = payment.id
        mismatch = _webhook_mismatch(payment, payload)
        if mismatch is not None:
            _reject_callback(
                callback, now, mismatch, "provider identity, amount, or currency mismatch"
            )
            await self.session.commit()
            return PaymentWebhookAck(callback_id=callback.callback_no)
        if payment.payment_status == "succeeded" and payload.status == "succeeded":
            callback.process_status = "duplicate"
            callback.processed_at = now
            await self.session.commit()
            return PaymentWebhookAck(callback_id=callback.callback_no, duplicate=True)
        if payment.payment_status not in {"created", "pending"}:
            _reject_callback(
                callback,
                now,
                "PAYMENT_LATE_TERMINAL_EVENT",
                f"terminal local status {payment.payment_status}",
            )
            await self.session.commit()
            return PaymentWebhookAck(callback_id=callback.callback_no)

        if payload.status == "failed":
            await self._record_failed_callback(payment, trade, callback, payload, now, request_id)
        else:
            await self._record_succeeded_callback(
                payment, trade, callback, payload, now, request_id
            )
        if callback.process_status == "received":
            callback.process_status = "processed"
            callback.processed_at = now
        callback.version += 1
        await self.session.commit()
        return PaymentWebhookAck(callback_id=callback.callback_no)

    async def _record_failed_callback(
        self,
        payment: Payment,
        trade: TradeOrder,
        callback: PaymentCallback | None,
        payload: FakePaymentWebhook,
        now: datetime,
        request_id: str,
        *,
        source_type: str = "callback",
    ) -> None:
        if trade.trade_status != "pending_payment":
            _reject_confirmation(
                callback,
                now,
                "TRADE_ORDER_STATE_CONFLICT",
                f"trade status {trade.trade_status}",
            )
            return
        previous = payment.payment_status
        payment.payment_status = require_payment_transition(previous, "ConfirmPaymentFailed")
        payment.failure_code = payload.failure_code or "PROVIDER_DECLINED"
        payment.failure_message = "支付渠道已明确返回失败。"
        payment.version += 1
        provider_time = payload.occurred_at.astimezone(UTC).replace(tzinfo=None)
        self.session.add(
            _event(
                payment,
                f"{source_type}_failed",
                previous,
                "failed",
                source_type,
                payload.provider_event_id,
                provider_occurred_at=provider_time,
            )
        )
        orders = await self.repository.trade_orders_for_update(trade.id)
        for order in orders:
            payment_from = order.payment_status
            if payment_from == "processing":
                order.payment_status = "unpaid"
                order.version += 1
                self.session.add(
                    _order_event(
                        order,
                        "payment",
                        payment_from,
                        "unpaid",
                        "payment.failed",
                        request_id,
                        now,
                    )
                )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="payment.failed.v1",
                aggregate_type="payment",
                aggregate_no=payment.payment_no,
                aggregate_version=payment.version,
                payload={
                    "payment_id": payment.payment_no,
                    "trade_order_id": trade.trade_no,
                    "failure_code": payment.failure_code,
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=request_id,
            )
        )

    async def _record_succeeded_callback(
        self,
        payment: Payment,
        trade: TradeOrder,
        callback: PaymentCallback | None,
        payload: FakePaymentWebhook,
        now: datetime,
        request_id: str,
        *,
        source_type: str = "callback",
    ) -> None:
        if trade.trade_status != "pending_payment":
            _reject_confirmation(
                callback,
                now,
                "TRADE_ORDER_STATE_CONFLICT",
                f"trade status {trade.trade_status}",
            )
            return
        orders = await self.repository.trade_orders_for_update(trade.id)
        if not orders or any(order.order_status != "pending_payment" for order in orders):
            _reject_confirmation(
                callback,
                now,
                "ORDER_STATE_CONFLICT",
                "one or more child orders are not pending payment",
            )
            return
        reservations = await self.order_repository.active_reservations_for_orders(
            [order.id for order in orders]
        )
        items_by_order = await self.order_repository.order_items([order.id for order in orders])
        expected_item_ids = {item.id for items in items_by_order.values() for item in items}
        reservation_item_ids = {reservation.order_item_id for reservation, _ in reservations}
        if not reservations or reservation_item_ids != expected_item_ids:
            _reject_confirmation(
                callback,
                now,
                "INVENTORY_RESERVATION_MISSING",
                "active reservation set does not cover every order item exactly once",
            )
            return
        order_by_id = {order.id: order for order in orders}
        for reservation, inventory in reservations:
            if (
                inventory.reserved_quantity < reservation.quantity
                or inventory.on_hand_quantity < reservation.quantity
            ):
                _reject_confirmation(
                    callback,
                    now,
                    "INVENTORY_RESERVATION_INVALID",
                    "reserved or on-hand quantity cannot be confirmed",
                )
                return
        for reservation, inventory in reservations:
            on_hand_before = inventory.on_hand_quantity
            reserved_before = inventory.reserved_quantity
            inventory.on_hand_quantity -= reservation.quantity
            inventory.reserved_quantity -= reservation.quantity
            inventory.sold_quantity += reservation.quantity
            inventory.version += 1
            reservation.reservation_status = "confirmed"
            reservation.confirmed_at = now
            reservation.version += 1
            order = order_by_id[reservation.order_id]
            self.session.add(
                InventoryLog(
                    inventory_id=inventory.id,
                    sku_id=inventory.sku_id,
                    operation_type="confirm_sale",
                    on_hand_delta=-reservation.quantity,
                    reserved_delta=-reservation.quantity,
                    on_hand_before=on_hand_before,
                    on_hand_after=inventory.on_hand_quantity,
                    reserved_before=reserved_before,
                    reserved_after=inventory.reserved_quantity,
                    reference_type="payment",
                    reference_no=payment.payment_no,
                    idempotency_key=f"payment:{payment.payment_no}:{reservation.reservation_no}",
                    actor_type="system",
                    actor_id=None,
                    reason="payment_succeeded",
                    inventory_version=inventory.version,
                )
            )

        previous_payment = payment.payment_status
        payment.payment_status = require_payment_transition(
            previous_payment, "ConfirmPaymentSucceeded"
        )
        payment.paid_amount = payment.requested_amount
        payment.paid_at = now
        payment.version += 1
        provider_time = payload.occurred_at.astimezone(UTC).replace(tzinfo=None)
        self.session.add(
            _event(
                payment,
                f"{source_type}_succeeded",
                previous_payment,
                "succeeded",
                source_type,
                payload.provider_event_id,
                provider_occurred_at=provider_time,
            )
        )
        trade.trade_status = "paid"
        trade.paid_amount = trade.payable_amount
        trade.paid_at = now
        trade.version += 1
        for order in orders:
            payment_from = order.payment_status
            order.payment_status = "paid"
            order.paid_amount = order.payable_amount
            order.paid_at = now
            order.version += 1
            self.session.add(
                _order_event(
                    order,
                    "payment",
                    payment_from,
                    "paid",
                    "payment.succeeded",
                    request_id,
                    now,
                )
            )
            previous_order = order.order_status
            order.order_status = require_transition(
                ORDER_TRANSITIONS, previous_order, "RecordPaymentSucceeded"
            )
            order.version += 1
            self.session.add(
                _order_event(
                    order,
                    "order",
                    previous_order,
                    "paid",
                    "order.payment_succeeded",
                    request_id,
                    now,
                )
            )
            order.order_status = require_transition(
                ORDER_TRANSITIONS, order.order_status, "InitializeFulfillment"
            )
            order.version += 1
            self.session.add(
                _order_event(
                    order,
                    "order",
                    "paid",
                    "pending_shipment",
                    "order.fulfillment_initialized",
                    request_id,
                    now,
                )
            )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="payment.succeeded.v1",
                aggregate_type="payment",
                aggregate_no=payment.payment_no,
                aggregate_version=payment.version,
                payload={
                    "payment_id": payment.payment_no,
                    "trade_order_id": trade.trade_no,
                    "order_ids": [order.order_no for order in orders],
                    "amount_minor_units": str(payment.paid_amount),
                    "currency": payment.currency,
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=request_id,
            )
        )

    async def _admin_view(
        self,
        access: AdminAccess,
        payment: Payment,
        trade: TradeOrder,
    ) -> AdminPaymentView:
        stores = await self.repository.trade_stores(trade.id)
        for store_id, _ in stores:
            access.require_scope("store", store_id)
        user_no = await self.repository.user_no(payment.user_id)
        if user_no is None:
            raise _not_found()
        return AdminPaymentView(
            payment=await self._view(payment, trade),
            user_id=user_no,
            store_ids=[store_no for _, store_no in stores],
            provider_trade_no_masked=_mask_provider_trade_no(payment.provider_trade_no),
            available_admin_actions=(
                ["reconcile"] if payment.payment_status in {"created", "pending"} else []
            ),
        )

    async def _view(
        self,
        payment: Payment,
        trade: TradeOrder,
        *,
        action: PaymentAction | None = None,
    ) -> PaymentView:
        events = await self.repository.events(payment.id)
        return PaymentView(
            payment_id=payment.payment_no,
            trade_order_id=trade.trade_no,
            provider=payment.provider,
            payment_method=payment.payment_method,
            payment_status=cast(PaymentStatus, payment.payment_status),
            display_status=_display_status(payment.payment_status),
            requested_amount=_money(payment.requested_amount, payment.currency),
            paid_amount=_money(payment.paid_amount, payment.currency),
            refunded_amount=_money(payment.refunded_amount, payment.currency),
            expires_at=payment.expires_at,
            paid_at=payment.paid_at,
            closed_at=payment.closed_at,
            failure_code=payment.failure_code,
            failure_message=payment.failure_message,
            action=action,
            events=[
                PaymentEventView(
                    event_id=event.event_no,
                    event_type=event.event_type,
                    from_status=event.from_status,
                    to_status=event.to_status,
                    amount=_money(event.amount, event.currency),
                    source_type=event.source_type,
                    occurred_at=event.created_at,
                )
                for event in events
            ],
            version=payment.version,
        )


def _event(
    payment: Payment,
    event_type: str,
    from_status: str | None,
    to_status: str,
    source_type: str,
    source_no: str,
    *,
    provider_occurred_at: datetime | None = None,
) -> PaymentEvent:
    return PaymentEvent(
        event_no=new_prefixed_ulid("pev_"),
        payment_id=payment.id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        amount=payment.requested_amount,
        currency=payment.currency,
        source_type=source_type,
        source_no=source_no[:64],
        provider_occurred_at=provider_occurred_at,
        trace_id=request_id_context.get(),
    )


def _order_event(
    order: Order,
    state_dimension: str,
    from_status: str,
    to_status: str,
    event_code: str,
    request_id: str,
    occurred_at: datetime,
) -> OrderStatusLog:
    return OrderStatusLog(
        order_id=order.id,
        state_dimension=state_dimension,
        from_status=from_status,
        to_status=to_status,
        event_code=event_code,
        actor_type="system",
        actor_id=None,
        reason=None,
        order_version=order.version,
        request_id=request_id,
        trace_id=request_id,
        created_at=occurred_at,
    )


def _valid_fake_signature(
    security: SecurityService,
    raw_body: bytes,
    signature: str,
    timestamp: str,
    now: datetime,
) -> bool:
    try:
        issued_at = datetime.fromtimestamp(int(timestamp), UTC).replace(tzinfo=None)
    except (OverflowError, ValueError):
        return False
    if abs((now - issued_at).total_seconds()) > 300:
        return False
    expected = security.keyed_hash(
        "fake-payment-webhook", timestamp.encode() + b"." + raw_body
    ).hex()
    return hmac.compare_digest(expected, signature)


def _webhook_mismatch(payment: Payment, payload: FakePaymentWebhook) -> str | None:
    if payment.provider != "fake":
        return "PAYMENT_PROVIDER_MISMATCH"
    if payment.provider_trade_no != payload.provider_trade_no:
        return "PAYMENT_PROVIDER_TRADE_MISMATCH"
    if payment.requested_amount != int(payload.amount_minor_units):
        return "PAYMENT_AMOUNT_MISMATCH"
    if payment.currency != payload.currency:
        return "PAYMENT_CURRENCY_MISMATCH"
    return None


def _reject_callback(
    callback: PaymentCallback,
    now: datetime,
    error_code: str,
    safe_error: str,
) -> None:
    callback.process_status = "rejected"
    callback.processed_at = now
    callback.error_code = error_code
    callback.last_error = safe_error[:1000]
    callback.version += 1


def _reject_confirmation(
    callback: PaymentCallback | None,
    now: datetime,
    error_code: str,
    safe_error: str,
) -> None:
    if callback is not None:
        _reject_callback(callback, now, error_code, safe_error)
        return
    raise _error(409, error_code, "本地交易状态或库存事实不允许应用渠道对账结果。")


def _mask_provider_trade_no(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * min(12, len(value) - 8)}{value[-4:]}"


def _money(amount: int, currency: str) -> Money:
    return Money(minor_units=str(amount), currency=currency)


def _display_status(
    status: str,
) -> Literal["confirming", "succeeded", "failed", "closed", "refunded"]:
    if status in {"created", "pending"}:
        return "confirming"
    if status in {"partially_refunded", "refunded"}:
        return "refunded"
    return cast(Literal["succeeded", "failed", "closed"], status)


def _not_found() -> ApplicationError:
    return _error(404, "RESOURCE_NOT_FOUND", "支付单不存在。")


def _error(status: int, code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=status, code=code, title="Payment error", detail=detail)
