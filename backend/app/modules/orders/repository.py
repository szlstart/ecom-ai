from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from sqlalchemy import Select, and_, case, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import CursorPosition
from app.core.security import utc_now
from app.modules.cart.models import Cart, CartItem
from app.modules.catalog.models import Product, ProductImage, ProductSku
from app.modules.files.models import FileObject
from app.modules.identity.models import User
from app.modules.inventory.models import Inventory, InventoryReservation
from app.modules.logistics.models import Shipment, ShipmentItem
from app.modules.orders.models import Order, OrderAddress, OrderItem, OrderStatusLog, TradeOrder
from app.modules.stores.models import Store


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def dashboard_counts(self, user_id: int) -> dict[str, int]:
        now = utc_now()
        pending_review = exists().where(
            OrderItem.order_id == Order.id,
            OrderItem.review_status == "pending",
        )
        visible = Order.user_hidden_at.is_(None)
        statement = select(
            func.sum(
                case(
                    (
                        visible
                        & (Order.order_status == "pending_payment")
                        & (Order.expires_at > now),
                        1,
                    ),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (visible & (Order.order_status == "pending_shipment"), 1),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (
                        visible
                        & (Order.order_status == "shipped")
                        & (Order.fulfillment_status != "received"),
                        1,
                    ),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (
                        visible & (Order.order_status == "completed") & pending_review,
                        1,
                    ),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (visible & (Order.after_sale_status != "none"), 1),
                    else_=0,
                )
            ),
        ).where(Order.user_id == user_id)
        row = (await self.session.execute(statement)).one()
        keys = (
            "pending_payment",
            "pending_shipment",
            "in_transit",
            "pending_review",
            "after_sale",
        )
        return {key: int(value or 0) for key, value in zip(keys, row, strict=True)}

    async def trade_by_checkout_no(
        self, user_id: int, checkout_no: str, *, for_update: bool = False
    ) -> TradeOrder | None:
        statement = select(TradeOrder).where(
            TradeOrder.user_id == user_id,
            TradeOrder.checkout_no_snapshot == checkout_no,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(
            TradeOrder | None,
            await self.session.scalar(statement),
        )

    async def user_orders(
        self,
        *,
        user_id: int,
        view: str,
        query: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        position: CursorPosition | None,
        limit: int,
    ) -> tuple[list[tuple[Order, Store, TradeOrder]], bool]:
        statement = (
            select(Order, Store, TradeOrder)
            .join(Store, Store.id == Order.store_id)
            .join(TradeOrder, TradeOrder.id == Order.trade_order_id)
            .where(Order.user_id == user_id, Order.user_hidden_at.is_(None))
        )
        statement = _order_view(statement, view)
        if query:
            pattern = f"%{query}%"
            statement = statement.where(
                or_(
                    Order.order_no.like(pattern),
                    Store.store_name.like(pattern),
                    exists().where(
                        OrderItem.order_id == Order.id,
                        OrderItem.product_name.like(pattern),
                    ),
                )
            )
        if created_from is not None:
            statement = statement.where(Order.created_at >= created_from)
        if created_to is not None:
            statement = statement.where(Order.created_at < created_to)
        statement, reverse = _order_cursor(statement, position)
        rows = list((await self.session.execute(statement.limit(limit + 1))).all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        if reverse:
            rows.reverse()
        return [(row[0], row[1], row[2]) for row in rows], has_more

    async def admin_orders(
        self,
        *,
        scopes: Sequence[tuple[str, int]],
        query: str | None,
        store_no: str | None,
        view: str | None,
        order_status: str | None,
        payment_status: str | None,
        fulfillment_status: str | None,
        after_sale_status: str | None,
        position: CursorPosition | None,
        limit: int,
    ) -> tuple[list[tuple[Order, Store, TradeOrder, User]], bool]:
        statement = (
            select(Order, Store, TradeOrder, User)
            .join(Store, Store.id == Order.store_id)
            .join(TradeOrder, TradeOrder.id == Order.trade_order_id)
            .join(User, User.id == Order.user_id)
        )
        if ("platform", 0) not in scopes:
            store_ids = [scope_id for scope_type, scope_id in scopes if scope_type == "store"]
            if not store_ids:
                return [], False
            statement = statement.where(Order.store_id.in_(store_ids))
        if store_no:
            statement = statement.where(Store.store_no == store_no)
        if query:
            pattern = f"%{query}%"
            statement = statement.where(
                or_(
                    Order.order_no.like(pattern),
                    TradeOrder.trade_no.like(pattern),
                    Store.store_name.like(pattern),
                    User.user_no.like(pattern),
                )
            )
        if order_status:
            statement = statement.where(Order.order_status == order_status)
        if payment_status:
            statement = statement.where(Order.payment_status == payment_status)
        if fulfillment_status:
            statement = statement.where(Order.fulfillment_status == fulfillment_status)
        if after_sale_status:
            statement = statement.where(Order.after_sale_status == after_sale_status)
        if view and view != "all":
            statement = _admin_order_view(statement, view)
        if position is not None:
            if position.direction != "next" or len(position.values) != 2:
                raise ValueError("unsupported admin order cursor")
            created_at = datetime.fromisoformat(position.values[0])
            order_id = int(position.values[1])
            statement = statement.where(
                or_(
                    Order.created_at < created_at,
                    and_(Order.created_at == created_at, Order.id < order_id),
                )
            )
        rows = list(
            (
                await self.session.execute(
                    statement.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit + 1)
                )
            ).all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        return [(row[0], row[1], row[2], row[3]) for row in rows], has_more

    async def shipment_allocations(self, order_ids: Sequence[int]) -> dict[int, dict[str, int]]:
        """Return non-voided shipment quantities keyed by order and public order-item ID."""
        if not order_ids:
            return {}
        rows = (
            await self.session.execute(
                select(
                    OrderItem.order_id,
                    OrderItem.order_item_no,
                    func.coalesce(func.sum(ShipmentItem.quantity), 0),
                )
                .join(ShipmentItem, ShipmentItem.order_item_id == OrderItem.id)
                .join(Shipment, Shipment.id == ShipmentItem.shipment_id)
                .where(
                    OrderItem.order_id.in_(order_ids),
                    Shipment.shipment_status != "voided",
                )
                .group_by(OrderItem.order_id, OrderItem.order_item_no)
            )
        ).all()
        result: dict[int, dict[str, int]] = {}
        for order_id, order_item_no, quantity in rows:
            result.setdefault(order_id, {})[order_item_no] = int(quantity)
        return result

    async def admin_order(
        self, order_no: str, *, for_update: bool = False
    ) -> tuple[Order, Store, TradeOrder, User] | None:
        statement = (
            select(Order, Store, TradeOrder, User)
            .join(Store, Store.id == Order.store_id)
            .join(TradeOrder, TradeOrder.id == Order.trade_order_id)
            .join(User, User.id == Order.user_id)
            .where(Order.order_no == order_no)
        )
        if for_update:
            statement = statement.with_for_update(of=Order)
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1], row[2], row[3]) if row else None

    async def auto_confirmable_orders(self, cutoff: datetime, limit: int) -> list[Order]:
        delivered_in_time = exists().where(
            Shipment.order_id == Order.id,
            Shipment.shipment_status == "delivered",
            Shipment.delivered_at.is_not(None),
            Shipment.delivered_at <= cutoff,
        )
        unfinished_package = exists().where(
            Shipment.order_id == Order.id,
            Shipment.shipment_status.not_in(("delivered", "voided")),
        )
        recently_delivered_package = exists().where(
            Shipment.order_id == Order.id,
            Shipment.shipment_status == "delivered",
            or_(Shipment.delivered_at.is_(None), Shipment.delivered_at > cutoff),
        )
        return list(
            (
                await self.session.scalars(
                    select(Order)
                    .where(
                        Order.order_status == "shipped",
                        Order.fulfillment_status == "shipped",
                        Order.after_sale_status != "in_progress",
                        delivered_in_time,
                        ~unfinished_package,
                        ~recently_delivered_package,
                    )
                    .order_by(Order.shipped_at, Order.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
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

    async def trade_store_ids(self, trade_order_id: int) -> list[int]:
        return list(
            (
                await self.session.scalars(
                    select(Order.store_id)
                    .where(Order.trade_order_id == trade_order_id)
                    .order_by(Order.store_id)
                )
            ).all()
        )

    async def user_order(
        self,
        user_id: int,
        order_no: str,
        *,
        include_hidden: bool = False,
        for_update: bool = False,
    ) -> tuple[Order, Store, TradeOrder] | None:
        statement = (
            select(Order, Store, TradeOrder)
            .join(Store, Store.id == Order.store_id)
            .join(TradeOrder, TradeOrder.id == Order.trade_order_id)
            .where(Order.user_id == user_id, Order.order_no == order_no)
        )
        if not include_hidden:
            statement = statement.where(Order.user_hidden_at.is_(None))
        if for_update:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1], row[2]) if row else None

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

    async def expired_pending_trades(self, now: datetime, limit: int) -> list[TradeOrder]:
        return list(
            (
                await self.session.scalars(
                    select(TradeOrder)
                    .where(
                        TradeOrder.trade_status == "pending_payment",
                        TradeOrder.expires_at <= now,
                    )
                    .order_by(TradeOrder.expires_at, TradeOrder.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )

    async def active_reservations_for_orders(
        self, order_ids: Sequence[int]
    ) -> list[tuple[InventoryReservation, Inventory]]:
        if not order_ids:
            return []
        rows = list(
            (
                await self.session.execute(
                    select(InventoryReservation, Inventory)
                    .join(Inventory, Inventory.id == InventoryReservation.inventory_id)
                    .where(
                        InventoryReservation.order_id.in_(order_ids),
                        InventoryReservation.reservation_status == "active",
                    )
                    .order_by(Inventory.sku_id, InventoryReservation.id)
                    .with_for_update()
                )
            ).all()
        )
        return [(row[0], row[1]) for row in rows]

    async def repurchase_contexts(
        self, order_id: int
    ) -> list[tuple[OrderItem, ProductSku, Product, Store, Inventory | None]]:
        rows = list(
            (
                await self.session.execute(
                    select(OrderItem, ProductSku, Product, Store, Inventory)
                    .join(ProductSku, ProductSku.id == OrderItem.sku_id)
                    .join(Product, Product.id == ProductSku.product_id)
                    .join(Store, Store.id == ProductSku.store_id)
                    .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
                    .where(OrderItem.order_id == order_id)
                    .order_by(ProductSku.id)
                    .with_for_update()
                )
            ).all()
        )
        return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]

    async def user_trade(
        self, user_id: int, trade_no: str
    ) -> tuple[TradeOrder, list[tuple[Order, Store]]] | None:
        trade = cast(
            TradeOrder | None,
            await self.session.scalar(
                select(TradeOrder).where(
                    TradeOrder.user_id == user_id, TradeOrder.trade_no == trade_no
                )
            ),
        )
        if trade is None:
            return None
        rows = list(
            (
                await self.session.execute(
                    select(Order, Store)
                    .join(Store, Store.id == Order.store_id)
                    .where(
                        Order.trade_order_id == trade.id,
                        Order.user_id == user_id,
                        Order.user_hidden_at.is_(None),
                    )
                    .order_by(Order.id)
                )
            ).all()
        )
        return trade, [(row[0], row[1]) for row in rows]

    async def order_items(self, order_ids: Sequence[int]) -> dict[int, list[OrderItem]]:
        if not order_ids:
            return {}
        rows = list(
            (
                await self.session.scalars(
                    select(OrderItem)
                    .where(OrderItem.order_id.in_(order_ids))
                    .order_by(OrderItem.order_id, OrderItem.id)
                )
            ).all()
        )
        result: dict[int, list[OrderItem]] = {}
        for item in rows:
            result.setdefault(item.order_id, []).append(item)
        return result

    async def product_availability(self, product_ids: set[int]) -> dict[int, bool]:
        """Return current public availability for products referenced by order snapshots."""
        if not product_ids:
            return {}
        rows = (
            await self.session.execute(
                select(
                    Product.id,
                    Product.product_status,
                    Product.deleted_at,
                ).where(Product.id.in_(product_ids))
            )
        ).all()
        return {
            product_id: product_status == "on_sale" and deleted_at is None
            for product_id, product_status, deleted_at in rows
        }

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

    async def public_files(self, object_keys: set[str]) -> dict[str, FileObject]:
        if not object_keys:
            return {}
        rows = list(
            (
                await self.session.scalars(
                    select(FileObject).where(
                        FileObject.object_key.in_(object_keys),
                        FileObject.file_status == "active",
                        FileObject.scan_status == "safe",
                        FileObject.visibility.in_(("public", "public_derivative")),
                    )
                )
            ).all()
        )
        return {item.object_key: item for item in rows}

    async def order_address(self, order_id: int) -> OrderAddress | None:
        return cast(
            OrderAddress | None,
            await self.session.scalar(
                select(OrderAddress).where(OrderAddress.order_id == order_id)
            ),
        )

    async def order_events(self, order_id: int) -> list[OrderStatusLog]:
        return list(
            (
                await self.session.scalars(
                    select(OrderStatusLog)
                    .where(OrderStatusLog.order_id == order_id)
                    .order_by(OrderStatusLog.created_at, OrderStatusLog.id)
                )
            ).all()
        )

    async def lock_inventories(self, sku_ids: list[int]) -> dict[int, Inventory]:
        rows = list(
            (
                await self.session.scalars(
                    select(Inventory)
                    .where(Inventory.sku_id.in_(sku_ids))
                    .order_by(Inventory.sku_id)
                    .with_for_update()
                )
            ).all()
        )
        return {row.sku_id: row for row in rows}

    async def lock_products(self, product_ids: list[int]) -> dict[int, Product]:
        rows = list(
            (
                await self.session.scalars(
                    select(Product)
                    .where(Product.id.in_(product_ids))
                    .order_by(Product.id)
                    .with_for_update()
                )
            ).all()
        )
        return {row.id: row for row in rows}

    async def sku_images(self, sku_ids: set[int]) -> dict[int, str]:
        if not sku_ids:
            return {}
        rows = (
            await self.session.execute(
                select(ProductImage.sku_id, ProductImage.object_key)
                .where(
                    ProductImage.sku_id.in_(sku_ids),
                    ProductImage.image_type == "spec",
                    ProductImage.image_status == "active",
                )
                .order_by(ProductImage.sku_id, ProductImage.sort_order, ProductImage.id)
            )
        ).all()
        result: dict[int, str] = {}
        for sku_id, object_key in rows:
            result.setdefault(sku_id, object_key)
        return result

    async def cart_items(self, user_id: int, item_nos: list[str]) -> list[CartItem]:
        if not item_nos:
            return []
        return list(
            (
                await self.session.scalars(
                    select(CartItem)
                    .join(Cart, Cart.id == CartItem.cart_id)
                    .where(Cart.user_id == user_id, CartItem.cart_item_no.in_(item_nos))
                    .order_by(CartItem.id)
                    .with_for_update()
                )
            ).all()
        )

    async def cart(self, user_id: int) -> Cart | None:
        return cast(
            Cart | None,
            await self.session.scalar(
                select(Cart).where(Cart.user_id == user_id).with_for_update()
            ),
        )


def _order_view(
    statement: Select[tuple[Order, Store, TradeOrder]], view: str
) -> Select[tuple[Order, Store, TradeOrder]]:
    if view == "all":
        return statement
    if view == "pending_payment":
        return statement.where(
            Order.order_status == "pending_payment", Order.expires_at > utc_now()
        )
    if view == "pending_shipment":
        return statement.where(Order.order_status == "pending_shipment")
    if view == "in_transit":
        return statement.where(
            Order.order_status == "shipped", Order.fulfillment_status != "received"
        )
    if view == "completed":
        return statement.where(Order.order_status == "completed")
    if view == "pending_review":
        return statement.where(
            Order.order_status == "completed",
            exists().where(OrderItem.order_id == Order.id, OrderItem.review_status == "pending"),
        )
    if view == "after_sale":
        return statement.where(Order.after_sale_status != "none")
    if view == "cancelled":
        return statement.where(
            Order.order_status.in_(("cancelled", "closed")), Order.paid_amount == 0
        )
    raise ValueError(f"unsupported order view: {view}")


def _admin_order_view(
    statement: Select[tuple[Order, Store, TradeOrder, User]], view: str
) -> Select[tuple[Order, Store, TradeOrder, User]]:
    if view == "pending_payment":
        return statement.where(Order.order_status == "pending_payment")
    if view == "pending_shipment":
        return statement.where(Order.order_status == "pending_shipment")
    if view == "in_transit":
        return statement.where(Order.order_status == "shipped")
    if view == "completed":
        return statement.where(Order.order_status == "completed")
    if view == "after_sale":
        return statement.where(Order.after_sale_status == "in_progress")
    if view == "cancelled":
        return statement.where(Order.order_status.in_(("cancelled", "closed")))
    raise ValueError(f"unsupported admin order view: {view}")


def _order_cursor(
    statement: Select[tuple[Order, Store, TradeOrder]], position: CursorPosition | None
) -> tuple[Select[tuple[Order, Store, TradeOrder]], bool]:
    reverse = position is not None and position.direction == "previous"
    descending = not reverse
    if position is not None:
        if len(position.values) != 2:
            raise ValueError("order cursor must contain two values")
        timestamp = datetime.fromisoformat(position.values[0])
        order_id = int(position.values[1])
        key = tuple_(Order.created_at, Order.id)
        statement = statement.where(
            key < (timestamp, order_id) if descending else key > (timestamp, order_id)
        )
    return statement.order_by(
        Order.created_at.desc() if descending else Order.created_at.asc(),
        Order.id.desc() if descending else Order.id.asc(),
    ), reverse
