from __future__ import annotations

from typing import cast

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import Brand, Category, Product, ProductSku
from app.modules.files.models import FileObject
from app.modules.inventory.models import Inventory, InventoryLog
from app.modules.stores.models import Store


class AdminCatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def category_by_no(
        self, category_no: str, *, for_update: bool = False
    ) -> Category | None:
        statement = select(Category).where(Category.category_no == category_no)
        if for_update:
            statement = statement.with_for_update()
        return cast(Category | None, await self.session.scalar(statement))

    async def categories(self) -> list[Category]:
        return list(
            (
                await self.session.scalars(
                    select(Category).order_by(Category.level, Category.sort_order, Category.id)
                )
            ).all()
        )

    async def category_descendants(self, category: Category) -> list[Category]:
        prefix = f"{category.path}/%"
        return list(
            (
                await self.session.scalars(
                    select(Category)
                    .where(Category.path.like(prefix))
                    .order_by(Category.level, Category.id)
                    .with_for_update()
                )
            ).all()
        )

    async def brand_by_no(self, brand_no: str, *, for_update: bool = False) -> Brand | None:
        statement = select(Brand).where(Brand.brand_no == brand_no)
        if for_update:
            statement = statement.with_for_update()
        return cast(Brand | None, await self.session.scalar(statement))

    async def brands(self) -> list[Brand]:
        return list(
            (
                await self.session.scalars(
                    select(Brand).order_by(Brand.brand_name_normalized, Brand.id)
                )
            ).all()
        )

    async def active_file(self, file_no: str, purpose: str) -> FileObject | None:
        return cast(
            FileObject | None,
            await self.session.scalar(
                select(FileObject).where(
                    FileObject.file_no == file_no,
                    FileObject.purpose == purpose,
                    FileObject.file_status == "active",
                    FileObject.scan_status == "safe",
                    FileObject.visibility == "public_derivative",
                )
            ),
        )

    async def files_by_object_keys(self, object_keys: list[str]) -> dict[str, FileObject]:
        if not object_keys:
            return {}
        files = list(
            (
                await self.session.scalars(
                    select(FileObject).where(
                        FileObject.object_key.in_(object_keys),
                        FileObject.file_status == "active",
                        FileObject.scan_status == "safe",
                        FileObject.visibility == "public_derivative",
                    )
                )
            ).all()
        )
        return {item.object_key: item for item in files}

    async def inventory_by_sku_no(
        self, sku_no: str, *, for_update: bool = False
    ) -> tuple[Inventory, ProductSku, Product, Store] | None:
        statement = (
            select(Inventory, ProductSku, Product, Store)
            .join(ProductSku, ProductSku.id == Inventory.sku_id)
            .join(Product, Product.id == ProductSku.product_id)
            .join(Store, Store.id == Product.store_id)
            .where(ProductSku.sku_no == sku_no)
        )
        if for_update:
            statement = statement.with_for_update(of=Inventory)
        row = (await self.session.execute(statement)).one_or_none()
        return None if row is None else (row[0], row[1], row[2], row[3])

    async def inventories(
        self,
        scopes: tuple[tuple[str, int], ...],
        *,
        store_no: str | None,
        product_no: str | None,
        q: str | None,
        limit: int,
    ) -> list[tuple[Inventory, ProductSku, Product, Store]]:
        statement = (
            select(Inventory, ProductSku, Product, Store)
            .join(ProductSku, ProductSku.id == Inventory.sku_id)
            .join(Product, Product.id == ProductSku.product_id)
            .join(Store, Store.id == Product.store_id)
        )
        if ("platform", 0) not in scopes:
            store_ids = [scope_id for scope_type, scope_id in scopes if scope_type == "store"]
            if not store_ids:
                return []
            statement = statement.where(Store.id.in_(store_ids))
        if store_no:
            statement = statement.where(Store.store_no == store_no)
        if product_no:
            statement = statement.where(Product.product_no == product_no)
        if q:
            term = f"%{_escape_like(q)}%"
            statement = statement.where(
                or_(
                    Product.product_name.like(term, escape="\\"),
                    ProductSku.sku_name.like(term, escape="\\"),
                    ProductSku.merchant_sku_code.like(term, escape="\\"),
                )
            )
        rows = (
            await self.session.execute(
                statement.order_by(Store.id, Product.id, ProductSku.id).limit(limit)
            )
        ).all()
        return [(row[0], row[1], row[2], row[3]) for row in rows]

    async def inventory_logs(
        self,
        scopes: tuple[tuple[str, int], ...],
        *,
        sku_no: str | None,
        limit: int,
    ) -> list[tuple[InventoryLog, ProductSku, Product, Store]]:
        statement = (
            select(InventoryLog, ProductSku, Product, Store)
            .join(ProductSku, ProductSku.id == InventoryLog.sku_id)
            .join(Product, Product.id == ProductSku.product_id)
            .join(Store, Store.id == Product.store_id)
        )
        if ("platform", 0) not in scopes:
            store_ids = [scope_id for scope_type, scope_id in scopes if scope_type == "store"]
            if not store_ids:
                return []
            statement = statement.where(Store.id.in_(store_ids))
        if sku_no:
            statement = statement.where(ProductSku.sku_no == sku_no)
        rows = (
            await self.session.execute(
                statement.order_by(InventoryLog.created_at.desc(), InventoryLog.id.desc()).limit(
                    limit
                )
            )
        ).all()
        return [(row[0], row[1], row[2], row[3]) for row in rows]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
