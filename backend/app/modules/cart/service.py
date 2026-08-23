from __future__ import annotations

from collections import OrderedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import utc_now
from app.modules.cart.models import Cart, CartItem
from app.modules.cart.repository import CartRepository
from app.modules.cart.schemas import (
    CartAmountSummary,
    CartItemCreateRequest,
    CartItemPatchRequest,
    CartItemView,
    CartSelectionReplaceRequest,
    CartStoreGroupView,
    CartView,
)
from app.modules.catalog.schemas import Money
from app.modules.identity.models import User
from app.modules.inventory.models import Inventory


class CartService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = CartRepository(session)
        self.idempotency = IdempotencyService(session)

    async def get(self, user: User) -> CartView:
        cart = await self.repository.cart(user.id)
        return await self._view(cart)

    async def add(self, user: User, payload: CartItemCreateRequest, key: str) -> CartView:
        claim = await self.idempotency.begin(
            scope_key=f"cart:add:{user.user_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="cart",
        )
        if claim.replayed:
            if claim.record.response_body is not None:
                return CartView.model_validate(claim.record.response_body)
            return await self.get(user)
        await self.session.execute(select(User.id).where(User.id == user.id).with_for_update())
        context = await self.repository.sku_context(payload.sku_id, for_update=True)
        if context is None:
            raise _not_found()
        sku, product, store, inventory = context
        if (
            product.product_status != "on_sale"
            or sku.sku_status != "active"
            or store.store_status != "active"
        ):
            raise _conflict("SKU_NOT_PURCHASABLE", "当前商品规格不可加入购物车。")
        cart = await self.repository.cart(user.id, for_update=True)
        if cart is None:
            cart = Cart(
                cart_no=new_prefixed_ulid("cart_"),
                user_id=user.id,
                cart_status="active",
                item_count=0,
                last_activity_at=utc_now(),
            )
            self.session.add(cart)
            await self.session.flush()
        item = await self.repository.item_for_sku(cart.id, sku.id, for_update=True)
        next_quantity = payload.quantity + (item.quantity if item else 0)
        if next_quantity > 99:
            raise _conflict("CART_SKU_QUANTITY_LIMIT", "同一规格最多购买 99 件。")
        invalid_reason = _invalid_reason(
            product.product_status, sku.sku_status, store.store_status, inventory, next_quantity
        )
        if item is None:
            item = CartItem(
                cart_item_no=new_prefixed_ulid("ci_"),
                cart_id=cart.id,
                sku_id=sku.id,
                quantity=next_quantity,
                is_selected=True,
                added_price_amount=sku.sale_price_amount,
                currency=sku.currency,
                sku_version=sku.version,
                invalid_reason=invalid_reason,
            )
            self.session.add(item)
            cart.item_count += 1
        else:
            item.quantity = next_quantity
            item.sku_version = sku.version
            item.invalid_reason = invalid_reason
            item.version += 1
        cart.last_activity_at = utc_now()
        cart.version += 1
        await self.session.flush()
        response = await self._view(cart)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=cart.cart_no,
            response_body=response.model_dump(mode="json"),
        )
        await self.session.commit()
        return response

    async def patch(
        self,
        user: User,
        item_no: str,
        payload: CartItemPatchRequest,
        expected_version: int,
    ) -> CartView:
        cart = await self._cart_for_write(user.id, expected_version)
        item = await self.repository.item(cart.id, item_no, for_update=True)
        if item is None:
            raise _not_found()
        context = await self.repository.sku_context_by_id(item.sku_id, for_update=True)
        if context is None:
            raise _not_found()
        sku, product, store, inventory = context
        if payload.quantity is not None:
            item.quantity = payload.quantity
        if payload.is_selected is not None:
            item.is_selected = payload.is_selected
        item.invalid_reason = _invalid_reason(
            product.product_status, sku.sku_status, store.store_status, inventory, item.quantity
        )
        item.sku_version = sku.version
        item.version += 1
        _touch(cart)
        await self.session.commit()
        return await self._fresh_view(user.id)

    async def delete(self, user: User, item_no: str, expected_version: int) -> CartView:
        cart = await self._cart_for_write(user.id, expected_version)
        item = await self.repository.item(cart.id, item_no, for_update=True)
        if item is None:
            raise _not_found()
        await self.session.delete(item)
        cart.item_count -= 1
        _touch(cart)
        await self.session.commit()
        return await self._fresh_view(user.id)

    async def replace_selection(
        self,
        user: User,
        payload: CartSelectionReplaceRequest,
        expected_version: int,
    ) -> CartView:
        cart = await self._cart_for_write(user.id, expected_version)
        items = await self.repository.items_by_nos(cart.id, payload.cart_item_ids)
        if len(items) != len(payload.cart_item_ids):
            raise _not_found()
        for item in items:
            item.is_selected = payload.is_selected
            item.version += 1
        _touch(cart)
        await self.session.commit()
        return await self._fresh_view(user.id)

    async def clear_invalid(self, user: User, expected_version: int) -> CartView:
        cart = await self._cart_for_write(user.id, expected_version)
        projections = await self.repository.projections(cart.id)
        removed = 0
        for item, sku, product, store, inventory in projections:
            reason = _invalid_reason(
                product.product_status, sku.sku_status, store.store_status, inventory, item.quantity
            )
            if reason is not None:
                await self.session.delete(item)
                removed += 1
        if removed:
            cart.item_count -= removed
            _touch(cart)
            await self.session.commit()
        else:
            await self.session.rollback()
        return await self._fresh_view(user.id)

    async def _cart_for_write(self, user_id: int, expected_version: int) -> Cart:
        cart = await self.repository.cart(user_id, for_update=True)
        if cart is None:
            raise _not_found()
        if cart.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="购物车已经变化，请刷新后重试。",
            )
        return cart

    async def _fresh_view(self, user_id: int) -> CartView:
        cart = await self.repository.cart(user_id)
        return await self._view(cart)

    async def _view(self, cart: Cart | None) -> CartView:
        if cart is None:
            return CartView(
                cart_id=None,
                groups=[],
                cart_total_quantity=0,
                selected_quantity=0,
                valid_item_count=0,
                amount_summary=CartAmountSummary(
                    selected_goods_amount=Money(minor_units="0", currency="CNY")
                ),
                version=0,
            )
        projections = await self.repository.projections(cart.id)
        groups: OrderedDict[int, tuple[str, str, list[CartItemView]]] = OrderedDict()
        total_quantity = selected_quantity = valid_count = selected_amount = 0
        for item, sku, product, store, inventory in projections:
            available = _available(inventory)
            reason = _invalid_reason(
                product.product_status, sku.sku_status, store.store_status, inventory, item.quantity
            )
            valid = reason is None
            total_quantity += item.quantity
            if valid:
                valid_count += 1
            if valid and item.is_selected:
                selected_quantity += item.quantity
                selected_amount += sku.sale_price_amount * item.quantity
            group = groups.setdefault(store.id, (store.store_no, store.store_name, []))
            group[2].append(
                CartItemView(
                    cart_item_id=item.cart_item_no,
                    product_id=product.product_no,
                    sku_id=sku.sku_no,
                    product_name=product.product_name,
                    sku_name=sku.sku_name,
                    spec_values=sku.spec_values,
                    quantity=item.quantity,
                    is_selected=item.is_selected,
                    added_price=Money(
                        minor_units=str(item.added_price_amount), currency=item.currency
                    ),
                    current_price=Money(
                        minor_units=str(sku.sale_price_amount), currency=sku.currency
                    ),
                    price_changed=(
                        item.added_price_amount != sku.sale_price_amount
                        or item.currency != sku.currency
                    ),
                    available_quantity=available,
                    is_valid=valid,
                    invalid_reason=reason,
                )
            )
        group_views: list[CartStoreGroupView] = []
        for store_no, store_name, items in groups.values():
            group_selected_quantity = sum(
                item.quantity for item in items if item.is_valid and item.is_selected
            )
            group_amount = sum(
                int(item.current_price.minor_units) * item.quantity
                for item in items
                if item.is_valid and item.is_selected
            )
            group_views.append(
                CartStoreGroupView(
                    store_id=store_no,
                    store_name=store_name,
                    items=items,
                    selected_quantity=group_selected_quantity,
                    selected_amount=Money(minor_units=str(group_amount), currency="CNY"),
                )
            )
        return CartView(
            cart_id=cart.cart_no,
            groups=group_views,
            cart_total_quantity=total_quantity,
            selected_quantity=selected_quantity,
            valid_item_count=valid_count,
            amount_summary=CartAmountSummary(
                selected_goods_amount=Money(minor_units=str(selected_amount), currency="CNY")
            ),
            version=cart.version,
        )


def _available(inventory: Inventory | None) -> int:
    if inventory is None:
        return 0
    return max(
        inventory.on_hand_quantity - inventory.reserved_quantity - inventory.safety_stock_quantity,
        0,
    )


def _invalid_reason(
    product_status: str,
    sku_status: str,
    store_status: str,
    inventory: Inventory | None,
    quantity: int,
) -> str | None:
    if store_status != "active":
        return "STORE_UNAVAILABLE"
    if product_status != "on_sale":
        return "PRODUCT_OFF_SHELF"
    if sku_status != "active":
        return "SKU_UNAVAILABLE"
    if inventory is None or inventory.inventory_status != "active":
        return "INVENTORY_UNAVAILABLE"
    if _available(inventory) < quantity:
        return "INSUFFICIENT_STOCK"
    return None


def _touch(cart: Cart) -> None:
    cart.last_activity_at = utc_now()
    cart.version += 1


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404, code="RESOURCE_NOT_FOUND", title="Resource not found", detail="未找到该资源。"
    )


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Cart conflict", detail=detail)
