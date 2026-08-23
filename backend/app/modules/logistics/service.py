from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PaginationMeta
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.pagination import CursorCodec
from app.core.security import SecurityService, utc_now
from app.modules.identity.models import User
from app.modules.logistics.models import (
    LogisticsSyncLog,
    Shipment,
    ShipmentItem,
    ShipmentTrack,
)
from app.modules.logistics.repository import LogisticsRepository
from app.modules.logistics.schemas import (
    DeliveryEstimate,
    ShipmentItemView,
    ShipmentRefreshResult,
    ShipmentStatus,
    ShipmentTrackList,
    ShipmentTrackView,
    UserOrderShipmentList,
    UserOrderShipmentSummary,
    UserShipmentDetail,
)
from app.modules.orders.models import OrderItem
from app.modules.system.models import OutboxEvent


class LogisticsService:
    def __init__(
        self,
        session: AsyncSession,
        security: SecurityService,
        cursor_secret: str,
    ) -> None:
        self.session = session
        self.security = security
        self.repository = LogisticsRepository(session)
        self.idempotency = IdempotencyService(session)
        self.cursor = CursorCodec(cursor_secret)

    async def list_for_order(self, user: User, order_no: str) -> UserOrderShipmentList:
        order = await self.repository.user_order(user.id, order_no)
        if order is None:
            raise _not_found()
        shipments = await self.repository.order_shipments(order.id)
        items = await self.repository.shipment_items([item.id for item in shipments])
        latest_tracks = await self.repository.latest_tracks([item.id for item in shipments])
        summaries: list[UserOrderShipmentSummary] = []
        for shipment in shipments:
            latest_sync = await self.repository.latest_sync(shipment.id)
            summaries.append(
                UserOrderShipmentSummary(
                    shipment_id=shipment.shipment_no,
                    carrier_code=shipment.carrier_code,
                    carrier_name=shipment.carrier_name,
                    tracking_no_masked=shipment.tracking_no_masked,
                    shipment_status=cast(ShipmentStatus, shipment.shipment_status),
                    items=_items(items.get(shipment.id, [])),
                    delivery_estimate=_estimate(shipment),
                    last_track=_track(latest_tracks[shipment.id])
                    if shipment.id in latest_tracks
                    else None,
                    last_synced_at=latest_sync.created_at if latest_sync else None,
                )
            )
        return UserOrderShipmentList(order_id=order.order_no, items=summaries)

    async def detail(self, user: User, shipment_no: str) -> UserShipmentDetail:
        row = await self.repository.user_shipment(user.id, shipment_no)
        if row is None or row[0].shipment_status == "voided":
            raise _not_found()
        shipment, order = row
        item_map = await self.repository.shipment_items([shipment.id])
        tracks = await self.repository.recent_tracks(shipment.id, limit=5)
        latest_sync = await self.repository.latest_sync(shipment.id)
        return UserShipmentDetail(
            shipment_id=shipment.shipment_no,
            order_id=order.order_no,
            carrier_code=shipment.carrier_code,
            carrier_name=shipment.carrier_name,
            tracking_no=self.security.decrypt(
                "shipment-tracking-no", shipment.tracking_no_ciphertext
            ),
            tracking_no_masked=shipment.tracking_no_masked,
            shipment_status=cast(ShipmentStatus, shipment.shipment_status),
            items=_items(item_map.get(shipment.id, [])),
            delivery_estimate=_estimate(shipment),
            latest_tracks=[_track(item) for item in tracks],
            last_synced_at=latest_sync.created_at if latest_sync else None,
            version=shipment.version,
        )

    async def tracks(
        self,
        user: User,
        shipment_no: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> tuple[ShipmentTrackList, PaginationMeta]:
        row = await self.repository.user_shipment(user.id, shipment_no)
        if row is None or row[0].shipment_status == "voided":
            raise _not_found()
        shipment, _ = row
        position = self.cursor.decode(cursor, filter_key=f"shipment-tracks:{shipment_no}")
        after: tuple[datetime, int] | None = None
        if position is not None:
            if position.direction != "next" or len(position.values) != 2:
                raise _invalid_cursor()
            try:
                after = (datetime.fromisoformat(position.values[0]), int(position.values[1]))
            except (ValueError, TypeError) as exc:
                raise _invalid_cursor() from exc
        rows, has_more = await self.repository.tracks(shipment.id, after=after, limit=limit)
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = self.cursor.encode(
                filter_key=f"shipment-tracks:{shipment_no}",
                values=(last.occurred_at.isoformat(), str(last.id)),
            )
        return (
            ShipmentTrackList(
                shipment_id=shipment.shipment_no,
                items=[_track(item) for item in rows],
            ),
            PaginationMeta(
                next_cursor=next_cursor,
                has_next=has_more,
                has_previous=position is not None,
                limit=limit,
            ),
        )

    async def request_refresh(
        self,
        user: User,
        shipment_no: str,
        idempotency_key: str,
    ) -> ShipmentRefreshResult:
        claim = await self.idempotency.begin(
            scope_key=f"shipment:refresh:{user.user_no}:{shipment_no}",
            idempotency_key=idempotency_key,
            payload={"shipment_id": shipment_no},
            resource_type="shipment_refresh",
            ttl_days=1,
        )
        if claim.replayed and claim.record.response_body is not None:
            return ShipmentRefreshResult.model_validate(claim.record.response_body)
        row = await self.repository.user_shipment(user.id, shipment_no)
        if row is None or row[0].shipment_status in {"voided", "closed"}:
            raise _not_found()
        shipment, _ = row
        now = utc_now()
        latest = await self.repository.latest_sync(shipment.id)
        if latest is not None and latest.created_at > now - timedelta(seconds=60):
            raise ApplicationError(
                status=429,
                code="SHIPMENT_REFRESH_RATE_LIMITED",
                title="Shipment refresh rate limited",
                detail="物流刷新过于频繁，请稍后再试。",
                headers={"Retry-After": "60"},
                retryable=True,
            )
        request_id = request_id_context.get()
        self.session.add(
            LogisticsSyncLog(
                shipment_id=shipment.id,
                sync_type="poll",
                sync_status="retry",
                track_count=0,
                attempt_count=0,
                duration_ms=0,
                next_retry_at=now,
                trace_id=request_id,
            )
        )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="shipment.refresh_requested.v1",
                aggregate_type="shipment",
                aggregate_no=shipment.shipment_no,
                aggregate_version=shipment.version,
                payload={"shipment_id": shipment.shipment_no},
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=request_id,
            )
        )
        result = ShipmentRefreshResult(shipment_id=shipment.shipment_no, requested_at=now)
        self.idempotency.complete(
            claim,
            response_status=202,
            resource_no=shipment.shipment_no,
            response_body=result.model_dump(mode="json"),
        )
        await self.session.commit()
        return result


