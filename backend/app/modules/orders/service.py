from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PaginationMeta
from app.core.config import Settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.pagination import CursorCodec, CursorPosition
from app.core.security import SecurityService, canonical_request_hash, utc_now
from app.modules.cart.models import Cart, CartItem
from app.modules.cart.repository import CartRepository
from app.modules.cart.service import CartService
from app.modules.catalog.schemas import Money
from app.modules.checkout.service import CheckoutService, CheckoutSubmissionContext
from app.modules.files.models import FileObject
from app.modules.identity.models import User, UserAddress
from app.modules.inventory.models import Inventory, InventoryLog, InventoryReservation
from app.modules.orders.domain import (
    FULFILLMENT_TRANSITIONS,
    ORDER_TRANSITIONS,
    OrderPolicySnapshot,
    available_action_codes,
    can_hide,
    matched_views,
    require_transition,
)
from app.modules.orders.models import (
    Order,
    OrderAddress,
    OrderItem,
    OrderOperationLog,
    OrderStatusLog,
    TradeOrder,
)
from app.modules.orders.repository import OrderRepository
from app.modules.orders.schemas import (
    AdminOrderAmountAdjustmentRequest,
    AdminOrderCancellationRequest,
    AdminOrderDetail,
    AdminOrderList,
    AdminOrderSummary,
    OrderAction,
    OrderActionTarget,
    OrderAddressView,
    OrderAmountsView,
    OrderCancellationRequest,
    OrderCommandResult,
    OrderCreateRequest,
    OrderCreateResponse,
    OrderDetail,
    OrderEventList,
    OrderEventView,
    OrderHideResult,
    OrderItemView,
    OrderList,
    OrderListItem,
    OrderRepurchaseResult,
    OrderStoreView,
    OrderView,
    RepurchaseUnavailableItem,
    SignedMoney,
    TradeOrderView,
)
from app.modules.payments.models import Payment
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.stores.models import Store
from app.modules.system.models import OutboxEvent


