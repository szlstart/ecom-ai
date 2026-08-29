from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.cart.models import Cart, CartItem
from app.modules.catalog.models import Product, ProductImage, ProductSku
from app.modules.files.models import FileObject
from app.modules.inventory.models import Inventory
from app.modules.stores.models import Store

CartProjection = tuple[CartItem, ProductSku, Product, Store, Inventory | None]


class CartRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def cart(self, user_id: int, *, for_update: bool = False) -> Cart | None:
        statement = select(Cart).where(Cart.user_id == user_id)
        if for_update:
            statement = statement.with_for_update()
        return cast(Cart | None, await self.session.scalar(statement))

    async def item(
        self, cart_id: int, item_no: str, *, for_update: bool = False
    ) -> CartItem | None:
        statement = select(CartItem).where(
            CartItem.cart_id == cart_id, CartItem.cart_item_no == item_no
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(CartItem | None, await self.session.scalar(statement))

    async def item_for_sku(
        self, cart_id: int, sku_id: int, *, for_update: bool = False
    ) -> CartItem | None:
        statement = select(CartItem).where(CartItem.cart_id == cart_id, CartItem.sku_id == sku_id)
        if for_update:
            statement = statement.with_for_update()
        return cast(CartItem | None, await self.session.scalar(statement))

    async def sku_context(
        self, sku_no: str, *, for_update: bool = False
    ) -> tuple[ProductSku, Product, Store, Inventory | None] | None:
        statement = (
            select(ProductSku, Product, Store, Inventory)
            .join(Product, Product.id == ProductSku.product_id)
            .join(Store, Store.id == ProductSku.store_id)
            .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
            .where(ProductSku.sku_no == sku_no)
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).one_or_none()
        return cast(tuple[ProductSku, Product, Store, Inventory | None] | None, row)

    async def sku_context_by_id(
        self, sku_id: int, *, for_update: bool = False
    ) -> tuple[ProductSku, Product, Store, Inventory | None] | None:
        statement = (
            select(ProductSku, Product, Store, Inventory)
            .join(Product, Product.id == ProductSku.product_id)
            .join(Store, Store.id == ProductSku.store_id)
            .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
            .where(ProductSku.id == sku_id)
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).one_or_none()
        return cast(tuple[ProductSku, Product, Store, Inventory | None] | None, row)

    async def projections(self, cart_id: int) -> list[CartProjection]:
        rows = (
            await self.session.execute(
                select(CartItem, ProductSku, Product, Store, Inventory)
                .join(ProductSku, ProductSku.id == CartItem.sku_id)
                .join(Product, Product.id == ProductSku.product_id)
                .join(Store, Store.id == ProductSku.store_id)
                .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
                .where(CartItem.cart_id == cart_id)
                .order_by(Store.id, CartItem.id)
            )
        ).all()
        return cast(list[CartProjection], rows)

    async def sku_image_urls(self, sku_ids: set[int]) -> dict[int, str]:
        if not sku_ids:
            return {}
        rows = (
            await self.session.execute(
                select(ProductImage.sku_id, FileObject.file_no)
                .join(FileObject, FileObject.id == ProductImage.file_id)
                .where(
                    ProductImage.sku_id.in_(sku_ids),
                    ProductImage.image_type == "spec",
                    ProductImage.image_status == "active",
                    FileObject.file_status == "active",
                    FileObject.scan_status == "safe",
                )
                .order_by(ProductImage.sku_id, ProductImage.sort_order, ProductImage.id)
            )
        ).all()
        result: dict[int, str] = {}
        for sku_id, file_no in rows:
            result.setdefault(sku_id, f"/api/v1/files/{file_no}")
        return result

    async def public_file_urls(self, object_keys: set[str]) -> dict[str, str]:
        if not object_keys:
            return {}
        rows = (
            await self.session.execute(
                select(FileObject.object_key, FileObject.file_no).where(
                    FileObject.object_key.in_(object_keys),
                    FileObject.file_status == "active",
                    FileObject.scan_status == "safe",
                )
            )
        ).all()
        return {object_key: f"/api/v1/files/{file_no}" for object_key, file_no in rows}

    async def items_by_nos(self, cart_id: int, item_nos: list[str]) -> list[CartItem]:
        return list(
            (
                await self.session.scalars(
                    select(CartItem)
                    .where(CartItem.cart_id == cart_id, CartItem.cart_item_no.in_(item_nos))
                    .order_by(CartItem.id)
                    .with_for_update()
                )
            ).all()
        )
