from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models import Order, TradeOrder
from app.modules.payments.models import Payment, PaymentCallback, PaymentEvent


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def user_trade_for_update(self, user_id: int, trade_no: str) -> TradeOrder | None:
        return cast(
            TradeOrder | None,
            await self.session.scalar(
                select(TradeOrder)
                .where(TradeOrder.user_id == user_id, TradeOrder.trade_no == trade_no)
                .with_for_update()
            ),
        )

    async def user_trade(self, user_id: int, trade_no: str) -> TradeOrder | None:
        return cast(
            TradeOrder | None,
            await self.session.scalar(
                select(TradeOrder).where(
                    TradeOrder.user_id == user_id, TradeOrder.trade_no == trade_no
                )
            ),
        )

    async def active_for_trade(self, trade_order_id: int) -> Payment | None:
        return cast(
            Payment | None,
            await self.session.scalar(
                select(Payment)
                .where(
                    Payment.trade_order_id == trade_order_id,
                    Payment.payment_status.in_(("created", "pending")),
                )
                .order_by(Payment.id.desc())
            ),
        )

    async def untrusted_by_no(self, payment_no: str) -> Payment | None:
        return cast(
            Payment | None,
            await self.session.scalar(select(Payment).where(Payment.payment_no == payment_no)),
        )

    async def payment_for_update(self, payment_id: int) -> Payment | None:
        return cast(
            Payment | None,
            await self.session.scalar(
                select(Payment).where(Payment.id == payment_id).with_for_update()
            ),
        )

    async def trade_for_update(self, trade_order_id: int) -> TradeOrder | None:
        return cast(
            TradeOrder | None,
            await self.session.scalar(
                select(TradeOrder).where(TradeOrder.id == trade_order_id).with_for_update()
            ),
        )

    async def by_no(self, user_id: int, payment_no: str) -> tuple[Payment, TradeOrder] | None:
        row = (
            await self.session.execute(
                select(Payment, TradeOrder)
                .join(TradeOrder, TradeOrder.id == Payment.trade_order_id)
                .where(Payment.user_id == user_id, Payment.payment_no == payment_no)
            )
        ).one_or_none()
        return (row[0], row[1]) if row else None

    async def for_trade(self, user_id: int, trade_no: str) -> list[Payment]:
        return list(
            (
                await self.session.scalars(
                    select(Payment)
                    .join(TradeOrder, TradeOrder.id == Payment.trade_order_id)
                    .where(TradeOrder.user_id == user_id, TradeOrder.trade_no == trade_no)
                    .order_by(Payment.created_at.desc(), Payment.id.desc())
                )
            ).all()
        )

    async def events(self, payment_id: int) -> list[PaymentEvent]:
        return list(
            (
                await self.session.scalars(
                    select(PaymentEvent)
                    .where(PaymentEvent.payment_id == payment_id)
                    .order_by(PaymentEvent.created_at, PaymentEvent.id)
                )
            ).all()
        )

    async def callback_by_provider_event(
        self, provider: str, provider_event_id: str
    ) -> PaymentCallback | None:
        return cast(
            PaymentCallback | None,
            await self.session.scalar(
                select(PaymentCallback).where(
                    PaymentCallback.provider == provider,
                    PaymentCallback.provider_event_id == provider_event_id,
                )
            ),
        )

    async def trade_orders_for_update(self, trade_order_id: int) -> list[Order]:
        return list(
            (
                await self.session.scalars(
                    select(Order)
                    .where(Order.trade_order_id == trade_order_id)
                    .order_by(Order.id)
                    .with_for_update()
                )
            ).all()
        )

    async def mark_trade_orders_processing(self, trade_order_id: int) -> list[Order]:
        orders = list(
            (
                await self.session.scalars(
                    select(Order)
                    .where(Order.trade_order_id == trade_order_id)
                    .order_by(Order.id)
                    .with_for_update()
                )
            ).all()
        )
        for order in orders:
            order.payment_status = "processing"
            order.version += 1
        return orders
