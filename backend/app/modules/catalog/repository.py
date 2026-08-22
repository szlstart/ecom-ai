from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Select, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.pagination import CursorPosition
from app.modules.catalog.models import (
    Brand,
    Category,
    Product,
    ProductAttribute,
    ProductContentVersion,
    ProductFaq,
    ProductFaqVersion,
    ProductFavorite,
    ProductFulfillmentProfile,
    ProductImage,
    ProductSku,
)
from app.modules.files.models import FileObject
from app.modules.inventory.models import Inventory
from app.modules.stores.models import Store, StoreProductGroup, StoreProductGroupItem


class CatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search_products(
        self,
        *,
        q: str | None,
        category_no: str | None,
        brand_no: str | None,
        store_no: str | None,
        group_no: str | None,
        price_min: int | None,
        price_max: int | None,
        sort: str,
        position: CursorPosition | None,
        limit: int,
    ) -> tuple[list[tuple[Product, Store]], bool]:
        statement = (
            select(Product, Store)
            .join(Store, Store.id == Product.store_id)
            .outerjoin(Category, Category.id == Product.category_id)
            .outerjoin(Brand, Brand.id == Product.brand_id)
            .where(Product.product_status == "on_sale", Store.store_status == "active")
        )
        if q:
            term = f"%{_escape_like(q)}%"
            statement = statement.where(
                or_(
                    Product.product_name.like(term, escape="\\"),
                    Product.subtitle.like(term, escape="\\"),
                    Brand.brand_name.like(term, escape="\\"),
                )
            )
        if category_no:
            statement = statement.where(Category.category_no == category_no)
        if brand_no:
            statement = statement.where(Brand.brand_no == brand_no)
        if store_no:
            statement = statement.where(Store.store_no == store_no)
        if group_no:
            statement = statement.join(
                StoreProductGroupItem,
                StoreProductGroupItem.product_id == Product.id,
            ).join(
                StoreProductGroup,
                StoreProductGroup.id == StoreProductGroupItem.store_product_group_id,
            )
            statement = statement.where(
                StoreProductGroup.group_no == group_no,
                StoreProductGroup.group_status == "active",
                StoreProductGroup.store_id == Product.store_id,
            )
        if price_min is not None:
            statement = statement.where(Product.min_price_amount >= price_min)
        if price_max is not None:
            statement = statement.where(Product.min_price_amount <= price_max)

        statement, reverse_result = _apply_product_cursor(statement, sort, position)
        rows = list((await self.session.execute(statement.limit(limit + 1))).all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        if reverse_result:
            rows.reverse()
        return [(row[0], row[1]) for row in rows], has_more

    async def public_product(self, product_no: str) -> tuple[Product, Store] | None:
        row = (
            await self.session.execute(
                select(Product, Store)
                .join(Store, Store.id == Product.store_id)
                .where(
                    Product.product_no == product_no,
                    Product.product_status == "on_sale",
                    Store.store_status == "active",
                )
            )
        ).one_or_none()
        return None if row is None else (row[0], row[1])

    async def main_images(
        self, product_ids: Sequence[int]
    ) -> dict[int, tuple[ProductImage, FileObject]]:
        if not product_ids:
            return {}
        rows = (
            await self.session.execute(
                select(ProductImage, FileObject)
                .join(FileObject, FileObject.id == ProductImage.file_id)
                .where(
                    ProductImage.product_id.in_(product_ids),
                    ProductImage.sku_id.is_(None),
                    ProductImage.image_type == "main",
                    ProductImage.image_status == "active",
                    FileObject.file_status == "active",
                    FileObject.scan_status == "safe",
                )
            )
        ).all()
        return {row[0].product_id: (row[0], row[1]) for row in rows}

    async def product_images(
        self, product_id: int
    ) -> tuple[
        list[tuple[ProductImage, FileObject]], dict[int, list[tuple[ProductImage, FileObject]]]
    ]:
        rows = (
            await self.session.execute(
                select(ProductImage, FileObject)
                .join(FileObject, FileObject.id == ProductImage.file_id)
                .where(
                    ProductImage.product_id == product_id,
                    ProductImage.image_status == "active",
                    FileObject.file_status == "active",
                    FileObject.scan_status == "safe",
                )
                .order_by(ProductImage.sort_order, ProductImage.id)
            )
        ).all()
        public_images: list[tuple[ProductImage, FileObject]] = []
        sku_images: dict[int, list[tuple[ProductImage, FileObject]]] = {}
        for image, file_object in rows:
            if image.sku_id is None:
                public_images.append((image, file_object))
            else:
                sku_images.setdefault(image.sku_id, []).append((image, file_object))
        return public_images, sku_images

    async def public_skus(self, product_id: int) -> list[tuple[ProductSku, Inventory | None]]:
        rows = (
            await self.session.execute(
                select(ProductSku, Inventory)
                .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
                .where(ProductSku.product_id == product_id, ProductSku.sku_status == "active")
                .order_by(ProductSku.id)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def product_attributes(self, product_id: int) -> list[ProductAttribute]:
        return list(
            (
                await self.session.scalars(
                    select(ProductAttribute)
                    .where(ProductAttribute.product_id == product_id)
                    .order_by(ProductAttribute.sort_order, ProductAttribute.id)
                )
            ).all()
        )

    async def published_content(self, product: Product) -> ProductContentVersion | None:
        if product.published_detail_content_version_id is None:
            return None
        return cast(
            ProductContentVersion | None,
            await self.session.scalar(
                select(ProductContentVersion).where(
                    ProductContentVersion.id == product.published_detail_content_version_id,
                    ProductContentVersion.product_id == product.id,
                    ProductContentVersion.version_status == "published",
                    ProductContentVersion.security_scan_status == "passed",
                )
            ),
        )

    async def fulfillment_profile(self, product_id: int) -> ProductFulfillmentProfile | None:
        return cast(
            ProductFulfillmentProfile | None,
            await self.session.scalar(
                select(ProductFulfillmentProfile).where(
                    ProductFulfillmentProfile.product_id == product_id
                )
            ),
        )

    async def public_faqs(self, product_id: int) -> list[tuple[ProductFaq, ProductFaqVersion]]:
        result = await self.session.execute(
            select(ProductFaq, ProductFaqVersion)
            .join(
                ProductFaqVersion,
                ProductFaqVersion.id == ProductFaq.published_content_version_id,
            )
            .where(
                ProductFaq.product_id == product_id,
                ProductFaq.faq_status == "published",
                ProductFaqVersion.version_status == "published",
                ProductFaqVersion.security_scan_status == "passed",
            )
            .order_by(ProductFaq.sort_order, ProductFaq.id)
        )
        return [(row[0], row[1]) for row in result.all()]

    async def favorite_product_ids(self, user_id: int, product_ids: Sequence[int]) -> set[int]:
        if not product_ids:
            return set()
        return set(
            (
                await self.session.scalars(
                    select(ProductFavorite.product_id).where(
                        ProductFavorite.user_id == user_id,
                        ProductFavorite.product_id.in_(product_ids),
                        ProductFavorite.deleted_at.is_(None),
                    )
                )
            ).all()
        )

    async def favorite_by_user_product(
        self, user_id: int, product_id: int, *, for_update: bool = False
    ) -> ProductFavorite | None:
        statement = select(ProductFavorite).where(
            ProductFavorite.user_id == user_id,
            ProductFavorite.product_id == product_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(ProductFavorite | None, await self.session.scalar(statement))

    async def favorite_products(self, user_id: int, limit: int) -> list[tuple[Product, Store]]:
        rows = (
            await self.session.execute(
                select(Product, Store)
                .join(ProductFavorite, ProductFavorite.product_id == Product.id)
                .join(Store, Store.id == Product.store_id)
                .where(ProductFavorite.user_id == user_id, ProductFavorite.deleted_at.is_(None))
                .order_by(ProductFavorite.favorited_at.desc(), ProductFavorite.id.desc())
                .limit(limit)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def category_by_id(self, category_id: int) -> Category | None:
        return cast(
            Category | None,
            await self.session.scalar(select(Category).where(Category.id == category_id)),
        )

    async def brand_by_id(self, brand_id: int | None) -> Brand | None:
        if brand_id is None:
            return None
        return cast(
            Brand | None,
            await self.session.scalar(select(Brand).where(Brand.id == brand_id)),
        )

    async def public_file_by_object_key(self, object_key: str | None) -> FileObject | None:
        if object_key is None:
            return None
        return cast(
            FileObject | None,
            await self.session.scalar(
                select(FileObject).where(
                    FileObject.object_key == object_key,
                    FileObject.visibility == "public_derivative",
                    FileObject.file_status == "active",
                    FileObject.scan_status == "safe",
                )
            ),
        )

    async def categories(self) -> list[Category]:
        return list(
            (
                await self.session.scalars(
                    select(Category)
                    .where(Category.category_status == "active")
                    .order_by(Category.level, Category.sort_order, Category.id)
                )
            ).all()
        )

    async def brands(self, q: str | None, limit: int) -> list[Brand]:
        statement = select(Brand).where(Brand.brand_status == "active")
        if q:
            statement = statement.where(Brand.brand_name.like(f"%{_escape_like(q)}%", escape="\\"))
        return list(
            (
                await self.session.scalars(
                    statement.order_by(Brand.brand_name_normalized, Brand.id).limit(limit)
                )
            ).all()
        )

    async def suggestions(self, q: str, limit: int) -> list[str]:
        return list(
            (
                await self.session.scalars(
                    select(Product.product_name)
                    .join(Store, Store.id == Product.store_id)
                    .where(
                        Product.product_status == "on_sale",
                        Store.store_status == "active",
                        Product.product_name.like(f"%{_escape_like(q)}%", escape="\\"),
                    )
                    .group_by(Product.product_name)
                    .order_by(func.max(Product.sales_count).desc(), Product.product_name)
                    .limit(limit)
                )
            ).all()
        )


def _apply_product_cursor(
    statement: Select[tuple[Product, Store]],
    sort: str,
    position: CursorPosition | None,
) -> tuple[Select[tuple[Product, Store]], bool]:
    reverse_result = position is not None and position.direction == "previous"
    descending = sort in {"relevance", "sales", "newest", "price_desc"}
    if reverse_result:
        descending = not descending

    sort_column: ColumnElement[Any]
    parse_value: Callable[[str], Any]
    if sort == "newest":
        sort_column = cast(
            ColumnElement[Any], func.coalesce(Product.published_at, Product.created_at)
        )
        parse_value = datetime.fromisoformat
    elif sort in {"sales", "relevance"}:
        sort_column = cast(ColumnElement[Any], Product.sales_count)
        parse_value = int
    else:
        sort_column = cast(ColumnElement[Any], Product.min_price_amount)
        parse_value = int

    if position is not None:
        if len(position.values) != 2:
            raise ValueError("product cursor must contain two values")
        sort_value = parse_value(position.values[0])
        product_id = int(position.values[1])
        key = tuple_(sort_column, Product.id)
        cursor_key = (sort_value, product_id)
        statement = statement.where(key < cursor_key if descending else key > cursor_key)

    order = sort_column.desc() if descending else sort_column.asc()
    id_order = Product.id.desc() if descending else Product.id.asc()
    return statement.order_by(order, id_order), reverse_result


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
