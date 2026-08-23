from __future__ import annotations

from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import SecurityService, utc_now
from app.integrations.payments import PaymentProviderRequest, payment_provider
from app.modules.catalog.schemas import Money
from app.modules.identity.models import User
from app.modules.orders.models import OrderStatusLog, TradeOrder
from app.modules.payments.models import Payment, PaymentEvent
from app.modules.payments.repository import PaymentRepository
from app.modules.payments.schemas import (
    PaymentAction,
    PaymentCreateRequest,
    PaymentEventView,
    PaymentList,
    PaymentStatus,
    PaymentView,
)
from app.modules.system.models import OutboxEvent


class PaymentService:
    def __init__(self, session: AsyncSession, security: SecurityService) -> None:
        self.session = session
        self.security = security
        self.repository = PaymentRepository(session)
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

    async def list_for_trade(self, user: User, trade_no: str) -> PaymentList:
        trade = await self.repository.user_trade(user.id, trade_no)
        if trade is None:
            raise _not_found()
        payments = await self.repository.for_trade(user.id, trade_no)
        return PaymentList(items=[await self._view(item, trade) for item in payments])

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
        source_no=source_no,
        trace_id=request_id_context.get(),
    )


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
