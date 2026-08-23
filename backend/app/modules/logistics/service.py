from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PaginationMeta
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.pagination import CursorCodec
from app.core.security import SecurityService, canonical_request_hash, utc_now
from app.modules.identity.models import User
from app.modules.logistics.models import (
    LogisticsSyncLog,
    Shipment,
    ShipmentItem,
    ShipmentTrack,
)
from app.modules.logistics.repository import LogisticsRepository
from app.modules.logistics.schemas import (
    AdminShipmentCreateRequest,
    AdminShipmentDetail,
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
from app.modules.orders.domain import (
    FULFILLMENT_TRANSITIONS,
    ORDER_TRANSITIONS,
    require_transition,
)
from app.modules.orders.models import Order, OrderItem, OrderOperationLog, OrderStatusLog
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.stores.models import Store
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

    async def admin_detail(self, access: AdminAccess, shipment_no: str) -> AdminShipmentDetail:
        row = await self.repository.shipment_by_no(shipment_no)
        if row is None:
            raise _not_found()
        shipment, order, store = row
        access.require_scope("store", store.id)
        return await self._admin_view(shipment, order, store)

    async def create_shipment(
        self,
        access: AdminAccess,
        order_no: str,
        payload: AdminShipmentCreateRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminShipmentDetail:
        claim = await self.idempotency.begin(
            scope_key=(f"admin:shipment-create:{access.context.user.user_no}:{order_no}"),
            idempotency_key=idempotency_key,
            payload={
                "order_id": order_no,
                "expected_version": expected_version,
                **payload.model_dump(mode="json"),
            },
            resource_type="shipment",
        )
        if claim.replayed and claim.record.resource_no is not None:
            previous = await self.repository.shipment_by_no(claim.record.resource_no)
            if previous is None:
                raise ApplicationError(
                    status=409,
                    code="IDEMPOTENCY_RESULT_UNAVAILABLE",
                    title="Idempotency result unavailable",
                    detail="原发货结果暂不可用。",
                )
            shipment, order, store = previous
            access.require_scope("store", store.id)
            return await self._admin_view(shipment, order, store)

        row = await self.repository.admin_order(order_no, for_update=True)
        if row is None:
            raise _not_found()
        order, store = row
        access.require_scope("store", store.id)
        _require_version(order.version, expected_version)
        if (
            order.payment_status != "paid"
            or order.order_status != "pending_shipment"
            or order.fulfillment_status not in {"unfulfilled", "partial"}
        ):
            raise ApplicationError(
                status=409,
                code="ORDER_NOT_SHIPPABLE",
                title="Order is not shippable",
                detail="订单未支付、已全部发货或当前状态不允许发货。",
            )
        if order.after_sale_status == "in_progress":
            raise ApplicationError(
                status=409,
                code="ORDER_SHIPMENT_BLOCKED_BY_AFTER_SALE",
                title="Shipment blocked by after-sale",
                detail="订单存在阻止发货的售后流程。",
            )

        normalized_tracking = _normalize_tracking_no(payload.tracking_no)
        tracking_hash = self.security.keyed_hash(
            "shipment-tracking-no",
            f"{payload.carrier_code}:{normalized_tracking}",
        )
        duplicate = await self.repository.shipment_by_tracking_hash(
            payload.carrier_code, tracking_hash
        )
        if duplicate is not None:
            raise ApplicationError(
                status=409,
                code="SHIPMENT_TRACKING_ALREADY_EXISTS",
                title="Tracking number already exists",
                detail="该承运商运单已经用于其他包裹。",
            )

        order_items = await self.repository.order_items_for_update(order.id)
        by_no = {item.order_item_no: item for item in order_items}
        requested = {item.order_item_id: item.quantity for item in payload.items}
        if set(requested) - set(by_no):
            raise ApplicationError(
                status=422,
                code="SHIPMENT_ORDER_ITEM_INVALID",
                title="Shipment order item invalid",
                detail="包裹包含不属于当前订单的商品项。",
            )
        allocated = await self.repository.allocated_quantities(order.id)
        for item_no, quantity in requested.items():
            order_item = by_no[item_no]
            remaining = (
                order_item.quantity - order_item.refunded_quantity - allocated.get(order_item.id, 0)
            )
            if quantity > remaining:
                raise ApplicationError(
                    status=409,
                    code="SHIPMENT_ITEM_QUANTITY_EXCEEDED",
                    title="Shipment quantity exceeded",
                    detail=f"订单项 {item_no} 的可发数量已变化。",
                )

        now = utc_now()
        estimate = _snapshot_delivery_estimate(order, now)
        shipment = Shipment(
            shipment_no=new_prefixed_ulid("shp_"),
            order_id=order.id,
            store_id=store.id,
            carrier_code=payload.carrier_code,
            carrier_name=payload.carrier_name,
            tracking_no_ciphertext=self.security.encrypt(
                "shipment-tracking-no", normalized_tracking
            ),
            tracking_no_hash=tracking_hash,
            tracking_no_masked=_mask_tracking_no(normalized_tracking),
            shipment_status="created",
            estimated_delivery_min_at=estimate[0],
            estimated_delivery_max_at=estimate[1],
            estimate_source="shipping_template" if estimate[0] else None,
            estimate_updated_at=estimate[2],
            shipped_at=now,
            key_version=1,
        )
        self.session.add(shipment)
        await self.session.flush()
        for item_no, quantity in requested.items():
            self.session.add(
                ShipmentItem(
                    shipment_id=shipment.id,
                    order_item_id=by_no[item_no].id,
                    quantity=quantity,
                )
            )

        allocated_after = {
            item.id: allocated.get(item.id, 0) + requested.get(item.order_item_no, 0)
            for item in order_items
        }
        all_shipped = all(
            allocated_after[item.id] >= item.quantity - item.refunded_quantity
            for item in order_items
        )
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        previous_fulfillment = order.fulfillment_status
        previous_order_status = order.order_status
        if all_shipped:
            order.fulfillment_status = require_transition(
                FULFILLMENT_TRANSITIONS,
                previous_fulfillment,
                "RecordAllItemsShipped",
            )
            order.version += 1
            self.session.add(
                _order_status_log(
                    order,
                    "fulfillment",
                    previous_fulfillment,
                    "shipped",
                    "fulfillment.shipped",
                    access.context.user.id,
                    request_id,
                    now,
                )
            )
            order.order_status = require_transition(
                ORDER_TRANSITIONS,
                previous_order_status,
                "RecordAllItemsShipped",
            )
            order.shipped_at = now
            order.version += 1
            self.session.add(
                _order_status_log(
                    order,
                    "order",
                    previous_order_status,
                    "shipped",
                    "order.shipped",
                    access.context.user.id,
                    request_id,
                    now,
                )
            )
        else:
            if previous_fulfillment == "unfulfilled":
                order.fulfillment_status = require_transition(
                    FULFILLMENT_TRANSITIONS,
                    previous_fulfillment,
                    "RecordPartialShipment",
                )
                order.version += 1
                self.session.add(
                    _order_status_log(
                        order,
                        "fulfillment",
                        previous_fulfillment,
                        "partial",
                        "fulfillment.partial",
                        access.context.user.id,
                        request_id,
                        now,
                    )
                )
            else:
                order.version += 1

        self.session.add(
            OrderOperationLog(
                operation_no=new_prefixed_ulid("oop_"),
                order_id=order.id,
                operation_type="create_shipment",
                actor_type="admin",
                actor_id=access.context.user.id,
                request_payload_hash=self.security.keyed_hash(
                    "shipment-create-request",
                    canonical_request_hash(payload.model_dump(mode="json")),
                ),
                result_status="success",
                request_id=request_id,
                trace_id=request_id,
            )
        )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="shipment.created.v1",
                aggregate_type="shipment",
                aggregate_no=shipment.shipment_no,
                aggregate_version=shipment.version,
                payload={
                    "shipment_id": shipment.shipment_no,
                    "order_id": order.order_no,
                    "store_id": store.store_no,
                    "carrier_code": shipment.carrier_code,
                    "tracking_no_masked": shipment.tracking_no_masked,
                    "all_items_shipped": all_shipped,
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
            action="create_shipment",
            target_type="shipment",
            target_no=shipment.shipment_no,
            before={
                "order_status": previous_order_status,
                "fulfillment_status": previous_fulfillment,
                "order_version": expected_version,
            },
            after={
                "order_status": order.order_status,
                "fulfillment_status": order.fulfillment_status,
                "order_version": order.version,
                "tracking_no_masked": shipment.tracking_no_masked,
            },
            scope_type="store",
            scope_id=store.id,
        )
        await self.session.flush()
        result = await self._admin_view(shipment, order, store)
        self.idempotency.complete(
            claim,
            response_status=201,
            resource_no=shipment.shipment_no,
            response_body=result.model_dump(mode="json"),
        )
        await self.session.commit()
        return result

    async def _admin_view(
        self, shipment: Shipment, order: Order, store: Store
    ) -> AdminShipmentDetail:
        item_map = await self.repository.shipment_items([shipment.id])
        tracks = await self.repository.recent_tracks(shipment.id, limit=20)
        latest_sync = await self.repository.latest_sync(shipment.id)
        if shipment.shipped_at is None:
            raise RuntimeError(f"shipment {shipment.shipment_no} has no shipped_at")
        return AdminShipmentDetail(
            shipment_id=shipment.shipment_no,
            order_id=order.order_no,
            store_id=store.store_no,
            carrier_code=shipment.carrier_code,
            carrier_name=shipment.carrier_name,
            tracking_no_masked=shipment.tracking_no_masked,
            shipment_status=cast(ShipmentStatus, shipment.shipment_status),
            items=_items(item_map.get(shipment.id, [])),
            delivery_estimate=_estimate(shipment),
            latest_tracks=[_track(item) for item in tracks],
            shipped_at=shipment.shipped_at,
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


def _normalize_tracking_no(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().upper()
    normalized = normalized.replace(" ", "").replace("-", "")
    if not re.fullmatch(r"[A-Z0-9]{6,64}", normalized):
        raise ApplicationError(
            status=422,
            code="SHIPMENT_TRACKING_NUMBER_INVALID",
            title="Tracking number invalid",
            detail="运单号格式不正确。",
        )
    return normalized


def _mask_tracking_no(value: str) -> str:
    return f"{'*' * max(4, len(value) - 4)}{value[-4:]}"


def _snapshot_delivery_estimate(
    order: Order, now: datetime
) -> tuple[datetime | None, datetime | None, datetime | None]:
    raw_option = order.policy_snapshot.get("delivery_option")
    if not isinstance(raw_option, dict):
        return None, None, None
    raw_estimate = raw_option.get("estimate")
    if not isinstance(raw_estimate, dict) or raw_estimate.get("status") != "available":
        return None, None, None
    if raw_estimate.get("source") != "shipping_template":
        return None, None, None
    try:
        minimum = datetime.fromisoformat(str(raw_estimate["min_at"]))
        maximum = datetime.fromisoformat(str(raw_estimate["max_at"]))
        updated_raw = raw_estimate.get("source_updated_at")
        updated = datetime.fromisoformat(str(updated_raw)) if updated_raw else now
    except (KeyError, TypeError, ValueError):
        return None, None, None
    if minimum > maximum or maximum <= now:
        return None, None, None
    return minimum, maximum, updated


def _order_status_log(
    order: Order,
    dimension: str,
    from_status: str,
    to_status: str,
    event_code: str,
    actor_id: int,
    request_id: str,
    occurred_at: datetime,
) -> OrderStatusLog:
    return OrderStatusLog(
        order_id=order.id,
        state_dimension=dimension,
        from_status=from_status,
        to_status=to_status,
        event_code=event_code,
        actor_type="admin",
        actor_id=actor_id,
        reason=None,
        order_version=order.version,
        request_id=request_id,
        trace_id=request_id,
        created_at=occurred_at,
    )


def _require_version(actual: int, expected: int) -> None:
    if actual != expected:
        raise ApplicationError(
            status=412,
            code="RESOURCE_VERSION_CONFLICT",
            title="Version conflict",
            detail="订单已发生变化，请刷新后重试。",
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
