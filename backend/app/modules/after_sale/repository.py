from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import CursorPosition
from app.modules.after_sale.models import (
    RefundAppeal,
    RefundAppealEvent,
    RefundApplication,
    RefundEvent,
    RefundItem,
    RefundPaymentRecord,
    RefundShipment,
)
from app.modules.orders.models import Order, OrderItem


class AfterSaleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def order_for_update(self, user_id: int, order_no: str) -> Order | None:
        stmt = (
            select(Order)
            .where(Order.user_id == user_id, Order.order_no == order_no)
            .with_for_update()
        )
        return cast(Order | None, await self.session.scalar(stmt))

    async def order_items_for_update(
        self, order_id: int, item_nos: Sequence[str] | None = None
    ) -> list[OrderItem]:
        stmt = (
            select(OrderItem)
            .where(OrderItem.order_id == order_id)
            .order_by(OrderItem.id)
            .with_for_update()
        )
        if item_nos:
            stmt = stmt.where(OrderItem.order_item_no.in_(item_nos))
        return list((await self.session.scalars(stmt)).all())

    async def active_items(self, item_ids: Sequence[int]) -> list[RefundItem]:
        if not item_ids:
            return []
        return list(
            (
                await self.session.scalars(
                    select(RefundItem).where(
                        RefundItem.order_item_id.in_(item_ids), RefundItem.refund_status == "active"
                    )
                )
            ).all()
        )

    async def application(
        self, user_id: int, refund_no: str, *, for_update: bool = False
    ) -> RefundApplication | None:
        stmt = select(RefundApplication).where(
            RefundApplication.user_id == user_id, RefundApplication.refund_no == refund_no
        )
        if for_update:
            stmt = stmt.with_for_update()
        return cast(RefundApplication | None, await self.session.scalar(stmt))

    async def applications(self, user_id: int, limit: int) -> list[RefundApplication]:
        return list(
            (
                await self.session.scalars(
                    select(RefundApplication)
                    .where(RefundApplication.user_id == user_id)
                    .order_by(RefundApplication.created_at.desc(), RefundApplication.id.desc())
                    .limit(limit)
                )
            ).all()
        )

    async def items_for_refund(self, refund_id: int) -> list[tuple[RefundItem, OrderItem]]:
        rows = (
            await self.session.execute(
                select(RefundItem, OrderItem)
                .join(OrderItem, OrderItem.id == RefundItem.order_item_id)
                .where(RefundItem.refund_id == refund_id)
                .order_by(RefundItem.id)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def items_for_refunds(
        self, refund_ids: Sequence[int]
    ) -> list[tuple[RefundItem, OrderItem]]:
        if not refund_ids:
            return []
        rows = (
            await self.session.execute(
                select(RefundItem, OrderItem)
                .join(OrderItem, OrderItem.id == RefundItem.order_item_id)
                .where(RefundItem.refund_id.in_(refund_ids))
                .order_by(RefundItem.refund_id, RefundItem.id)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def orders_by_ids(self, order_ids: Sequence[int]) -> list[Order]:
        if not order_ids:
            return []
        return list(
            (await self.session.scalars(select(Order).where(Order.id.in_(order_ids)))).all()
        )

    async def events(self, refund_id: int) -> list[RefundEvent]:
        return list(
            (
                await self.session.scalars(
                    select(RefundEvent)
                    .where(RefundEvent.refund_id == refund_id)
                    .order_by(RefundEvent.created_at, RefundEvent.id)
                )
            ).all()
        )

    async def admin_application(
        self, refund_no: str, *, for_update: bool = False
    ) -> RefundApplication | None:
        statement = select(RefundApplication).where(RefundApplication.refund_no == refund_no)
        if for_update:
            statement = statement.with_for_update()
        return cast(RefundApplication | None, await self.session.scalar(statement))

    async def admin_applications(
        self,
        limit: int,
        scopes: Sequence[tuple[str, int]],
        position: CursorPosition | None,
    ) -> tuple[list[RefundApplication], bool]:
        statement = select(RefundApplication)
        if ("platform", 0) not in scopes:
            store_ids = [scope_id for scope_type, scope_id in scopes if scope_type == "store"]
            if not store_ids:
                return [], False
            statement = statement.where(RefundApplication.store_id.in_(store_ids))
        if position is not None:
            if position.direction != "next" or len(position.values) != 2:
                raise ValueError("unsupported refund cursor")
            created_at = datetime.fromisoformat(position.values[0])
            refund_id = int(position.values[1])
            statement = statement.where(
                or_(
                    RefundApplication.created_at < created_at,
                    and_(
                        RefundApplication.created_at == created_at,
                        RefundApplication.id < refund_id,
                    ),
                )
            )
        rows = list(
            (
                await self.session.scalars(
                    statement.order_by(
                        RefundApplication.created_at.desc(), RefundApplication.id.desc()
                    ).limit(limit + 1)
                )
            ).all()
        )
        has_more = len(rows) > limit
        return rows[:limit], has_more

    async def admin_appeals(self, limit: int) -> list[tuple[RefundAppeal, RefundApplication]]:
        rows = (
            await self.session.execute(
                select(RefundAppeal, RefundApplication)
                .join(RefundApplication, RefundApplication.id == RefundAppeal.refund_id)
                .order_by(RefundAppeal.created_at.desc(), RefundAppeal.id.desc())
                .limit(limit)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def admin_appeal(
        self, appeal_no: str, *, for_update: bool = False
    ) -> tuple[RefundAppeal, RefundApplication] | None:
        statement = (
            select(RefundAppeal, RefundApplication)
            .join(RefundApplication, RefundApplication.id == RefundAppeal.refund_id)
            .where(RefundAppeal.appeal_no == appeal_no)
        )
        if for_update:
            statement = statement.with_for_update(of=(RefundAppeal, RefundApplication))
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1]) if row is not None else None

    async def appeal(
        self, user_id: int, appeal_no: str, *, for_update: bool = False
    ) -> RefundAppeal | None:
        statement = select(RefundAppeal).where(
            RefundAppeal.user_id == user_id,
            RefundAppeal.appeal_no == appeal_no,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(RefundAppeal | None, await self.session.scalar(statement))

    async def appeal_events(self, appeal_id: int) -> list[RefundAppealEvent]:
        return list(
            (
                await self.session.scalars(
                    select(RefundAppealEvent)
                    .where(RefundAppealEvent.appeal_id == appeal_id)
                    .order_by(RefundAppealEvent.created_at, RefundAppealEvent.id)
                )
            ).all()
        )

    async def refund_payment_by_no(
        self, payment_no: str, *, for_update: bool = False
    ) -> RefundPaymentRecord | None:
        statement = select(RefundPaymentRecord).where(
            RefundPaymentRecord.refund_payment_no == payment_no
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(RefundPaymentRecord | None, await self.session.scalar(statement))

    async def return_shipment(
        self, refund_id: int, *, for_update: bool = False
    ) -> RefundShipment | None:
        statement = select(RefundShipment).where(RefundShipment.refund_id == refund_id)
        if for_update:
            statement = statement.with_for_update()
        return cast(RefundShipment | None, await self.session.scalar(statement))