class OrderService:
    def __init__(
        self, session: AsyncSession, settings: Settings, security: SecurityService
    ) -> None:
        self.session = session
        self.repository = OrderRepository(session)
        self.checkout_service = CheckoutService(session)
        self.idempotency = IdempotencyService(session)
        self.cursor = CursorCodec(settings.security_hmac_secret.get_secret_value())
        self.security = security

    async def dashboard_counts(self, user: User) -> dict[str, int]:
        return await self.repository.dashboard_counts(user.id)

    async def create(
        self, user: User, payload: OrderCreateRequest, idempotency_key: str
    ) -> OrderCreateResponse:
        try:
            claim = await self.idempotency.begin(
                scope_key=f"order:create:{user.user_no}",
                idempotency_key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                resource_type="trade_order",
            )
        except ApplicationError as exc:
            if exc.code != "IDEMPOTENCY_IN_PROGRESS":
                raise
            raise ApplicationError(
                status=409,
                code="IDEMPOTENCY_REQUEST_IN_PROGRESS",
                title="Order creation in progress",
                detail="订单正在创建，请使用相同幂等键稍后重试。",
                retryable=True,
                headers={"Retry-After": "1"},
            ) from exc
        if claim.replayed:
            if claim.record.response_body is None:
                raise _conflict("IDEMPOTENCY_REQUEST_IN_PROGRESS", "订单正在创建，请稍后重试。")
            return OrderCreateResponse.model_validate(claim.record.response_body)

        existing = await self.repository.trade_by_checkout_no(user.id, payload.checkout_id)
        if existing is not None:
            raise _conflict(
                "CHECKOUT_ALREADY_CONSUMED",
                f"该结算会话已创建交易单 {existing.trade_no}，不能重复下单。",
            )
        try:
            checkout = await self.checkout_service.submission_context(
                user, payload.checkout_id, payload.checkout_version
            )
        except ApplicationError as exc:
            if exc.code != "CHECKOUT_NOT_ACTIVE":
                raise
            consumed = await self.repository.trade_by_checkout_no(
                user.id, payload.checkout_id, for_update=True
            )
            if consumed is None:
                raise
            raise _conflict(
                "CHECKOUT_ALREADY_CONSUMED",
                f"该结算会话已创建交易单 {consumed.trade_no}，不能重复下单。",
            ) from exc
        quantity_by_sku = _quantity_by_sku(checkout)
        inventories = await self.repository.lock_inventories(sorted(quantity_by_sku))
        if set(inventories) != set(quantity_by_sku):
            raise _conflict("INVENTORY_INSUFFICIENT", "部分商品暂无可用库存。")

        trade = TradeOrder(
            trade_no=new_prefixed_ulid("trd_"),
            checkout_session_id=checkout.session.id,
            checkout_no_snapshot=checkout.session.checkout_no,
            checkout_snapshot_hash=checkout.snapshot.snapshot_hash,
            user_id=user.id,
            order_source=checkout.session.source_type,
            trade_status="pending_payment",
            goods_amount=checkout.session.goods_amount,
            freight_amount=checkout.session.freight_amount,
            payable_amount=checkout.session.payable_amount,
            adjustment_amount=0,
            paid_amount=0,
            refunded_amount=0,
            currency=checkout.session.currency,
            order_count=len(checkout.view.store_groups),
            expires_at=checkout.session.expires_at,
        )
        self.session.add(trade)
        await self.session.flush()

        contexts_by_store = _contexts_by_store(checkout)
        product_ids = {context[0][2].id for context in checkout.contexts}
        images = await self.repository.main_images(product_ids)
        order_ids: list[str] = []
        order_items: list[tuple[Order, OrderItem, Inventory, int]] = []
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        request_hash = canonical_request_hash(payload.model_dump(mode="json"))
        for group in checkout.view.store_groups:
            contexts = contexts_by_store[group.store_id]
            store = contexts[0][0][3]
            selected_delivery = next(
                (
                    option
                    for option in group.delivery_options
                    if option.option_id == group.selected_delivery_option
                ),
                None,
            )
            order = Order(
                order_no=new_prefixed_ulid("ord_"),
                trade_order_id=trade.id,
                user_id=user.id,
                store_id=store.id,
                order_status="pending_payment",
                payment_status="unpaid",
                fulfillment_status="unfulfilled",
                after_sale_status="none",
                goods_amount=int(group.goods_amount.minor_units),
                freight_amount=int(group.freight_amount.minor_units),
                payable_amount=int(group.goods_amount.minor_units)
                + int(group.freight_amount.minor_units),
                adjustment_amount=0,
                paid_amount=0,
                refunded_amount=0,
                currency="CNY",
                buyer_remark=group.buyer_remark,
                policy_snapshot={
                    "schema_version": 1,
                    "pricing_version": checkout.session.pricing_version,
                    "policy_versions": group.policy_versions,
                    "delivery_option": selected_delivery.model_dump(mode="json")
                    if selected_delivery
                    else None,
                    "checkout_snapshot_hash": checkout.snapshot.snapshot_hash.hex(),
                },
                expires_at=checkout.session.expires_at,
            )
            self.session.add(order)
            await self.session.flush()
            order_ids.append(order.order_no)
            self.session.add(_address_snapshot(order.id, checkout.address))
            for state_dimension, to_status in (
                ("order", "pending_payment"),
                ("payment", "unpaid"),
                ("fulfillment", "unfulfilled"),
                ("after_sale", "none"),
            ):
                self.session.add(
                    OrderStatusLog(
                        order_id=order.id,
                        state_dimension=state_dimension,
                        from_status=None,
                        to_status=to_status,
                        event_code="order.created",
                        actor_type="user",
                        actor_id=user.id,
                        order_version=order.version,
                        request_id=request_id,
                        trace_id=request_id,
                    )
                )
            self.session.add(
                OrderOperationLog(
                    operation_no=new_prefixed_ulid("oop_"),
                    order_id=order.id,
                    operation_type="create",
                    actor_type="user",
                    actor_id=user.id,
                    request_payload_hash=request_hash,
                    result_status="success",
                    request_id=request_id,
                    trace_id=request_id,
                )
            )
            for context, quantity in contexts:
                _, sku, product, _, _, _, _ = context
                gross = sku.sale_price_amount * quantity
                item = OrderItem(
                    order_item_no=new_prefixed_ulid("oit_"),
                    order_id=order.id,
                    product_id=product.id,
                    sku_id=sku.id,
                    product_no=product.product_no,
                    sku_no=sku.sku_no,
                    product_name=product.product_name,
                    sku_name=sku.sku_name,
                    spec_snapshot=sku.spec_values,
                    image_object_key=images.get(product.id),
                    quantity=quantity,
                    unit_price_amount=sku.sale_price_amount,
                    market_price_amount=sku.market_price_amount,
                    gross_amount=gross,
                    payable_amount=gross,
                    adjustment_amount=0,
                    refunded_quantity=0,
                    refunded_amount=0,
                    currency=sku.currency,
                    review_status="pending",
                    after_sale_status="none",
                )
                self.session.add(item)
                await self.session.flush()
                order_items.append((order, item, inventories[sku.id], quantity))

        for order, item, inventory, quantity in sorted(
            order_items, key=lambda value: value[2].sku_id
        ):
            await self._reserve(user, trade, order, item, inventory, quantity)
        await self._consume_cart(user.id, checkout)
        checkout.session.checkout_status = "submitted"
        checkout.session.submitted_at = utc_now()
        checkout.session.version += 1
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="order.created.v1",
                aggregate_type="trade_order",
                aggregate_no=trade.trade_no,
                aggregate_version=trade.version,
                payload={
                    "trade_order_id": trade.trade_no,
                    "order_ids": order_ids,
                    "user_id": user.user_no,
                    "payment_deadline_at": trade.expires_at.isoformat(),
                },
                event_status="pending",
                available_at=utc_now(),
                attempt_count=0,
                trace_id=request_id,
            )
        )
        response = OrderCreateResponse(
            trade_order_id=trade.trade_no,
            order_ids=order_ids,
            payment_deadline_at=trade.expires_at,
            available_actions=["pay", "view_orders"],
            version=trade.version,
        )
        self.idempotency.complete(
            claim,
            response_status=201,
            resource_no=trade.trade_no,
            response_body=cast(dict[str, object], response.model_dump(mode="json")),
        )
        await self.session.commit()
        return response

    async def list_mine(
        self,
        user: User,
        *,
        view: OrderView,
        query: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[OrderList, PaginationMeta]:
        normalized_query = query.strip() if query else None
        if normalized_query == "":
            normalized_query = None
        filter_key = json.dumps(
            {
                "user": user.user_no,
                "view": view,
                "q": normalized_query,
                "created_from": created_from.isoformat() if created_from else None,
                "created_to": created_to.isoformat() if created_to else None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        position = self.cursor.decode(cursor, filter_key=filter_key)
        try:
            rows, has_more = await self.repository.user_orders(
                user_id=user.id,
                view=view,
                query=normalized_query,
                created_from=created_from,
                created_to=created_to,
                position=position,
                limit=limit,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ApplicationError(
                status=400,
                code="PAGINATION_CURSOR_INVALID",
                title="Invalid pagination cursor",
                detail="分页位置无效，请重新加载订单列表。",
            ) from exc
        items = await self._order_views(rows)
        return OrderList(items=items), _order_pagination(
            rows=[row[0] for row in rows],
            position=position,
            has_more=has_more,
            filter_key=filter_key,
            limit=limit,
            codec=self.cursor,
        )

    async def detail_mine(self, user: User, order_no: str) -> OrderDetail:
        row = await self.repository.user_order(user.id, order_no)
        if row is None:
            raise _not_found()
        order, _, _ = row
        views = await self._order_views([row])
        address = await self.repository.order_address(order.id)
        if address is None:
            raise RuntimeError(f"order {order.order_no} has no address snapshot")
        events = await self.repository.order_events(order.id)
        base = views[0].model_dump()
        return OrderDetail(
            **base,
            buyer_remark=order.buyer_remark,
            address=OrderAddressView(
                recipient_name=self.security.decrypt(
                    "address-recipient", address.recipient_name_ciphertext
                ),
                phone_masked=f"*** **** {address.phone_last4}",
                country_code=address.country_code,
                province_code=address.province_code,
                city_code=address.city_code,
                district_code=address.district_code,
                address=self.security.decrypt("address-detail", address.address_ciphertext),
                postal_code=address.postal_code,
            ),
            policy_snapshot=order.policy_snapshot,
            events=[_event_view(event) for event in events],
        )

    async def events_mine(self, user: User, order_no: str) -> OrderEventList:
        row = await self.repository.user_order(user.id, order_no)
        if row is None:
            raise _not_found()
        return OrderEventList(
            items=[_event_view(event) for event in await self.repository.order_events(row[0].id)]
        )

    async def trade_mine(self, user: User, trade_no: str) -> TradeOrderView:
        result = await self.repository.user_trade(user.id, trade_no)
        if result is None:
            raise _not_found()
        trade, order_rows = result
        rows = [(order, store, trade) for order, store in order_rows]
        orders = await self._order_views(rows)
        return TradeOrderView(
            trade_order_id=trade.trade_no,
            order_source=cast(Any, trade.order_source),
            trade_status=trade.trade_status,
            amounts=_amounts(trade),
            order_count=trade.order_count,
            orders=orders,
            created_at=trade.created_at,
            expires_at=trade.expires_at,
            paid_at=trade.paid_at,
            closed_at=trade.closed_at,
            available_actions=_trade_actions(trade),
            version=trade.version,
        )

    async def admin_list(
        self,
        access: AdminAccess,
        *,
        query: str | None,
        order_status: str | None,
        payment_status: str | None,
        fulfillment_status: str | None,
        after_sale_status: str | None,
        limit: int,
    ) -> AdminOrderList:
        normalized_query = query.strip() if query else None
        rows = await self.repository.admin_orders(
            scopes=access.scopes,
            query=normalized_query or None,
            order_status=order_status,
            payment_status=payment_status,
            fulfillment_status=fulfillment_status,
            after_sale_status=after_sale_status,
            limit=limit,
        )
        order_views = await self._order_views([(row[0], row[1], row[2]) for row in rows])
        return AdminOrderList(
            items=[
                _admin_order_summary(view, row[3])
                for view, row in zip(order_views, rows, strict=True)
            ]
        )

    async def admin_detail(self, access: AdminAccess, order_no: str) -> AdminOrderDetail:
        row = await self.repository.admin_order(order_no)
        if row is None:
            raise _not_found()
        order, store, trade, user = row
        access.require_scope("store", store.id)
        view = (await self._order_views([(order, store, trade)]))[0]
        return AdminOrderDetail(
            **_admin_order_summary(view, user).model_dump(),
            events=[_event_view(event) for event in await self.repository.order_events(order.id)],
        )

    async def admin_adjust_amount(
        self,
        access: AdminAccess,
        order_no: str,
        payload: AdminOrderAmountAdjustmentRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminOrderDetail:
        claim = await self.idempotency.begin(
            scope_key=f"admin:order-adjust:{access.context.user.user_no}:{order_no}",
            idempotency_key=idempotency_key,
            payload={"version": expected_version, **payload.model_dump(mode="json")},
            resource_type="order",
        )
        if claim.replayed and claim.record.response_body is not None:
            return AdminOrderDetail.model_validate(claim.record.response_body)
        initial = await self.repository.admin_order(order_no)
        if initial is None:
            raise _not_found()
        access.require_scope("store", initial[1].id)
        trade = await self.repository.trade_for_update(initial[0].trade_order_id)
        if trade is None:
            raise _not_found()
        orders = await self.repository.trade_orders_for_update(trade.id)
        order = next((candidate for candidate in orders if candidate.order_no == order_no), None)
        if order is None:
            raise _not_found()
        _require_version(order, expected_version)
        if (
            order.order_status != "pending_payment"
            or order.payment_status != "unpaid"
            or trade.trade_status != "pending_payment"
        ):
            raise _state_conflict(order, "adjust_amount")
        active_payment = await self.session.scalar(
            select(Payment.id).where(
                Payment.trade_order_id == trade.id,
                Payment.payment_status.in_(("created", "pending")),
            )
        )
        if active_payment is not None:
            raise _conflict(
                "ORDER_ADJUSTMENT_BLOCKED_BY_PAYMENT",
                "交易单存在正在处理或结果未知的支付尝试，不能改价。",
            )
        if payload.adjustment_amount.currency != order.currency:
            raise _conflict("ORDER_CURRENCY_MISMATCH", "调整金额币种与订单不一致。")
        target_adjustment = int(payload.adjustment_amount.minor_units)
        if abs(target_adjustment) > 9_000_000_000_000_000:
            raise _conflict("ORDER_ADJUSTMENT_OUT_OF_RANGE", "调整金额超出允许范围。")
        items = await self.repository.order_items_for_update(order.id)
        if not items or order.goods_amount + target_adjustment < 0:
            raise _conflict(
                "ORDER_PAYABLE_AMOUNT_INVALID",
                "调整后商品应付金额不能小于零。",
            )
        allocations = _allocate_adjustment(items, target_adjustment)
        previous_adjustment = order.adjustment_amount
        previous_payable = order.payable_amount
        delta = target_adjustment - previous_adjustment
        for item, allocation in zip(items, allocations, strict=True):
            item.adjustment_amount = allocation
            item.payable_amount = item.gross_amount + allocation
            item.version += 1
        order.adjustment_amount = target_adjustment
        order.payable_amount = order.goods_amount + order.freight_amount + target_adjustment
        order.adjustment_reason_code = payload.reason_code
        order.adjustment_reason = payload.reason
        order.adjusted_by = access.context.user.id
        order.adjusted_at = utc_now()
        order.version += 1
        trade.adjustment_amount += delta
        trade.payable_amount += delta
        trade.version += 1
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        now = utc_now()
        self.session.add(
            OrderStatusLog(
                order_id=order.id,
                state_dimension="pricing",
                from_status=str(previous_adjustment),
                to_status=str(target_adjustment),
                event_code="order.amount_adjusted",
                actor_type="admin",
                actor_id=access.context.user.id,
                reason=payload.reason,
                order_version=order.version,
                request_id=request_id,
                trace_id=request_id,
                created_at=now,
            )
        )
        self.session.add(
            OrderOperationLog(
                operation_no=new_prefixed_ulid("oop_"),
                order_id=order.id,
                operation_type="admin_adjust_amount",
                actor_type="admin",
                actor_id=access.context.user.id,
                request_payload_hash=canonical_request_hash(payload.model_dump(mode="json")),
                result_status="success",
                request_id=request_id,
                trace_id=request_id,
            )
        )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="order.amount_adjusted.v1",
                aggregate_type="order",
                aggregate_no=order.order_no,
                aggregate_version=order.version,
                payload={
                    "order_id": order.order_no,
                    "trade_order_id": trade.trade_no,
                    "adjustment_amount": str(target_adjustment),
                    "payable_amount": str(order.payable_amount),
                    "currency": order.currency,
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
            action="adjust_order_amount",
            target_type="order",
            target_no=order.order_no,
            reason=payload.reason,
            before={
                "adjustment_amount": str(previous_adjustment),
                "payable_amount": str(previous_payable),
                "version": expected_version,
            },
            after={
                "adjustment_amount": str(target_adjustment),
                "payable_amount": str(order.payable_amount),
                "version": order.version,
                "reason_code": payload.reason_code,
            },
            scope_type="store",
            scope_id=initial[1].id,
        )
        await self.session.flush()
        result = await self.admin_detail(access, order_no)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=order.order_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def admin_cancel(
        self,
        access: AdminAccess,
        order_no: str,
        payload: AdminOrderCancellationRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminOrderDetail:
        claim = await self.idempotency.begin(
            scope_key=f"admin:order-cancel:{access.context.user.user_no}:{order_no}",
            idempotency_key=idempotency_key,
            payload={"version": expected_version, **payload.model_dump(mode="json")},
            resource_type="order",
        )
        if claim.replayed and claim.record.response_body is not None:
            return AdminOrderDetail.model_validate(claim.record.response_body)
        initial = await self.repository.admin_order(order_no)
        if initial is None:
            raise _not_found()
        for store_id in await self.repository.trade_store_ids(initial[0].trade_order_id):
            access.require_scope("store", store_id)
        trade = await self.repository.trade_for_update(initial[0].trade_order_id)
        if trade is None:
            raise _not_found()
        orders = await self.repository.trade_orders_for_update(trade.id)
        order = next((candidate for candidate in orders if candidate.order_no == order_no), None)
        if order is None:
            raise _not_found()
        _require_version(order, expected_version)
        if trade.trade_status != "pending_payment" or any(
            sibling.order_status != "pending_payment" or sibling.payment_status != "unpaid"
            for sibling in orders
        ):
            raise _state_conflict(order, "cancel")
        active_payment = await self.session.scalar(
            select(Payment.id).where(
                Payment.trade_order_id == trade.id,
                Payment.payment_status.in_(("created", "pending")),
            )
        )
        if active_payment is not None:
            raise _conflict(
                "ORDER_CANCELLATION_BLOCKED_BY_PAYMENT",
                "交易单存在正在处理或结果未知的支付尝试，不能取消。",
            )
        reservations = await self.repository.active_reservations_for_orders(
            [sibling.id for sibling in orders]
        )
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        self._close_unpaid_trade(
            trade=trade,
            orders=orders,
            reservations=reservations,
            actor_type="admin",
            actor_id=access.context.user.id,
            reason_code=payload.reason_code,
            reason=payload.reason,
            event_code="order.admin_cancelled",
            operation_type="admin_cancel",
            request_id=request_id,
            now=utc_now(),
            request_payload_hash=canonical_request_hash(payload.model_dump(mode="json")),
        )
        record_admin_operation(
            self.session,
            access,
            action="cancel_trade_order",
            target_type="trade_order",
            target_no=trade.trade_no,
            reason=payload.reason,
            before={
                "trade_status": "pending_payment",
                "order_ids": [sibling.order_no for sibling in orders],
                "target_order_version": expected_version,
            },
            after={
                "trade_status": "closed",
                "order_status": "cancelled",
                "reason_code": payload.reason_code,
            },
            scope_type="store",
            scope_id=initial[1].id,
        )
        await self.session.flush()
        result = await self.admin_detail(access, order_no)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=order.order_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def cancel(
        self,
        user: User,
        order_no: str,
        payload: OrderCancellationRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> OrderCommandResult:
        claim = await self.idempotency.begin(
            scope_key=f"order:cancel:{user.user_no}:{order_no}",
            idempotency_key=idempotency_key,
            payload={"version": expected_version, **payload.model_dump(mode="json")},
            resource_type="order",
        )
        if claim.replayed and claim.record.response_body is not None:
            return OrderCommandResult.model_validate(claim.record.response_body)
        row = await self.repository.user_order(user.id, order_no, for_update=True)
        if row is None:
            raise _not_found()
        order, _, trade = row
        _require_version(order, expected_version)
        require_transition(ORDER_TRANSITIONS, order.order_status, "CancelUnpaidOrder")
        if (
            order.order_status != "pending_payment"
            or order.payment_status != "unpaid"
            or trade.trade_status != "pending_payment"
        ):
            raise _state_conflict(order, "cancel_order")

        now = utc_now()
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        sibling_orders = await self.repository.trade_orders_for_update(trade.id)
        if any(
            sibling.order_status != "pending_payment" or sibling.payment_status != "unpaid"
            for sibling in sibling_orders
        ):
            raise _state_conflict(order, "cancel_order")
        reservations = await self.repository.active_reservations_for_orders(
            [sibling.id for sibling in sibling_orders]
        )
        all_events = self._close_unpaid_trade(
            trade=trade,
            orders=sibling_orders,
            reservations=reservations,
            actor_type="user",
            actor_id=user.id,
            reason_code=payload.reason_code,
            reason=payload.description or payload.reason_code,
            event_code="order.user_cancelled",
            operation_type="cancel",
            request_id=request_id,
            now=now,
            request_payload_hash=canonical_request_hash(payload.model_dump(mode="json")),
        )
        new_events = [event for event in all_events if event.order_id == order.id]
        await self.session.flush()
        refreshed = await self.repository.user_order(user.id, order_no)
        if refreshed is None:
            raise RuntimeError("cancelled order disappeared")
        view = (await self._order_views([refreshed]))[0]
        result = OrderCommandResult(order=view, events=[_event_view(event) for event in new_events])
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=order.order_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def expire_due(self, *, limit: int = 100) -> int:
        now = utc_now()
        trades = await self.repository.expired_pending_trades(now, limit)
        processed = 0
        for trade in trades:
            orders = await self.repository.trade_orders_for_update(trade.id)
            if not orders or any(
                order.order_status != "pending_payment" or order.payment_status != "unpaid"
                for order in orders
            ):
                continue
            for order in orders:
                require_transition(ORDER_TRANSITIONS, order.order_status, "CancelUnpaidOrder")
            reservations = await self.repository.active_reservations_for_orders(
                [order.id for order in orders]
            )
            request_id = new_prefixed_ulid("req_")
            self._close_unpaid_trade(
                trade=trade,
                orders=orders,
                reservations=reservations,
                actor_type="system",
                actor_id=None,
                reason_code="payment_timeout",
                reason="支付时限已到，订单自动关闭。",
                event_code="order.payment_timed_out",
                operation_type="timeout_close",
                request_id=request_id,
                now=now,
                request_payload_hash=None,
            )
            processed += 1
        await self.session.commit()
        return processed

    def _close_unpaid_trade(
        self,
        *,
        trade: TradeOrder,
        orders: list[Order],
        reservations: list[tuple[InventoryReservation, Inventory]],
        actor_type: str,
        actor_id: int | None,
        reason_code: str,
        reason: str,
        event_code: str,
        operation_type: str,
        request_id: str,
        now: datetime,
        request_payload_hash: bytes | None,
    ) -> list[OrderStatusLog]:
        order_by_id = {order.id: order for order in orders}
        for reservation, inventory in reservations:
            before = inventory.reserved_quantity
            if before < reservation.quantity:
                raise RuntimeError("active reservation exceeds reserved inventory")
            inventory.reserved_quantity -= reservation.quantity
            inventory.version += 1
            reservation.reservation_status = "released"
            reservation.released_at = now
            reservation.release_reason = reason_code
            reservation.version += 1
            order = order_by_id[reservation.order_id]
            self.session.add(
                InventoryLog(
                    inventory_id=inventory.id,
                    sku_id=inventory.sku_id,
                    operation_type="release",
                    on_hand_delta=0,
                    reserved_delta=-reservation.quantity,
                    on_hand_before=inventory.on_hand_quantity,
                    on_hand_after=inventory.on_hand_quantity,
                    reserved_before=before,
                    reserved_after=before - reservation.quantity,
                    reference_type="order",
                    reference_no=order.order_no,
                    idempotency_key=f"release:{reservation.reservation_no}",
                    actor_type=actor_type,
                    actor_id=actor_id,
                    reason=reason_code,
                    inventory_version=inventory.version,
                )
            )
        events: list[OrderStatusLog] = []
        for order in orders:
            previous = order.order_status
            order.order_status = "cancelled"
            order.closed_at = now
            order.version += 1
            event = OrderStatusLog(
                order_id=order.id,
                state_dimension="order",
                from_status=previous,
                to_status="cancelled",
                event_code=event_code,
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
                order_version=order.version,
                request_id=request_id,
                trace_id=request_id,
                created_at=now,
            )
            events.append(event)
            self.session.add(event)
            self.session.add(
                OrderOperationLog(
                    operation_no=new_prefixed_ulid("oop_"),
                    order_id=order.id,
                    operation_type=operation_type,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    request_payload_hash=request_payload_hash,
                    result_status="success",
                    request_id=request_id,
                    trace_id=request_id,
                )
            )
        trade.trade_status = "closed"
        trade.closed_at = now
        trade.version += 1
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="trade_order.cancelled.v1",
                aggregate_type="trade_order",
                aggregate_no=trade.trade_no,
                aggregate_version=trade.version,
                payload={
                    "trade_order_id": trade.trade_no,
                    "order_ids": [order.order_no for order in orders],
                    "reason_code": reason_code,
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=request_id,
            )
        )
        return events

    async def confirm_receipt(
        self,
        user: User,
        order_no: str,
        expected_version: int,
        idempotency_key: str,
    ) -> OrderCommandResult:
        claim = await self.idempotency.begin(
            scope_key=f"order:receipt:{user.user_no}:{order_no}",
            idempotency_key=idempotency_key,
            payload={"version": expected_version},
            resource_type="order",
        )
        if claim.replayed and claim.record.response_body is not None:
            return OrderCommandResult.model_validate(claim.record.response_body)
        row = await self.repository.user_order(user.id, order_no, for_update=True)
        if row is None:
            raise _not_found()
        order, _, _ = row
        _require_version(order, expected_version)
        require_transition(ORDER_TRANSITIONS, order.order_status, "ConfirmReceipt")
        require_transition(FULFILLMENT_TRANSITIONS, order.fulfillment_status, "ConfirmReceipt")
        if order.order_status != "shipped" or order.fulfillment_status != "shipped":
            raise _state_conflict(order, "confirm_receipt")
        now = utc_now()
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        order.order_status = "completed"
        order.fulfillment_status = "received"
        order.completed_at = now
        order.version += 1
        events = [
            OrderStatusLog(
                order_id=order.id,
                state_dimension="fulfillment",
                from_status="shipped",
                to_status="received",
                event_code="order.receipt_confirmed",
                actor_type="user",
                actor_id=user.id,
                order_version=order.version,
                request_id=request_id,
                trace_id=request_id,
                created_at=now,
            ),
            OrderStatusLog(
                order_id=order.id,
                state_dimension="order",
                from_status="shipped",
                to_status="completed",
                event_code="order.receipt_confirmed",
                actor_type="user",
                actor_id=user.id,
                order_version=order.version,
                request_id=request_id,
                trace_id=request_id,
                created_at=now,
            ),
        ]
        self.session.add_all(events)
        self.session.add(
            OrderOperationLog(
                operation_no=new_prefixed_ulid("oop_"),
                order_id=order.id,
                operation_type="confirm_receipt",
                actor_type="user",
                actor_id=user.id,
                result_status="success",
                request_id=request_id,
                trace_id=request_id,
            )
        )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="order.receipt_confirmed.v1",
                aggregate_type="order",
                aggregate_no=order.order_no,
                aggregate_version=order.version,
                payload={"order_id": order.order_no, "confirmed_at": now.isoformat()},
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=request_id,
            )
        )
        await self.session.flush()
        refreshed = await self.repository.user_order(user.id, order_no)
        if refreshed is None:
            raise RuntimeError("completed order disappeared")
        result = OrderCommandResult(
            order=(await self._order_views([refreshed]))[0],
            events=[_event_view(event) for event in events],
        )
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=order.order_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def hide(self, user: User, order_no: str, expected_version: int) -> OrderHideResult:
        row = await self.repository.user_order(user.id, order_no, for_update=True)
        if row is None:
            raise _not_found()
        order, _, _ = row
        _require_version(order, expected_version)
        items = (await self.repository.order_items([order.id])).get(order.id, [])
        if not can_hide(_policy_snapshot(order, items)):
            raise _state_conflict(order, "delete_order")
        now = utc_now()
        order.user_hidden_at = now
        order.undo_until = now + timedelta(minutes=5)
        order.version += 1
        self.session.add(_operation(order, user, "hide"))
        await self.session.commit()
        return OrderHideResult(
            order_id=order.order_no,
            undo_until=order.undo_until,
            restore_url=f"/api/v1/users/me/orders/{order.order_no}/restorations",
            version=order.version,
        )

    async def restore(
        self,
        user: User,
        order_no: str,
        expected_version: int,
        idempotency_key: str,
    ) -> OrderListItem:
        claim = await self.idempotency.begin(
            scope_key=f"order:restore:{user.user_no}:{order_no}",
            idempotency_key=idempotency_key,
            payload={"version": expected_version},
            resource_type="order",
        )
        if claim.replayed and claim.record.response_body is not None:
            return OrderListItem.model_validate(claim.record.response_body)
        row = await self.repository.user_order(
            user.id, order_no, include_hidden=True, for_update=True
        )
        if row is None:
            raise _not_found()
        order, _, _ = row
        _require_version(order, expected_version)
        if order.user_hidden_at is None or order.undo_until is None or order.undo_until < utc_now():
            raise _conflict("ORDER_RESTORE_WINDOW_EXPIRED", "订单撤销隐藏窗口已过期。")
        order.user_hidden_at = None
        order.undo_until = None
        order.version += 1
        self.session.add(_operation(order, user, "restore"))
        await self.session.flush()
        refreshed = await self.repository.user_order(user.id, order_no)
        if refreshed is None:
            raise RuntimeError("restored order disappeared")
        result = (await self._order_views([refreshed]))[0]
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=order.order_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def repurchase(
        self,
        user: User,
        order_no: str,
        expected_cart_version: int,
        idempotency_key: str,
    ) -> OrderRepurchaseResult:
        claim = await self.idempotency.begin(
            scope_key=f"order:repurchase:{user.user_no}:{order_no}",
            idempotency_key=idempotency_key,
            payload={"cart_version": expected_cart_version},
            resource_type="cart",
        )
        if claim.replayed and claim.record.response_body is not None:
            return OrderRepurchaseResult.model_validate(claim.record.response_body)
        row = await self.repository.user_order(user.id, order_no)
        if row is None:
            raise _not_found()
        order, _, _ = row
        cart_repository = CartRepository(self.session)
        await self.session.execute(select(User.id).where(User.id == user.id).with_for_update())
        cart = await cart_repository.cart(user.id, for_update=True)
        if cart is None:
            if expected_cart_version != 0:
                raise _cart_version_conflict()
            cart = Cart(
                cart_no=new_prefixed_ulid("cart_"),
                user_id=user.id,
                cart_status="active",
                item_count=0,
                last_activity_at=utc_now(),
            )
            self.session.add(cart)
            await self.session.flush()
        elif cart.version != expected_cart_version:
            raise _cart_version_conflict()
        contexts = await self.repository.repurchase_contexts(order.id)
        added: list[str] = []
        unavailable: list[RepurchaseUnavailableItem] = []
        for source, sku, product, store, inventory in contexts:
            reason = _repurchase_unavailable(source, sku, product, store, inventory)
            existing = await cart_repository.item_for_sku(cart.id, sku.id, for_update=True)
            if reason is None and existing is not None and existing.quantity + source.quantity > 99:
                reason = ("CART_SKU_QUANTITY_LIMIT", "同一规格加入购物车后将超过 99 件。")
            if reason is not None:
                unavailable.append(
                    RepurchaseUnavailableItem(
                        order_item_id=source.order_item_no,
                        sku_id=source.sku_no,
                        product_name=source.product_name,
                        reason_code=reason[0],
                        reason_message=reason[1],
                    )
                )
                continue
            if existing is None:
                existing = CartItem(
                    cart_item_no=new_prefixed_ulid("ci_"),
                    cart_id=cart.id,
                    sku_id=sku.id,
                    quantity=source.quantity,
                    is_selected=True,
                    added_price_amount=sku.sale_price_amount,
                    currency=sku.currency,
                    sku_version=sku.version,
                )
                self.session.add(existing)
                cart.item_count += 1
            else:
                existing.quantity += source.quantity
                existing.is_selected = True
                existing.added_price_amount = sku.sale_price_amount
                existing.currency = sku.currency
                existing.sku_version = sku.version
                existing.invalid_reason = None
                existing.version += 1
            added.append(source.order_item_no)
        if added:
            cart.last_activity_at = utc_now()
            cart.version += 1
        self.session.add(_operation(order, user, "repurchase"))
        await self.session.flush()
        cart_view = await CartService(self.session).get(user)
        result = OrderRepurchaseResult(
            order_id=order.order_no,
            added_items=added,
            unavailable_items=unavailable,
            requires_reselection=bool(unavailable),
            cart=cart_view,
        )
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=cart.cart_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def _order_views(
        self, rows: list[tuple[Order, Store, TradeOrder]]
    ) -> list[OrderListItem]:
        order_ids = [order.id for order, _, _ in rows]
        items_by_order = await self.repository.order_items(order_ids)
        object_keys = {
            key for order, store, _ in rows for key in [store.logo_object_key] if key is not None
        }
        object_keys.update(
            item.image_object_key
            for items in items_by_order.values()
            for item in items
            if item.image_object_key is not None
        )
        files = await self.repository.public_files(object_keys)
        result: list[OrderListItem] = []
        for order, store, trade in rows:
            items = items_by_order.get(order.id, [])
            logo = files.get(store.logo_object_key or "")
            result.append(
                OrderListItem(
                    order_id=order.order_no,
                    trade_order_id=trade.trade_no,
                    order_source=cast(Any, trade.order_source),
                    store=OrderStoreView(
                        store_id=store.store_no,
                        store_name=store.store_name,
                        logo_url=f"/api/v1/files/{logo.file_no}" if logo else None,
                    ),
                    order_status=order.order_status,
                    payment_status=order.payment_status,
                    fulfillment_status=order.fulfillment_status,
                    after_sale_status=order.after_sale_status,
                    matched_views=cast(
                        Any, matched_views(_policy_snapshot(order, items), utc_now())
                    ),
                    items=[
                        _item_view(item, files.get(item.image_object_key or "")) for item in items
                    ],
                    item_count=len(items),
                    total_quantity=sum(item.quantity for item in items),
                    amounts=_amounts(order),
                    created_at=order.created_at,
                    expires_at=order.expires_at,
                    available_actions=_order_actions(order, trade, items),
                    version=order.version,
                )
            )
        return result

    async def _reserve(
        self,
        user: User,
        trade: TradeOrder,
        order: Order,
        item: OrderItem,
        inventory: Inventory,
        quantity: int,
    ) -> None:
        before = inventory.reserved_quantity
        statement = (
            update(Inventory)
            .where(
                Inventory.id == inventory.id,
                Inventory.inventory_status == "active",
                Inventory.on_hand_quantity
                - Inventory.reserved_quantity
                - Inventory.safety_stock_quantity
                >= quantity,
            )
            .values(
                reserved_quantity=Inventory.reserved_quantity + quantity,
                version=Inventory.version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        if cast(Any, result).rowcount != 1:
            raise _conflict("INVENTORY_INSUFFICIENT", "库存不足，请返回结算页刷新。")
        reservation_key = f"{trade.trade_no}:{item.order_item_no}:reserve"
        self.session.add(
            InventoryReservation(
                reservation_no=new_prefixed_ulid("irs_"),
                inventory_id=inventory.id,
                sku_id=item.sku_id,
                order_id=order.id,
                order_item_id=item.id,
                quantity=quantity,
                reservation_status="active",
                idempotency_key=reservation_key,
                expires_at=trade.expires_at,
            )
        )
        self.session.add(
            InventoryLog(
                inventory_id=inventory.id,
                sku_id=item.sku_id,
                operation_type="reserve",
                on_hand_delta=0,
                reserved_delta=quantity,
                on_hand_before=inventory.on_hand_quantity,
                on_hand_after=inventory.on_hand_quantity,
                reserved_before=before,
                reserved_after=before + quantity,
                reference_type="order",
                reference_no=order.order_no,
                idempotency_key=f"{reservation_key}:log",
                actor_type="user",
                actor_id=user.id,
                reason="order_created",
                inventory_version=inventory.version + 1,
            )
        )

    async def _consume_cart(self, user_id: int, checkout: CheckoutSubmissionContext) -> None:
        if checkout.source["source_type"] != "cart":
            return
        item_nos = cast(list[str], checkout.source["cart_item_ids"])
        items = await self.repository.cart_items(user_id, item_nos)
        if len(items) != len(item_nos):
            raise _conflict("CART_ITEMS_CHANGED", "购物车商品已变化，请重新结算。")
        for item in items:
            item.is_selected = False
            item.version += 1
        cart = await self.repository.cart(user_id)
        if cart is not None:
            cart.last_activity_at = utc_now()
            cart.version += 1


def _quantity_by_sku(checkout: CheckoutSubmissionContext) -> dict[int, int]:
    result: dict[int, int] = {}
    for context, quantity in checkout.contexts:
        sku = context[1]
        result[sku.id] = result.get(sku.id, 0) + quantity
    return result


def _contexts_by_store(
    checkout: CheckoutSubmissionContext,
) -> dict[str, list[tuple[Any, int]]]:
    result: OrderedDict[str, list[tuple[Any, int]]] = OrderedDict()
    for context, quantity in checkout.contexts:
        store = context[3]
        result.setdefault(store.store_no, []).append((context, quantity))
    return result


def _address_snapshot(order_id: int, address: UserAddress) -> OrderAddress:
    digest = hashlib.sha256()
    for value in (
        address.recipient_name_ciphertext,
        address.phone_ciphertext,
        address.country_code.encode(),
        address.province_code.encode(),
        address.city_code.encode(),
        address.district_code.encode(),
        address.address_ciphertext,
        (address.postal_code or "").encode(),
    ):
        digest.update(len(value).to_bytes(4, "big"))
        digest.update(value)
    return OrderAddress(
        order_id=order_id,
        source_address_no=address.address_no,
        recipient_name_ciphertext=address.recipient_name_ciphertext,
        phone_ciphertext=address.phone_ciphertext,
        phone_last4=address.phone_last4,
        country_code=address.country_code,
        province_code=address.province_code,
        city_code=address.city_code,
        district_code=address.district_code,
        address_ciphertext=address.address_ciphertext,
        postal_code=address.postal_code,
        address_hash=digest.digest(),
        key_version=address.key_version,
    )


def _money(amount: int, currency: str) -> Money:
    return Money(minor_units=str(amount), currency=currency)


def _amounts(value: Order | TradeOrder) -> OrderAmountsView:
    return OrderAmountsView(
        goods_amount=_money(value.goods_amount, value.currency),
        freight_amount=_money(value.freight_amount, value.currency),
        adjustment_amount=SignedMoney(
            minor_units=str(value.adjustment_amount), currency=value.currency
        ),
        payable_amount=_money(value.payable_amount, value.currency),
        paid_amount=_money(value.paid_amount, value.currency),
        refunded_amount=_money(value.refunded_amount, value.currency),
    )


def _item_view(item: OrderItem, file: FileObject | None) -> OrderItemView:
    return OrderItemView(
        order_item_id=item.order_item_no,
        product_id=item.product_no,
        sku_id=item.sku_no,
        product_name=item.product_name,
        sku_name=item.sku_name,
        spec_snapshot=item.spec_snapshot,
        image_url=f"/api/v1/files/{file.file_no}?variant=thumbnail" if file else None,
        quantity=item.quantity,
        unit_price=_money(item.unit_price_amount, item.currency),
        gross_amount=_money(item.gross_amount, item.currency),
        payable_amount=_money(item.payable_amount, item.currency),
        refunded_amount=_money(item.refunded_amount, item.currency),
        refunded_quantity=item.refunded_quantity,
        review_status=item.review_status,
        after_sale_status=item.after_sale_status,
    )


def _admin_order_summary(view: OrderListItem, user: User) -> AdminOrderSummary:
    actions: list[str] = []
    if view.order_status == "pending_payment" and view.payment_status == "unpaid":
        actions.extend(("adjust_amount", "cancel"))
    if (
        view.order_status == "pending_shipment"
        and view.payment_status == "paid"
        and view.fulfillment_status in {"unfulfilled", "partial"}
        and view.after_sale_status != "in_progress"
    ):
        actions.append("create_shipment")
    return AdminOrderSummary(
        order=view,
        user_id=user.user_no,
        user_name_masked=_mask_username(user.username),
        available_admin_actions=cast(Any, actions),
    )


def _mask_username(value: str) -> str:
    if len(value) <= 2:
        return value[0] + "*" if value else "*"
    return value[0] + "*" * min(4, len(value) - 2) + value[-1]


def _allocate_adjustment(items: list[OrderItem], target: int) -> list[int]:
    total = sum(item.gross_amount for item in items)
    if total <= 0:
        if target != 0:
            raise _conflict("ORDER_ADJUSTMENT_UNALLOCATABLE", "订单项金额无法分摊调整额。")
        return [0 for _ in items]
    magnitude = abs(target)
    allocations = [magnitude * item.gross_amount // total for item in items]
    remainder = magnitude - sum(allocations)
    for index in range(remainder):
        allocations[index % len(allocations)] += 1
    sign = -1 if target < 0 else 1
    result = [sign * value for value in allocations]
    if any(
        item.gross_amount + allocation < 0 for item, allocation in zip(items, result, strict=True)
    ):
        raise _conflict("ORDER_ADJUSTMENT_UNALLOCATABLE", "调整额无法安全分摊到订单项。")
    return result


def _policy_snapshot(order: Order, items: list[OrderItem]) -> OrderPolicySnapshot:
    return OrderPolicySnapshot(
        order_status=order.order_status,
        payment_status=order.payment_status,
        fulfillment_status=order.fulfillment_status,
        after_sale_status=order.after_sale_status,
        paid_amount=order.paid_amount,
        expires_at=order.expires_at,
        all_reviews_terminal=all(item.review_status in {"reviewed", "closed"} for item in items),
        has_pending_review=any(item.review_status == "pending" for item in items),
        has_after_sale_history=order.after_sale_status != "none",
        has_refundable_items=any(
            item.refunded_quantity < item.quantity and item.refunded_amount < item.payable_amount
            for item in items
        ),
    )


def _route_action(
    code: Any,
    name: str,
    params: dict[str, str],
    *,
    confirmation: bool = False,
) -> OrderAction:
    return OrderAction(
        code=code,
        enabled=True,
        requires_confirmation=confirmation,
        target=OrderActionTarget(name=name, params=params),
    )


def _order_actions(
    order: Order,
    trade: TradeOrder,
    items: list[OrderItem],
) -> list[OrderAction]:
    routes = {
        "pay": ("payment-cashier", {"tradeOrderId": trade.trade_no}, False),
        "cancel_order": ("my-order-detail", {"orderId": order.order_no}, True),
        "view_logistics": ("my-order-logistics", {"orderId": order.order_no}, False),
        "view_after_sale": ("my-after-sales", {}, False),
        "apply_after_sale": ("refund-application", {"orderId": order.order_no}, False),
        "review": (
            "my-review-create",
            {
                "orderItemId": next(
                    item.order_item_no for item in items if item.review_status == "pending"
                )
            },
            False,
        ),
        "confirm_receipt": ("my-order-detail", {"orderId": order.order_no}, True),
        "delete_order": ("my-order-detail", {"orderId": order.order_no}, True),
        "repurchase": ("my-order-detail", {"orderId": order.order_no}, False),
    }
    result: list[OrderAction] = []
    for code in available_action_codes(_policy_snapshot(order, items), utc_now()):
        route, params, confirmation = routes[code]
        result.append(_route_action(code, route, params, confirmation=confirmation))
    return result


def _trade_actions(trade: TradeOrder) -> list[OrderAction]:
    if trade.trade_status == "pending_payment" and trade.expires_at > utc_now():
        return [_route_action("pay", "payment-cashier", {"tradeOrderId": trade.trade_no})]
    return []


def _event_view(event: OrderStatusLog) -> OrderEventView:
    return OrderEventView(
        event_id=event.id,
        state_dimension=event.state_dimension,
        from_status=event.from_status,
        to_status=event.to_status,
        event_code=event.event_code,
        actor_type=event.actor_type,
        reason=event.reason,
        order_version=event.order_version,
        occurred_at=event.created_at,
    )


def _order_pagination(
    *,
    rows: list[Order],
    position: CursorPosition | None,
    has_more: bool,
    filter_key: str,
    limit: int,
    codec: CursorCodec,
) -> PaginationMeta:
    backward = position is not None and position.direction == "previous"
    has_previous = has_more if backward else position is not None
    has_next = position is not None if backward else has_more
    previous = (
        codec.encode(
            filter_key=filter_key,
            values=(rows[0].created_at.isoformat(), str(rows[0].id)),
            direction="previous",
        )
        if rows and has_previous
        else None
    )
    following = (
        codec.encode(
            filter_key=filter_key,
            values=(rows[-1].created_at.isoformat(), str(rows[-1].id)),
            direction="next",
        )
        if rows and has_next
        else None
    )
    return PaginationMeta(
        previous_cursor=previous,
        next_cursor=following,
        has_previous=has_previous,
        has_next=has_next,
        limit=limit,
    )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="未找到该订单。",
    )


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Order conflict", detail=detail)


def _require_version(order: Order, expected_version: int) -> None:
    if order.version != expected_version:
        raise ApplicationError(
            status=412,
            code="RESOURCE_VERSION_CONFLICT",
            title="Version conflict",
            detail="订单已经变化，请刷新后重试。",
        )


def _state_conflict(order: Order, action: str) -> ApplicationError:
    return ApplicationError(
        status=409,
        code="ORDER_STATE_CONFLICT",
        title="Order state conflict",
        detail=f"订单当前状态不允许执行 {action}。",
    )


def _operation(order: Order, user: User, operation_type: str) -> OrderOperationLog:
    request_id = request_id_context.get() or new_prefixed_ulid("req_")
    return OrderOperationLog(
        operation_no=new_prefixed_ulid("oop_"),
        order_id=order.id,
        operation_type=operation_type,
        actor_type="user",
        actor_id=user.id,
        result_status="success",
        request_id=request_id,
        trace_id=request_id,
    )


def _repurchase_unavailable(
    source: OrderItem,
    sku: Any,
    product: Any,
    store: Store,
    inventory: Inventory | None,
) -> tuple[str, str] | None:
    if product.product_status != "on_sale" or sku.sku_status != "active":
        return "SKU_NOT_PURCHASABLE", "原规格已下架，请重新选择商品规格。"
    if store.store_status != "active":
        return "STORE_UNAVAILABLE", "店铺当前不可用。"
    if inventory is None or inventory.inventory_status != "active":
        return "INVENTORY_UNAVAILABLE", "暂时无法确认该规格库存。"
    available = (
        inventory.on_hand_quantity - inventory.reserved_quantity - inventory.safety_stock_quantity
    )
    if available < source.quantity:
        return "INSUFFICIENT_STOCK", "当前库存不足，未加入购物车。"
    return None


def _cart_version_conflict() -> ApplicationError:
    return ApplicationError(
        status=412,
        code="RESOURCE_VERSION_CONFLICT",
        title="Version conflict",
        detail="购物车已经变化，请刷新后重试。",
    )
