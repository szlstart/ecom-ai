from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import canonical_request_hash, utc_now
from app.modules.checkout.service import CheckoutService, CheckoutSubmissionContext
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
from app.modules.orders.schemas import OrderCreateRequest, OrderCreateResponse
from app.modules.system.models import OutboxEvent


class OrderService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = OrderRepository(session)
        self.checkout_service = CheckoutService(session)
        self.idempotency = IdempotencyService(session)

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


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Order conflict", detail=detail)