def _items(rows: list[tuple[ShipmentItem, OrderItem]]) -> list[ShipmentItemView]:
    result: list[ShipmentItemView] = []
    for shipment_item, order_item in rows:
        result.append(
            ShipmentItemView(
                order_item_id=order_item.order_item_no,
                product_name=order_item.product_name,
                sku_name=order_item.sku_name,
                quantity=shipment_item.quantity,
            )
        )
    return result


def _estimate(shipment: Shipment) -> DeliveryEstimate:
    available = (
        shipment.estimated_delivery_min_at is not None
        and shipment.estimated_delivery_max_at is not None
        and shipment.estimate_source in {"shipping_template", "carrier"}
    )
    if not available:
        return DeliveryEstimate(status="unavailable")
    return DeliveryEstimate(
        status="available",
        min_at=shipment.estimated_delivery_min_at,
        max_at=shipment.estimated_delivery_max_at,
        source=cast(Literal["shipping_template", "carrier"], shipment.estimate_source),
        updated_at=shipment.estimate_updated_at,
    )


def _track(track: ShipmentTrack) -> ShipmentTrackView:
    return ShipmentTrackView(
        track_status=track.track_status,
        description=track.description,
        location_text=track.location_text,
        occurred_at=track.occurred_at,
        received_at=track.created_at,
    )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="物流信息不存在。",
    )


def _invalid_cursor() -> ApplicationError:
    return ApplicationError(
        status=400,
        code="PAGINATION_CURSOR_INVALID",
        title="Invalid pagination cursor",
        detail="分页位置无效，请重新加载物流轨迹。",
    )
