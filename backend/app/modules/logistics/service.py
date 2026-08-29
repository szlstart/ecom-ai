from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Literal, cast

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PaginationMeta
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.pagination import CursorCodec
from app.core.security import SecurityService, canonical_request_hash, utc_now
from app.integrations.logistics import (
    LogisticsProvider,
    LogisticsProviderSnapshot,
    LogisticsProviderTrack,
    logistics_provider,
)
from app.modules.identity.models import User
from app.modules.logistics.domain import SHIPMENT_TRANSITIONS, require_shipment_transition
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
    AdminShipmentVoidRequest,
    AdminTrackingCorrectionRequest,
    DeliveryEstimate,
    FakeLogisticsWebhook,
    LogisticsWebhookAck,
    ShipmentItemView,
    ShipmentRefreshResult,
    ShipmentRouteView,
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
            latest_sync = await self.repository.latest_sync(
                shipment.id, sync_statuses=("success", "no_change")
            )
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
        latest_sync = await self.repository.latest_sync(
            shipment.id, sync_statuses=("success", "no_change")
        )
        route = await self._route_view(shipment, order)
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
            route=route,
            shipped_at=shipment.shipped_at,
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

        _provider(payload.carrier_code)
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

    async def correct_tracking(
        self,
        access: AdminAccess,
        shipment_no: str,
        payload: AdminTrackingCorrectionRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminShipmentDetail:
        claim = await self.idempotency.begin(
            scope_key=(f"admin:shipment-correct:{access.context.user.user_no}:{shipment_no}"),
            idempotency_key=idempotency_key,
            payload={"expected_version": expected_version, **payload.model_dump(mode="json")},
            resource_type="shipment",
        )
        if claim.replayed and claim.record.response_body is not None:
            return AdminShipmentDetail.model_validate(claim.record.response_body)
        initial = await self.repository.shipment_by_no(shipment_no)
        if initial is None:
            raise _not_found()
        access.require_scope("store", initial[2].id)
        order_row = await self.repository.admin_order(initial[1].order_no, for_update=True)
        locked = await self.repository.shipment_by_no(shipment_no, for_update=True)
        if order_row is None or locked is None:
            raise _not_found()
        order, store = order_row
        shipment = locked[0]
        _require_version(shipment.version, expected_version, resource="包裹")
        if shipment.shipment_status != "created" or shipment.last_track_at is not None:
            raise ApplicationError(
                status=409,
                code="SHIPMENT_TRACKING_CORRECTION_WINDOW_CLOSED",
                title="Tracking correction window closed",
                detail="包裹已揽收或已有物流轨迹，不能直接更正运单号。",
            )
        _provider(shipment.carrier_code)
        normalized = _normalize_tracking_no(payload.tracking_no)
        next_hash = self.security.keyed_hash(
            "shipment-tracking-no", f"{shipment.carrier_code}:{normalized}"
        )
        if next_hash == shipment.tracking_no_hash:
            raise ApplicationError(
                status=409,
                code="SHIPMENT_TRACKING_UNCHANGED",
                title="Tracking number unchanged",
                detail="新运单号与当前运单号相同。",
            )
        duplicate = await self.repository.shipment_by_tracking_hash(
            shipment.carrier_code, next_hash
        )
        if duplicate is not None and duplicate.id != shipment.id:
            raise ApplicationError(
                status=409,
                code="SHIPMENT_TRACKING_ALREADY_EXISTS",
                title="Tracking number already exists",
                detail="该承运商运单已经用于其他包裹。",
            )
        now = utc_now()
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        previous_masked = shipment.tracking_no_masked
        previous_hash = shipment.tracking_no_hash
        shipment.tracking_no_ciphertext = self.security.encrypt("shipment-tracking-no", normalized)
        shipment.tracking_no_hash = next_hash
        shipment.tracking_no_masked = _mask_tracking_no(normalized)
        shipment.version += 1
        self.session.add(
            ShipmentTrack(
                shipment_id=shipment.id,
                provider_event_id=f"correction:{request_id}"[:128],
                track_status="created",
                provider_status=None,
                description="发货信息已更正",
                location_text=None,
                occurred_at=now,
                payload_hash=self.security.keyed_hash(
                    "shipment-correction-event", previous_hash + next_hash
                ),
            )
        )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="shipment.tracking_corrected.v1",
                aggregate_type="shipment",
                aggregate_no=shipment.shipment_no,
                aggregate_version=shipment.version,
                payload={
                    "shipment_id": shipment.shipment_no,
                    "order_id": order.order_no,
                    "tracking_no_masked": shipment.tracking_no_masked,
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
            action="correct_shipment_tracking",
            target_type="shipment",
            target_no=shipment.shipment_no,
            reason=payload.reason,
            before={
                "tracking_no_masked": previous_masked,
                "version": expected_version,
            },
            after={
                "tracking_no_masked": shipment.tracking_no_masked,
                "version": shipment.version,
                "reason_code": payload.reason_code,
            },
            scope_type="store",
            scope_id=store.id,
        )
        await self.session.flush()
        result = await self._admin_view(shipment, order, store)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=shipment.shipment_no,
            response_body=result.model_dump(mode="json"),
        )
        await self.session.commit()
        return result

    async def void_shipment(
        self,
        access: AdminAccess,
        shipment_no: str,
        payload: AdminShipmentVoidRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminShipmentDetail:
        claim = await self.idempotency.begin(
            scope_key=f"admin:shipment-void:{access.context.user.user_no}:{shipment_no}",
            idempotency_key=idempotency_key,
            payload={"expected_version": expected_version, **payload.model_dump(mode="json")},
            resource_type="shipment",
        )
        if claim.replayed and claim.record.response_body is not None:
            return AdminShipmentDetail.model_validate(claim.record.response_body)
        initial = await self.repository.shipment_by_no(shipment_no)
        if initial is None:
            raise _not_found()
        access.require_scope("store", initial[2].id)
        order_row = await self.repository.admin_order(initial[1].order_no, for_update=True)
        locked = await self.repository.shipment_by_no(shipment_no, for_update=True)
        if order_row is None or locked is None:
            raise _not_found()
        order, store = order_row
        shipment = locked[0]
        _require_version(shipment.version, expected_version, resource="包裹")
        if shipment.shipment_status != "created" or shipment.last_track_at is not None:
            raise ApplicationError(
                status=409,
                code="SHIPMENT_VOID_WINDOW_CLOSED",
                title="Shipment void window closed",
                detail="只有尚未揽收且没有物流轨迹的包裹可以作废。",
            )
        if order.after_sale_status == "in_progress":
            raise ApplicationError(
                status=409,
                code="SHIPMENT_VOID_BLOCKED_BY_AFTER_SALE",
                title="Shipment void blocked by after-sale",
                detail="当前售后流程依赖该包裹，不能作废。",
            )
        plaintext_tracking = self.security.decrypt(
            "shipment-tracking-no", shipment.tracking_no_ciphertext
        )
        await _provider(shipment.carrier_code).cancel_shipment(
            carrier_code=shipment.carrier_code,
            tracking_no=plaintext_tracking,
        )
        now = utc_now()
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        previous_shipment_status = shipment.shipment_status
        shipment.shipment_status = require_shipment_transition(
            previous_shipment_status, "VoidShipment"
        )
        shipment.voided_at = now
        shipment.void_reason_code = payload.reason_code
        shipment.void_reason = payload.reason
        shipment.version += 1
        await self.session.flush()

        order_items = await self.repository.order_items_for_update(order.id)
        allocated = await self.repository.allocated_quantities(order.id)
        total_required = sum(item.quantity - item.refunded_quantity for item in order_items)
        total_allocated = sum(allocated.values())
        previous_fulfillment = order.fulfillment_status
        previous_order_status = order.order_status
        if total_allocated == 0:
            target_fulfillment = "unfulfilled"
        elif total_allocated < total_required:
            target_fulfillment = "partial"
        else:
            target_fulfillment = "shipped"

        if target_fulfillment != previous_fulfillment:
            command = (
                "ResetUnfulfilled"
                if target_fulfillment == "unfulfilled"
                else "ReopenPartialShipment"
            )
            order.fulfillment_status = require_transition(
                FULFILLMENT_TRANSITIONS, previous_fulfillment, command
            )
            order.version += 1
            self.session.add(
                _order_status_log(
                    order,
                    "fulfillment",
                    previous_fulfillment,
                    target_fulfillment,
                    "fulfillment.shipment_voided",
                    access.context.user.id,
                    request_id,
                    now,
                )
            )
        else:
            order.version += 1
        if previous_order_status == "shipped" and target_fulfillment != "shipped":
            order.order_status = require_transition(
                ORDER_TRANSITIONS,
                previous_order_status,
                "ReopenForShipmentCorrection",
            )
            order.shipped_at = None
            order.version += 1
            self.session.add(
                _order_status_log(
                    order,
                    "order",
                    previous_order_status,
                    "pending_shipment",
                    "order.shipment_voided",
                    access.context.user.id,
                    request_id,
                    now,
                )
            )
        self.session.add(
            OrderOperationLog(
                operation_no=new_prefixed_ulid("oop_"),
                order_id=order.id,
                operation_type="void_shipment",
                actor_type="admin",
                actor_id=access.context.user.id,
                request_payload_hash=self.security.keyed_hash(
                    "shipment-void-request",
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
                event_type="shipment.voided.v1",
                aggregate_type="shipment",
                aggregate_no=shipment.shipment_no,
                aggregate_version=shipment.version,
                payload={
                    "shipment_id": shipment.shipment_no,
                    "order_id": order.order_no,
                    "reason_code": payload.reason_code,
                    "fulfillment_status": order.fulfillment_status,
                    "order_status": order.order_status,
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
            action="void_shipment",
            target_type="shipment",
            target_no=shipment.shipment_no,
            reason=payload.reason,
            before={
                "shipment_status": previous_shipment_status,
                "order_status": previous_order_status,
                "fulfillment_status": previous_fulfillment,
            },
            after={
                "shipment_status": shipment.shipment_status,
                "order_status": order.order_status,
                "fulfillment_status": order.fulfillment_status,
                "reason_code": payload.reason_code,
            },
            scope_type="store",
            scope_id=store.id,
        )
        result = await self._admin_view(shipment, order, store)
        self.idempotency.complete(
            claim,
            response_status=200,
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
        latest_sync = await self.repository.latest_sync(
            shipment.id, sync_statuses=("success", "no_change")
        )
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

    async def process_webhook(
        self,
        provider: str,
        raw_body: bytes,
        signature: str,
        timestamp: str,
    ) -> LogisticsWebhookAck:
        if provider != "fake_express":
            raise _not_found()
        now = utc_now()
        if not _valid_fake_signature(self.security, raw_body, signature, timestamp, now):
            raise ApplicationError(
                status=401,
                code="LOGISTICS_WEBHOOK_SIGNATURE_INVALID",
                title="Logistics webhook signature invalid",
                detail="物流回调验签失败。",
            )
        try:
            payload = FakeLogisticsWebhook.model_validate_json(raw_body)
        except ValidationError as exc:
            raise ApplicationError(
                status=422,
                code="LOGISTICS_WEBHOOK_SCHEMA_INVALID",
                title="Logistics webhook schema invalid",
                detail="物流回调字段校验失败。",
            ) from exc
        if payload.carrier_code != provider:
            raise ApplicationError(
                status=422,
                code="LOGISTICS_PROVIDER_MISMATCH",
                title="Logistics provider mismatch",
                detail="物流回调渠道不匹配。",
            )
        row = await self.repository.shipment_by_no(payload.shipment_id, for_update=True)
        if row is None:
            raise _not_found()
        shipment = row[0]
        normalized_tracking = _normalize_tracking_no(payload.tracking_no)
        tracking_hash = self.security.keyed_hash(
            "shipment-tracking-no",
            f"{payload.carrier_code}:{normalized_tracking}",
        )
        if shipment.carrier_code != payload.carrier_code or not hmac.compare_digest(
            shipment.tracking_no_hash, tracking_hash
        ):
            raise ApplicationError(
                status=422,
                code="LOGISTICS_SHIPMENT_IDENTITY_MISMATCH",
                title="Logistics shipment identity mismatch",
                detail="物流回调运单身份校验失败。",
            )
        track = LogisticsProviderTrack(
            provider_event_id=payload.provider_event_id,
            status=payload.status,
            provider_status=payload.provider_status,
            description=payload.description,
            location_text=payload.location_text,
            occurred_at=_naive_utc(payload.occurred_at),
        )
        snapshot = LogisticsProviderSnapshot(
            provider_request_id=payload.provider_event_id,
            tracks=(track,),
            estimated_delivery_min_at=(
                _naive_utc(payload.estimated_delivery_min_at)
                if payload.estimated_delivery_min_at is not None
                else None
            ),
            estimated_delivery_max_at=(
                _naive_utc(payload.estimated_delivery_max_at)
                if payload.estimated_delivery_max_at is not None
                else None
            ),
        )
        created, duplicates = await self._apply_provider_snapshot(
            shipment,
            snapshot,
            sync_type="webhook",
            response_hash=hashlib.sha256(raw_body).digest(),
            now=now,
        )
        await self.session.commit()
        return LogisticsWebhookAck(
            shipment_id=shipment.shipment_no,
            duplicate=created == 0 and duplicates > 0,
        )

    async def sync_due(
        self,
        *,
        limit: int = 100,
        stale_after_seconds: int = 300,
    ) -> int:
        now = utc_now()
        created = await self._create_automatic_shipments(limit=limit, now=now)
        candidates = await self.repository.sync_candidates(
            now=now,
            stale_before=now - timedelta(seconds=stale_after_seconds),
            simulated_stale_before=now - timedelta(seconds=1),
            limit=limit,
        )
        processed = created
        for candidate in candidates:
            if await self.sync_shipment(candidate.shipment_no, now=now):
                processed += 1
        return processed

    async def sync_shipment(
        self,
        shipment_no: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        effective_now = now or utc_now()
        row = await self.repository.shipment_by_no(shipment_no)
        if row is None or row[0].shipment_status not in {
            "created",
            "picked_up",
            "in_transit",
            "exception",
        }:
            return False
        candidate = row[0]
        started = perf_counter()
        try:
            queried_hash = candidate.tracking_no_hash
            tracking_no = self.security.decrypt(
                "shipment-tracking-no", candidate.tracking_no_ciphertext
            )
            if candidate.carrier_code == "fake_express":
                snapshot = await self._simulated_snapshot(candidate, tracking_no, effective_now)
            else:
                snapshot = await _provider(candidate.carrier_code).query_tracking(
                    carrier_code=candidate.carrier_code,
                    tracking_no=tracking_no,
                )
            locked = await self.repository.shipment_by_no(shipment_no, for_update=True)
            if locked is None:
                return False
            shipment, order, _ = locked
            if not hmac.compare_digest(shipment.tracking_no_hash, queried_hash):
                self.session.add(
                    _sync_log(
                        shipment,
                        sync_type="reconcile",
                        sync_status="retry",
                        provider_request_id=snapshot.provider_request_id,
                        track_count=0,
                        attempt_count=1,
                        duration_ms=_duration_ms(started),
                        now=effective_now,
                        next_retry_at=effective_now + timedelta(seconds=30),
                        error_code="LOGISTICS_TRACKING_CHANGED_DURING_QUERY",
                        last_error="tracking identity changed while provider query was active",
                    )
                )
            else:
                await self._apply_provider_snapshot(
                    shipment,
                    snapshot,
                    order=order,
                    sync_type="poll",
                    response_hash=_snapshot_hash(snapshot),
                    now=effective_now,
                    duration_ms=_duration_ms(started),
                )
        except Exception as exc:
            latest = await self.repository.latest_sync(candidate.id)
            attempts = min((latest.attempt_count if latest else 0) + 1, 32_767)
            terminal = attempts >= 5
            self.session.add(
                _sync_log(
                    candidate,
                    sync_type="poll",
                    sync_status="failed" if terminal else "retry",
                    provider_request_id=None,
                    track_count=0,
                    attempt_count=attempts,
                    duration_ms=_duration_ms(started),
                    now=effective_now,
                    next_retry_at=(
                        None
                        if terminal
                        else effective_now + timedelta(seconds=min(30 * (2 ** (attempts - 1)), 900))
                    ),
                    error_code="LOGISTICS_PROVIDER_QUERY_FAILED",
                    last_error=f"provider query failed: {type(exc).__name__}",
                )
            )
        await self.session.commit()
        return True

    async def _apply_provider_snapshot(
        self,
        shipment: Shipment,
        snapshot: LogisticsProviderSnapshot,
        *,
        sync_type: Literal["poll", "webhook", "reconcile"],
        response_hash: bytes,
        now: datetime,
        duration_ms: int = 0,
        order: Order | None = None,
    ) -> tuple[int, int]:
        created = 0
        duplicates = 0
        for track in sorted(snapshot.tracks, key=lambda item: item.occurred_at):
            event_hash = _provider_track_hash(track)
            if track.provider_event_id is not None:
                existing = await self.repository.track_by_provider_event(
                    shipment.id, track.provider_event_id
                )
                if existing is not None:
                    if not hmac.compare_digest(existing.payload_hash, event_hash):
                        raise ApplicationError(
                            status=409,
                            code="LOGISTICS_PROVIDER_EVENT_CONFLICT",
                            title="Logistics provider event conflict",
                            detail="物流渠道事件标识与既有内容冲突。",
                        )
                    duplicates += 1
                    continue
            description = _safe_provider_text(track.description, maximum=1000, required=True)
            if description is None:
                raise RuntimeError("required provider description was removed during sanitization")
            location = _safe_provider_text(track.location_text, maximum=255, required=False)
            record = ShipmentTrack(
                shipment_id=shipment.id,
                provider_event_id=track.provider_event_id,
                track_status=track.status,
                provider_status=track.provider_status,
                description=description,
                location_text=location,
                occurred_at=track.occurred_at,
                payload_hash=event_hash,
            )
            try:
                async with self.session.begin_nested():
                    self.session.add(record)
                    await self.session.flush()
            except IntegrityError as exc:
                if track.provider_event_id is None:
                    duplicates += 1
                    continue
                existing = await self.repository.track_by_provider_event(
                    shipment.id, track.provider_event_id
                )
                if existing is None or not hmac.compare_digest(existing.payload_hash, event_hash):
                    raise ApplicationError(
                        status=409,
                        code="LOGISTICS_PROVIDER_EVENT_CONFLICT",
                        title="Logistics provider event conflict",
                        detail="物流渠道事件标识与既有内容冲突。",
                    ) from exc
                duplicates += 1
                continue
            previous_status = shipment.shipment_status
            is_latest = (
                shipment.last_track_at is None or track.occurred_at >= shipment.last_track_at
            )
            projected = False
            if is_latest:
                command = _shipment_command(track.status)
                transition = SHIPMENT_TRANSITIONS[command]
                if previous_status == track.status:
                    projected = True
                elif previous_status in transition[0]:
                    shipment.shipment_status = require_shipment_transition(previous_status, command)
                    projected = True
                shipment.last_track_at = track.occurred_at
                if projected:
                    shipment.provider_status = track.provider_status
                    if shipment.shipment_status == "delivered":
                        shipment.delivered_at = track.occurred_at
            shipment.version += 1
            created += 1
            request_id = request_id_context.get() or new_prefixed_ulid("req_")
            self.session.add(
                OutboxEvent(
                    event_no=new_prefixed_ulid("evt_"),
                    event_type="shipment.track_recorded.v1",
                    aggregate_type="shipment",
                    aggregate_no=shipment.shipment_no,
                    aggregate_version=shipment.version,
                    payload={
                        "shipment_id": shipment.shipment_no,
                        "track_status": track.status,
                        "shipment_status": shipment.shipment_status,
                        "provider_event_id": track.provider_event_id,
                        "state_projection_applied": projected,
                    },
                    event_status="pending",
                    available_at=now,
                    attempt_count=0,
                    trace_id=request_id,
                )
            )
        estimate_changed = _apply_carrier_estimate(shipment, snapshot, now)
        if created and order is not None:
            await self._project_order_shipped(order, shipment, now)
        if estimate_changed and created == 0:
            shipment.version += 1
            request_id = request_id_context.get() or new_prefixed_ulid("req_")
            self.session.add(
                OutboxEvent(
                    event_no=new_prefixed_ulid("evt_"),
                    event_type="shipment.estimate_updated.v1",
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
        self.session.add(
            _sync_log(
                shipment,
                sync_type=sync_type,
                sync_status="success" if created or estimate_changed else "no_change",
                provider_request_id=snapshot.provider_request_id,
                response_hash=response_hash,
                track_count=created,
                attempt_count=1,
                duration_ms=duration_ms,
                now=now,
            )
        )
        return created, duplicates

    async def _create_automatic_shipments(self, *, limit: int, now: datetime) -> int:
        created = 0
        for order_no in await self.repository.automatic_shipment_candidates(limit):
            row = await self.repository.admin_order(order_no, for_update=True)
            if row is None:
                continue
            order, store = row
            if (
                order.payment_status not in {"paid", "partially_refunded"}
                or order.order_status != "pending_shipment"
                or order.fulfillment_status != "unfulfilled"
                or order.after_sale_status == "in_progress"
                or await self.repository.allocated_quantities(order.id)
            ):
                continue
            order_items = await self.repository.order_items_for_update(order.id)
            if not order_items:
                continue
            tracking_no = _normalize_tracking_no(
                f"ECOM{new_prefixed_ulid('trk_').replace('_', '')}"
            )
            tracking_hash = self.security.keyed_hash(
                "shipment-tracking-no", f"fake_express:{tracking_no}"
            )
            started_at = order.paid_at or now
            shipment = Shipment(
                shipment_no=new_prefixed_ulid("shp_"),
                order_id=order.id,
                store_id=store.id,
                carrier_code="fake_express",
                carrier_name="Ecom 速运",
                tracking_no_ciphertext=self.security.encrypt(
                    "shipment-tracking-no", tracking_no
                ),
                tracking_no_hash=tracking_hash,
                tracking_no_masked=_mask_tracking_no(tracking_no),
                shipment_status="created",
                estimated_delivery_min_at=started_at + timedelta(seconds=20),
                estimated_delivery_max_at=started_at + timedelta(seconds=25),
                estimate_source="carrier",
                estimate_updated_at=now,
                shipped_at=started_at,
                key_version=1,
            )
            self.session.add(shipment)
            await self.session.flush()
            for item in order_items:
                remaining = item.quantity - item.refunded_quantity
                if remaining > 0:
                    self.session.add(
                        ShipmentItem(
                            shipment_id=shipment.id,
                            order_item_id=item.id,
                            quantity=remaining,
                        )
                    )
            self.session.add(
                OutboxEvent(
                    event_no=new_prefixed_ulid("evt_"),
                    event_type="shipment.automatic_created.v1",
                    aggregate_type="shipment",
                    aggregate_no=shipment.shipment_no,
                    aggregate_version=shipment.version,
                    payload={
                        "shipment_id": shipment.shipment_no,
                        "order_id": order.order_no,
                        "carrier_code": shipment.carrier_code,
                    },
                    event_status="pending",
                    available_at=now,
                    attempt_count=0,
                    trace_id=request_id_context.get(),
                )
            )
            created += 1
        if created:
            await self.session.commit()
        return created

    async def _simulated_snapshot(
        self, shipment: Shipment, tracking_no: str, now: datetime
    ) -> LogisticsProviderSnapshot:
        started_at = shipment.shipped_at or shipment.created_at
        address = await self.repository.order_address(shipment.order_id)
        origin = await self.repository.shipment_origin_region_code(shipment.id)
        destination_district = address.district_code if address is not None else None
        destination_address = (
            self.security.decrypt("address-detail", address.address_ciphertext)
            if address is not None
            else None
        )
        return _simulated_tracking_snapshot(
            shipment_no=shipment.shipment_no,
            tracking_no=tracking_no,
            started_at=started_at,
            now=now,
            origin_region_code=origin,
            destination_district_code=destination_district,
            destination_address=destination_address,
        )

    async def _project_order_shipped(
        self, order: Order, shipment: Shipment, now: datetime
    ) -> None:
        if (
            order.order_status != "pending_shipment"
            or order.fulfillment_status not in {"unfulfilled", "partial"}
            or shipment.shipment_status == "created"
        ):
            return
        order_items = await self.repository.order_items_for_update(order.id)
        allocated = await self.repository.allocated_quantities(order.id)
        if any(
            allocated.get(item.id, 0) < item.quantity - item.refunded_quantity
            for item in order_items
        ):
            return
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        previous_fulfillment = order.fulfillment_status
        order.fulfillment_status = require_transition(
            FULFILLMENT_TRANSITIONS, previous_fulfillment, "RecordAllItemsShipped"
        )
        order.version += 1
        self.session.add(
            OrderStatusLog(
                order_id=order.id,
                state_dimension="fulfillment",
                from_status=previous_fulfillment,
                to_status="shipped",
                event_code="shipment.automatic_dispatched",
                actor_type="system",
                actor_id=None,
                reason="模拟物流首条发货轨迹已生成",
                order_version=order.version,
                request_id=request_id,
                trace_id=request_id,
                created_at=now,
            )
        )
        previous_order = order.order_status
        order.order_status = require_transition(
            ORDER_TRANSITIONS, previous_order, "RecordAllItemsShipped"
        )
        order.shipped_at = shipment.shipped_at or now
        order.version += 1
        self.session.add(
            OrderStatusLog(
                order_id=order.id,
                state_dimension="order",
                from_status=previous_order,
                to_status="shipped",
                event_code="order.automatic_shipped",
                actor_type="system",
                actor_id=None,
                reason="模拟物流已发货",
                order_version=order.version,
                request_id=request_id,
                trace_id=request_id,
                created_at=now,
            )
        )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="order.shipped.v1",
                aggregate_type="order",
                aggregate_no=order.order_no,
                aggregate_version=order.version,
                payload={
                    "order_id": order.order_no,
                    "shipment_id": shipment.shipment_no,
                    "source": "automatic_simulation",
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=request_id,
            )
        )

    async def _route_view(self, shipment: Shipment, order: Order) -> ShipmentRouteView:
        address = await self.repository.order_address(order.id)
        if address is None:
            raise RuntimeError(f"order {order.order_no} has no address snapshot")
        return ShipmentRouteView(
            origin_region_code=await self.repository.shipment_origin_region_code(shipment.id),
            country_code=address.country_code,
            province_code=address.province_code,
            city_code=address.city_code,
            district_code=address.district_code,
            destination_address=self.security.decrypt(
                "address-detail", address.address_ciphertext
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
        latest = await self.repository.latest_sync(shipment.id, sync_type="poll")
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

    async def request_admin_refresh(
        self,
        access: AdminAccess,
        shipment_no: str,
        idempotency_key: str,
    ) -> ShipmentRefreshResult:
        claim = await self.idempotency.begin(
            scope_key=(f"admin:shipment-refresh:{access.context.user.user_no}:{shipment_no}"),
            idempotency_key=idempotency_key,
            payload={"shipment_id": shipment_no},
            resource_type="shipment_refresh",
            ttl_days=1,
        )
        if claim.replayed and claim.record.response_body is not None:
            return ShipmentRefreshResult.model_validate(claim.record.response_body)
        row = await self.repository.shipment_by_no(shipment_no)
        if row is None or row[0].shipment_status in {"voided", "closed"}:
            raise _not_found()
        shipment, _, store = row
        access.require_scope("store", store.id)
        now = utc_now()
        latest = await self.repository.latest_sync(shipment.id, sync_type="poll")
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
                payload={"shipment_id": shipment.shipment_no, "actor_type": "admin"},
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


def _simulated_tracking_snapshot(
    *,
    shipment_no: str,
    tracking_no: str,
    started_at: datetime,
    now: datetime,
    origin_region_code: str | None,
    destination_district_code: str | None,
    destination_address: str | None,
) -> LogisticsProviderSnapshot:
    definitions = (
        (5, "picked_up", "WAITING_PICKUP", "已发货，待揽收", origin_region_code),
        (10, "in_transit", "PICKED_UP", "已揽收，开始运输", origin_region_code),
        (
            15,
            "in_transit",
            "OUT_FOR_DELIVERY",
            "正在派送中…",
            destination_district_code,
        ),
        (20, "delivered", "DELIVERED", "已签收", destination_address),
    )
    tracks: list[LogisticsProviderTrack] = []
    for offset, status, provider_status, description, location in definitions:
        occurred_at = started_at + timedelta(seconds=offset)
        if now < occurred_at:
            continue
        tracks.append(
            LogisticsProviderTrack(
                provider_event_id=f"auto:{shipment_no}:{provider_status.lower()}",
                status=cast(
                    Literal[
                        "picked_up", "in_transit", "delivered", "exception", "returned"
                    ],
                    status,
                ),
                provider_status=provider_status,
                description=description,
                location_text=location,
                occurred_at=occurred_at,
            )
        )
    return LogisticsProviderSnapshot(
        provider_request_id=f"auto-query-{tracking_no[-6:]}-{int(now.timestamp())}",
        tracks=tuple(tracks),
        estimated_delivery_min_at=started_at + timedelta(seconds=20),
        estimated_delivery_max_at=started_at + timedelta(seconds=25),
    )


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
        provider_status=track.provider_status,
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


def _shipment_command(status: str) -> str:
    return {
        "picked_up": "RecordPickup",
        "in_transit": "RecordInTransit",
        "delivered": "RecordDelivery",
        "exception": "RecordException",
        "returned": "RecordReturn",
    }[status]


def _provider_track_hash(track: LogisticsProviderTrack) -> bytes:
    return canonical_request_hash(
        {
            "provider_event_id": track.provider_event_id,
            "status": track.status,
            "provider_status": track.provider_status,
            "description": unicodedata.normalize("NFKC", track.description),
            "location_text": (
                unicodedata.normalize("NFKC", track.location_text)
                if track.location_text is not None
                else None
            ),
            "occurred_at": track.occurred_at.isoformat(),
        }
    )


def _snapshot_hash(snapshot: LogisticsProviderSnapshot) -> bytes:
    return canonical_request_hash(
        {
            "provider_request_id": snapshot.provider_request_id,
            "track_hashes": [_provider_track_hash(item).hex() for item in snapshot.tracks],
            "estimated_delivery_min_at": (
                snapshot.estimated_delivery_min_at.isoformat()
                if snapshot.estimated_delivery_min_at is not None
                else None
            ),
            "estimated_delivery_max_at": (
                snapshot.estimated_delivery_max_at.isoformat()
                if snapshot.estimated_delivery_max_at is not None
                else None
            ),
        }
    )


def _apply_carrier_estimate(
    shipment: Shipment,
    snapshot: LogisticsProviderSnapshot,
    now: datetime,
) -> bool:
    minimum = snapshot.estimated_delivery_min_at
    maximum = snapshot.estimated_delivery_max_at
    if minimum is None and maximum is None:
        return False
    if minimum is None or maximum is None or minimum > maximum:
        return False
    # Current MySQL schema stores these estimates at second precision. Normalize
    # before comparison so an identical webhook replay cannot create a version bump.
    minimum = minimum.replace(microsecond=0)
    maximum = maximum.replace(microsecond=0)
    if maximum <= now:
        next_values: tuple[datetime | None, datetime | None, str | None] = (None, None, None)
    else:
        next_values = (minimum, maximum, "carrier")
    current = (
        shipment.estimated_delivery_min_at,
        shipment.estimated_delivery_max_at,
        shipment.estimate_source,
    )
    if current == next_values:
        return False
    (
        shipment.estimated_delivery_min_at,
        shipment.estimated_delivery_max_at,
        shipment.estimate_source,
    ) = next_values
    shipment.estimate_updated_at = now
    return True


def _safe_provider_text(
    value: str | None,
    *,
    maximum: int,
    required: bool,
) -> str | None:
    if value is None:
        if required:
            raise ApplicationError(
                status=422,
                code="LOGISTICS_TRACK_CONTENT_INVALID",
                title="Logistics track content invalid",
                detail="物流轨迹内容无效。",
            )
        return None
    normalized = unicodedata.normalize("NFKC", value)
    sanitized = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in normalized
    ).strip()
    sanitized = re.sub(r"\s+", " ", sanitized)[:maximum]
    if not sanitized:
        if required:
            raise ApplicationError(
                status=422,
                code="LOGISTICS_TRACK_CONTENT_INVALID",
                title="Logistics track content invalid",
                detail="物流轨迹内容无效。",
            )
        return None
    return sanitized


def _sync_log(
    shipment: Shipment,
    *,
    sync_type: Literal["poll", "webhook", "reconcile"],
    sync_status: Literal["success", "no_change", "retry", "failed"],
    provider_request_id: str | None,
    track_count: int,
    attempt_count: int,
    duration_ms: int,
    now: datetime,
    response_hash: bytes | None = None,
    next_retry_at: datetime | None = None,
    error_code: str | None = None,
    last_error: str | None = None,
) -> LogisticsSyncLog:
    return LogisticsSyncLog(
        shipment_id=shipment.id,
        sync_type=sync_type,
        sync_status=sync_status,
        provider_request_id=(provider_request_id[:128] if provider_request_id else None),
        response_hash=response_hash,
        track_count=track_count,
        attempt_count=attempt_count,
        duration_ms=duration_ms,
        next_retry_at=next_retry_at,
        error_code=error_code,
        last_error=last_error[:1000] if last_error else None,
        trace_id=request_id_context.get(),
        created_at=now,
    )


def _duration_ms(started: float) -> int:
    return min(max(int((perf_counter() - started) * 1000), 0), 4_294_967_295)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


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
        "fake-logistics-webhook", timestamp.encode() + b"." + raw_body
    ).hex()
    return hmac.compare_digest(expected, signature)


def _provider(carrier_code: str) -> LogisticsProvider:
    try:
        return logistics_provider(carrier_code)
    except ValueError as exc:
        raise ApplicationError(
            status=422,
            code="SHIPMENT_CARRIER_UNSUPPORTED",
            title="Unsupported shipment carrier",
            detail="该物流承运商尚未接入。",
        ) from exc


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


def _require_version(actual: int, expected: int, *, resource: str = "订单") -> None:
    if actual != expected:
        raise ApplicationError(
            status=412,
            code="RESOURCE_VERSION_CONFLICT",
            title="Version conflict",
            detail=f"{resource}已发生变化，请刷新后重试。",
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
