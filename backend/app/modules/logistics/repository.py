from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.logistics.models import (
    LogisticsSyncLog,
    Shipment,
    ShipmentItem,
    ShipmentTrack,
)
from app.modules.orders.models import Order, OrderItem
from app.modules.stores.models import Store


class LogisticsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def user_order(self, user_id: int, order_no: str) -> Order | None:
        return cast(
            Order | None,
            await self.session.scalar(
                select(Order).where(
                    Order.user_id == user_id,
                    Order.order_no == order_no,
                    Order.user_hidden_at.is_(None),
                )
            ),
        )

    async def user_shipment(self, user_id: int, shipment_no: str) -> tuple[Shipment, Order] | None:
        row = (
            await self.session.execute(
                select(Shipment, Order)
                .join(Order, Order.id == Shipment.order_id)
                .where(
                    Shipment.shipment_no == shipment_no,
                    Order.user_id == user_id,
                    Order.user_hidden_at.is_(None),
                )
            )
        ).one_or_none()
        return (row[0], row[1]) if row else None

    async def order_shipments(self, order_id: int) -> list[Shipment]:
        return list(
            (
                await self.session.scalars(
                    select(Shipment)
                    .where(Shipment.order_id == order_id, Shipment.shipment_status != "voided")
                    .order_by(Shipment.created_at, Shipment.id)
                )
            ).all()
        )

    async def shipment_items(
        self, shipment_ids: list[int]
    ) -> dict[int, list[tuple[ShipmentItem, OrderItem]]]:
        if not shipment_ids:
            return {}
        rows = list(
            (
                await self.session.execute(
                    select(ShipmentItem, OrderItem)
                    .join(OrderItem, OrderItem.id == ShipmentItem.order_item_id)
                    .where(ShipmentItem.shipment_id.in_(shipment_ids))
                    .order_by(ShipmentItem.shipment_id, ShipmentItem.id)
                )
            ).all()
        )
        result: dict[int, list[tuple[ShipmentItem, OrderItem]]] = {}
        for shipment_item, order_item in rows:
            result.setdefault(shipment_item.shipment_id, []).append((shipment_item, order_item))
        return result

    async def latest_tracks(self, shipment_ids: list[int]) -> dict[int, ShipmentTrack]:
        if not shipment_ids:
            return {}
        ranked = (
            select(
                ShipmentTrack.id.label("track_id"),
                func.row_number()
                .over(
                    partition_by=ShipmentTrack.shipment_id,
                    order_by=(
                        ShipmentTrack.occurred_at.desc(),
                        ShipmentTrack.id.desc(),
                    ),
                )
                .label("row_number"),
            )
            .where(ShipmentTrack.shipment_id.in_(shipment_ids))
            .subquery()
        )
        rows = list(
            (
                await self.session.scalars(
                    select(ShipmentTrack)
                    .join(ranked, ranked.c.track_id == ShipmentTrack.id)
                    .where(ranked.c.row_number == 1)
                    .order_by(ShipmentTrack.shipment_id)
                )
            ).all()
        )
        return {track.shipment_id: track for track in rows}

    async def recent_tracks(self, shipment_id: int, limit: int) -> list[ShipmentTrack]:
        rows = list(
            (
                await self.session.scalars(
                    select(ShipmentTrack)
                    .where(ShipmentTrack.shipment_id == shipment_id)
                    .order_by(ShipmentTrack.occurred_at.desc(), ShipmentTrack.id.desc())
                    .limit(limit)
                )
            ).all()
        )
        rows.reverse()
        return rows

    async def tracks(
        self,
        shipment_id: int,
        *,
        after: tuple[datetime, int] | None,
        limit: int,
    ) -> tuple[list[ShipmentTrack], bool]:
        statement = select(ShipmentTrack).where(ShipmentTrack.shipment_id == shipment_id)
        if after is not None:
            statement = statement.where(
                or_(
                    ShipmentTrack.occurred_at > after[0],
                    and_(
                        ShipmentTrack.occurred_at == after[0],
                        ShipmentTrack.id > after[1],
                    ),
                )
            )
        rows = list(
            (
                await self.session.scalars(
                    statement.order_by(ShipmentTrack.occurred_at, ShipmentTrack.id).limit(limit + 1)
                )
            ).all()
        )
        return rows[:limit], len(rows) > limit

    async def latest_sync(self, shipment_id: int) -> LogisticsSyncLog | None:
        return cast(
            LogisticsSyncLog | None,
            await self.session.scalar(
                select(LogisticsSyncLog)
                .where(LogisticsSyncLog.shipment_id == shipment_id)
                .order_by(LogisticsSyncLog.created_at.desc(), LogisticsSyncLog.id.desc())
                .limit(1)
            ),
        )

    async def admin_order(
        self, order_no: str, *, for_update: bool = False
    ) -> tuple[Order, Store] | None:
        statement = (
            select(Order, Store)
            .join(Store, Store.id == Order.store_id)
            .where(Order.order_no == order_no)
        )
        if for_update:
            statement = statement.with_for_update(of=Order)
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1]) if row else None

    async def order_items_for_update(self, order_id: int) -> list[OrderItem]:
        return list(
            (
                await self.session.scalars(
                    select(OrderItem)
                    .where(OrderItem.order_id == order_id)
                    .order_by(OrderItem.id)
                    .with_for_update()
                )
            ).all()
        )

    async def allocated_quantities(self, order_id: int) -> dict[int, int]:
        rows = (
            await self.session.execute(
                select(ShipmentItem.order_item_id, func.sum(ShipmentItem.quantity))
                .join(Shipment, Shipment.id == ShipmentItem.shipment_id)
                .where(
                    Shipment.order_id == order_id,
                    Shipment.shipment_status != "voided",
                )
                .group_by(ShipmentItem.order_item_id)
            )
        ).all()
        return {int(row[0]): int(row[1]) for row in rows}

    async def shipment_by_tracking_hash(
        self, carrier_code: str, tracking_hash: bytes
    ) -> Shipment | None:
        return cast(
            Shipment | None,
            await self.session.scalar(
                select(Shipment).where(
                    Shipment.carrier_code == carrier_code,
                    Shipment.tracking_no_hash == tracking_hash,
                )
            ),
        )

    async def shipment_by_no(
        self, shipment_no: str, *, for_update: bool = False
    ) -> tuple[Shipment, Order, Store] | None:
        statement = (
            select(Shipment, Order, Store)
            .join(Order, Order.id == Shipment.order_id)
            .join(Store, Store.id == Shipment.store_id)
            .where(Shipment.shipment_no == shipment_no)
        )
        if for_update:
            statement = statement.with_for_update(of=Shipment)
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1], row[2]) if row else None
