from __future__ import annotations

from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import (
    Brand,
    Category,
    Product,
    ProductAttribute,
    ProductContentVersion,
    ProductFaq,
    ProductFaqVersion,
    ProductFulfillmentProfile,
    ProductImage,
    ProductSku,
    ProductStatusLog,
)
from app.modules.files.models import FileObject
from app.modules.inventory.models import Inventory
from app.modules.stores.models import ShippingTemplate, Store

ProductRow = tuple[Product, Store, Category, Brand | None]


class ProductAdminRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def products(
        self,
        scopes: tuple[tuple[str, int], ...],
        *,
        store_no: str | None,
        product_status: str | None,
        q: str | None,
        cursor_no: str | None,
        limit: int,
    ) -> list[ProductRow]:
        statement = (
            select(Product, Store, Category, Brand)
            .join(Store, Store.id == Product.store_id)
            .join(Category, Category.id == Product.category_id)
            .outerjoin(Brand, Brand.id == Product.brand_id)
        )
        if ("platform", 0) not in scopes:
            store_ids = [scope_id for scope_type, scope_id in scopes if scope_type == "store"]
            if not store_ids:
                return []
            statement = statement.where(Product.store_id.in_(store_ids))
        if store_no:
            statement = statement.where(Store.store_no == store_no)
        if product_status:
            statement = statement.where(Product.product_status == product_status)
        if q:
            term = f"%{_escape_like(q)}%"
            statement = statement.where(Product.product_name.like(term, escape="\\"))
        if cursor_no:
            statement = statement.where(Product.product_no > cursor_no)
        rows = (
            await self.session.execute(statement.order_by(Product.product_no).limit(limit + 1))
        ).all()
        return [(row[0], row[1], row[2], row[3]) for row in rows]

    async def product_by_no(
        self, product_no: str, *, for_update: bool = False
    ) -> ProductRow | None:
        statement = (
            select(Product, Store, Category, Brand)
            .join(Store, Store.id == Product.store_id)
            .join(Category, Category.id == Product.category_id)
            .outerjoin(Brand, Brand.id == Product.brand_id)
            .where(Product.product_no == product_no)
        )
        if for_update:
            statement = statement.with_for_update(of=Product)
        row = (await self.session.execute(statement)).one_or_none()
        return None if row is None else (row[0], row[1], row[2], row[3])

    async def store_by_no(self, store_no: str) -> Store | None:
        return cast(
            Store | None,
            await self.session.scalar(select(Store).where(Store.store_no == store_no)),
        )

    async def category_by_no(self, category_no: str) -> Category | None:
        return cast(
            Category | None,
            await self.session.scalar(select(Category).where(Category.category_no == category_no)),
        )

    async def brand_by_no(self, brand_no: str) -> Brand | None:
        return cast(
            Brand | None,
            await self.session.scalar(select(Brand).where(Brand.brand_no == brand_no)),
        )

    async def skus(self, product_id: int, *, for_update: bool = False) -> list[ProductSku]:
        statement = (
            select(ProductSku).where(ProductSku.product_id == product_id).order_by(ProductSku.id)
        )
        if for_update:
            statement = statement.with_for_update()
        return list((await self.session.scalars(statement)).all())

    async def sku_by_no(
        self, product_id: int, sku_no: str, *, for_update: bool = False
    ) -> ProductSku | None:
        statement = select(ProductSku).where(
            ProductSku.product_id == product_id,
            ProductSku.sku_no == sku_no,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(ProductSku | None, await self.session.scalar(statement))

    async def images(self, product_id: int) -> list[tuple[ProductImage, FileObject]]:
        rows = (
            await self.session.execute(
                select(ProductImage, FileObject)
                .join(FileObject, FileObject.id == ProductImage.file_id)
                .where(ProductImage.product_id == product_id)
                .order_by(ProductImage.sku_id, ProductImage.sort_order, ProductImage.id)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def attributes(self, product_id: int) -> list[ProductAttribute]:
        return list(
            (
                await self.session.scalars(
                    select(ProductAttribute)
                    .where(ProductAttribute.product_id == product_id)
                    .order_by(ProductAttribute.sort_order, ProductAttribute.id)
                )
            ).all()
        )

    async def fulfillment(self, product_id: int) -> ProductFulfillmentProfile | None:
        return cast(
            ProductFulfillmentProfile | None,
            await self.session.scalar(
                select(ProductFulfillmentProfile).where(
                    ProductFulfillmentProfile.product_id == product_id
                )
            ),
        )

    async def fulfillment_with_template(
        self, product_id: int
    ) -> tuple[ProductFulfillmentProfile, ShippingTemplate] | None:
        row = (
            await self.session.execute(
                select(ProductFulfillmentProfile, ShippingTemplate)
                .join(
                    ShippingTemplate,
                    ShippingTemplate.id == ProductFulfillmentProfile.shipping_template_id,
                )
                .where(ProductFulfillmentProfile.product_id == product_id)
            )
        ).one_or_none()
        return (row[0], row[1]) if row else None

    async def shipping_template(self, store_id: int, template_no: str) -> ShippingTemplate | None:
        return cast(
            ShippingTemplate | None,
            await self.session.scalar(
                select(ShippingTemplate).where(
                    ShippingTemplate.store_id == store_id,
                    ShippingTemplate.template_no == template_no,
                )
            ),
        )

    async def files_by_nos(self, file_nos: list[str]) -> list[FileObject]:
        if not file_nos:
            return []
        return list(
            (
                await self.session.scalars(
                    select(FileObject).where(FileObject.file_no.in_(file_nos))
                )
            ).all()
        )

    async def next_content_version(self, product_id: int) -> int:
        current = await self.session.scalar(
            select(func.max(ProductContentVersion.content_version)).where(
                ProductContentVersion.product_id == product_id
            )
        )
        return int(current or 0) + 1

    async def content_version_by_no(
        self, product_id: int, version_no: str
    ) -> ProductContentVersion | None:
        return cast(
            ProductContentVersion | None,
            await self.session.scalar(
                select(ProductContentVersion).where(
                    ProductContentVersion.product_id == product_id,
                    ProductContentVersion.content_version_no == version_no,
                )
            ),
        )

    async def content_version_by_id(
        self, product_id: int, version_id: int | None
    ) -> ProductContentVersion | None:
        if version_id is None:
            return None
        return cast(
            ProductContentVersion | None,
            await self.session.scalar(
                select(ProductContentVersion).where(
                    ProductContentVersion.product_id == product_id,
                    ProductContentVersion.id == version_id,
                )
            ),
        )

    async def faqs(self, product_id: int) -> list[ProductFaq]:
        return list(
            (
                await self.session.scalars(
                    select(ProductFaq)
                    .where(ProductFaq.product_id == product_id)
                    .order_by(ProductFaq.sort_order, ProductFaq.id)
                )
            ).all()
        )

    async def faq_by_no(
        self, product_id: int, faq_no: str, *, for_update: bool = False
    ) -> ProductFaq | None:
        statement = select(ProductFaq).where(
            ProductFaq.product_id == product_id,
            ProductFaq.faq_no == faq_no,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(ProductFaq | None, await self.session.scalar(statement))

    async def next_faq_version(self, faq_id: int) -> int:
        current = await self.session.scalar(
            select(func.max(ProductFaqVersion.content_version)).where(
                ProductFaqVersion.product_faq_id == faq_id
            )
        )
        return int(current or 0) + 1

    async def faq_version_by_no(self, faq_id: int, version_no: str) -> ProductFaqVersion | None:
        return cast(
            ProductFaqVersion | None,
            await self.session.scalar(
                select(ProductFaqVersion).where(
                    ProductFaqVersion.product_faq_id == faq_id,
                    ProductFaqVersion.faq_version_no == version_no,
                )
            ),
        )

    async def faq_version_by_id(self, faq_id: int, version_id: int) -> ProductFaqVersion | None:
        return cast(
            ProductFaqVersion | None,
            await self.session.scalar(
                select(ProductFaqVersion).where(
                    ProductFaqVersion.product_faq_id == faq_id,
                    ProductFaqVersion.id == version_id,
                )
            ),
        )

    async def latest_status_log(self, product_id: int) -> ProductStatusLog | None:
        return cast(
            ProductStatusLog | None,
            await self.session.scalar(
                select(ProductStatusLog)
                .where(ProductStatusLog.product_id == product_id)
                .order_by(ProductStatusLog.id.desc())
                .limit(1)
            ),
        )

    async def products_using_sku(self, sku_id: int) -> int:
        return int(
            await self.session.scalar(
                select(func.count(Product.id)).where(Product.default_sku_id == sku_id)
            )
            or 0
        )

    async def recalculate_price_bounds(self, product: Product) -> None:
        row = (
            await self.session.execute(
                select(
                    func.min(ProductSku.sale_price_amount),
                    func.max(ProductSku.sale_price_amount),
                ).where(
                    ProductSku.product_id == product.id,
                    ProductSku.sku_status == "active",
                )
            )
        ).one()
        product.min_price_amount = int(row[0] or 0)
        product.max_price_amount = int(row[1] or 0)

    async def inventory_for_sku(self, sku_id: int) -> Inventory | None:
        return cast(
            Inventory | None,
            await self.session.scalar(select(Inventory).where(Inventory.sku_id == sku_id)),
        )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
