from collections.abc import Sequence
from datetime import datetime
from typing import cast

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
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

    async def expired_active(self, now: datetime, limit: int) -> list[Payment]:
        return list(
            (
                await self.session.scalars(
                    select(Payment)
                    .where(
                        Payment.payment_status.in_(("created", "pending")),
                        Payment.expires_at <= now,
                    )
                    .order_by(Payment.expires_at, Payment.id)
                    .limit(limit)
                )
            ).all()
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

    async def admin_payments(
        self,
        *,
        scopes: Sequence[tuple[str, int]],
        query: str | None,
        payment_status: str | None,
        provider: str | None,
        limit: int,
    ) -> list[tuple[Payment, TradeOrder]]:
        statement = select(Payment, TradeOrder).join(
            TradeOrder, TradeOrder.id == Payment.trade_order_id
        )
        if ("platform", 0) not in scopes:
            store_ids = [scope_id for scope_type, scope_id in scopes if scope_type == "store"]
            if not store_ids:
                return []
            statement = statement.where(
                exists().where(
                    Order.trade_order_id == Payment.trade_order_id,
                    Order.store_id.in_(store_ids),
                ),
                ~exists().where(
                    Order.trade_order_id == Payment.trade_order_id,
                    Order.store_id.not_in(store_ids),
                ),
            )
        if query:
            pattern = f"%{query}%"
            statement = statement.where(
                or_(Payment.payment_no.like(pattern), TradeOrder.trade_no.like(pattern))
            )
        if payment_status:
            statement = statement.where(Payment.payment_status == payment_status)
        if provider:
            statement = statement.where(Payment.provider == provider)
        rows = (
            await self.session.execute(
                statement.order_by(Payment.created_at.desc(), Payment.id.desc()).limit(limit)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def admin_by_no(self, payment_no: str) -> tuple[Payment, TradeOrder] | None:
        row = (
            await self.session.execute(
                select(Payment, TradeOrder)
                .join(TradeOrder, TradeOrder.id == Payment.trade_order_id)
                .where(Payment.payment_no == payment_no)
            )
        ).one_or_none()
        return (row[0], row[1]) if row else None

    async def trade_stores(self, trade_order_id: int) -> list[tuple[int, str]]:
        from app.modules.stores.models import Store

        rows = (
            await self.session.execute(
                select(Store.id, Store.store_no)
                .join(Order, Order.store_id == Store.id)
                .where(Order.trade_order_id == trade_order_id)
                .distinct()
                .order_by(Store.id)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def user_no(self, user_id: int) -> str | None:
        return cast(
            str | None,
            await self.session.scalar(select(User.user_no).where(User.id == user_id)),
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
