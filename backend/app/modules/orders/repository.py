from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.cart.models import Cart, CartItem
from app.modules.catalog.models import ProductImage
from app.modules.inventory.models import Inventory
from app.modules.orders.models import TradeOrder


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

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

    async def main_images(self, product_ids: set[int]) -> dict[int, str]:
        if not product_ids:
            return {}
        rows = (
            await self.session.execute(
                select(ProductImage.product_id, ProductImage.object_key)
                .where(
                    ProductImage.product_id.in_(product_ids),
                    ProductImage.sku_id.is_(None),
                    ProductImage.image_type == "main",
                    ProductImage.image_status == "active",
                )
                .order_by(ProductImage.product_id, ProductImage.sort_order, ProductImage.id)
            )
        ).all()
        result: dict[int, str] = {}
        for product_id, object_key in rows:
            result.setdefault(product_id, object_key)
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
