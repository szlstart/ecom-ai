from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import datetime
from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PaginationMeta
from app.core.config import Settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.pagination import CursorCodec, CursorPosition
from app.core.security import SecurityService, canonical_request_hash, utc_now
from app.modules.catalog.schemas import Money
from app.modules.checkout.service import CheckoutService, CheckoutSubmissionContext
from app.modules.files.models import FileObject
from app.modules.identity.models import User, UserAddress
from app.modules.inventory.models import Inventory, InventoryLog, InventoryReservation
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
    OrderAction,
    OrderActionTarget,
    OrderAddressView,
    OrderAmountsView,
    OrderCreateRequest,
    OrderCreateResponse,
    OrderDetail,
    OrderEventList,
    OrderEventView,
    OrderItemView,
    OrderList,
    OrderListItem,
    OrderStoreView,
    OrderView,
    SignedMoney,
    TradeOrderView,
)
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
                    "delivery_option": group.selected_delivery_option,
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
                    matched_views=_matched_views(order, items),
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


def _matched_views(order: Order, items: list[OrderItem]) -> list[OrderView]:
    result: list[OrderView] = ["all"]
    if order.order_status == "pending_payment" and order.expires_at > utc_now():
        result.append("pending_payment")
    if order.order_status == "pending_shipment":
        result.append("pending_shipment")
    if order.order_status == "shipped" and order.fulfillment_status != "received":
        result.append("in_transit")
    if order.order_status == "completed":
        result.append("completed")
        if any(item.review_status == "pending" for item in items):
            result.append("pending_review")
    if order.after_sale_status != "none":
        result.append("after_sale")
    if order.order_status in {"cancelled", "closed"} and order.paid_amount == 0:
        result.append("cancelled")
    return result


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


def _order_actions(order: Order, trade: TradeOrder, items: list[OrderItem]) -> list[OrderAction]:
    # Only advertise actions whose endpoint exists in the current deployable slice.
    if order.order_status == "pending_payment" and order.expires_at > utc_now():
        return [_route_action("pay", "payment-cashier", {"tradeOrderId": trade.trade_no})]
    return []


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
