from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.modules.catalog.models import ProductFulfillmentProfile
from app.modules.logistics.models import (
    LogisticsSyncLog,
    Shipment,
    ShipmentItem,
    ShipmentTrack,
)
from app.modules.orders.models import Order, OrderAddress, OrderItem
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

    async def latest_sync(
        self,
        shipment_id: int,
        *,
        sync_type: str | None = None,
        sync_statuses: tuple[str, ...] | None = None,
    ) -> LogisticsSyncLog | None:
        statement = select(LogisticsSyncLog).where(LogisticsSyncLog.shipment_id == shipment_id)
        if sync_type is not None:
            statement = statement.where(LogisticsSyncLog.sync_type == sync_type)
        if sync_statuses is not None:
            statement = statement.where(LogisticsSyncLog.sync_status.in_(sync_statuses))
        return cast(
            LogisticsSyncLog | None,
            await self.session.scalar(
                statement.order_by(
                    LogisticsSyncLog.created_at.desc(), LogisticsSyncLog.id.desc()
                ).limit(1)
            ),
        )

    async def track_by_provider_event(
        self, shipment_id: int, provider_event_id: str
    ) -> ShipmentTrack | None:
        return cast(
            ShipmentTrack | None,
            await self.session.scalar(
                select(ShipmentTrack).where(
                    ShipmentTrack.shipment_id == shipment_id,
                    ShipmentTrack.provider_event_id == provider_event_id,
                )
            ),
        )

    async def sync_candidates(
        self,
        *,
        now: datetime,
        stale_before: datetime,
        limit: int,
        simulated_stale_before: datetime | None = None,
    ) -> list[Shipment]:
        simulated_threshold = simulated_stale_before or stale_before
        latest_log = aliased(LogisticsSyncLog)
        latest_log_id = (
            select(func.max(LogisticsSyncLog.id))
            .where(LogisticsSyncLog.shipment_id == Shipment.id)
            .correlate(Shipment)
            .scalar_subquery()
        )
        return list(
            (
                await self.session.scalars(
                    select(Shipment)
                    .outerjoin(latest_log, latest_log.id == latest_log_id)
                    .where(
                        Shipment.shipment_status.in_(
                            {"created", "picked_up", "in_transit", "exception"}
                        ),
                        or_(
                            latest_log.id.is_(None),
                            and_(
                                Shipment.carrier_code == "fake_express",
                                latest_log.created_at < simulated_threshold,
                            ),
                            and_(
                                Shipment.carrier_code != "fake_express",
                                latest_log.created_at < stale_before,
                            ),
                            and_(
                                latest_log.sync_status == "retry",
                                latest_log.next_retry_at.is_not(None),
                                latest_log.next_retry_at <= now,
                            ),
                        ),
                    )
                    .order_by(
                        latest_log.next_retry_at.is_(None),
                        latest_log.next_retry_at,
                        Shipment.last_track_at,
                        Shipment.id,
                    )
                    .limit(limit)
                )
            ).all()
        )

    async def automatic_shipment_candidates(self, limit: int) -> list[str]:
        active_shipment_exists = exists(
            select(Shipment.id).where(
                Shipment.order_id == Order.id,
                Shipment.shipment_status != "voided",
            )
        )
        return list(
            (
                await self.session.scalars(
                    select(Order.order_no)
                    .where(
                        Order.payment_status.in_({"paid", "partially_refunded"}),
                        Order.order_status == "pending_shipment",
                        Order.fulfillment_status == "unfulfilled",
                        Order.after_sale_status != "in_progress",
                        ~active_shipment_exists,
                    )
                    .order_by(Order.paid_at, Order.id)
                    .limit(limit)
                )
            ).all()
        )

    async def order_address(self, order_id: int) -> OrderAddress | None:
        return cast(
            OrderAddress | None,
            await self.session.scalar(
                select(OrderAddress).where(OrderAddress.order_id == order_id)
            ),
        )

    async def shipment_origin_region_code(self, shipment_id: int) -> str | None:
        return cast(
            str | None,
            await self.session.scalar(
                select(ProductFulfillmentProfile.origin_region_code)
                .join(OrderItem, OrderItem.product_id == ProductFulfillmentProfile.product_id)
                .join(ShipmentItem, ShipmentItem.order_item_id == OrderItem.id)
                .where(ShipmentItem.shipment_id == shipment_id)
                .order_by(ShipmentItem.id)
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
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1], row[2]) if row else None
