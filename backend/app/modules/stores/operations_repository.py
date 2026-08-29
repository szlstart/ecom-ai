from __future__ import annotations

from typing import cast

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import Product
from app.modules.stores.models import (
    ShippingTemplate,
    ShippingTemplateRule,
    Store,
    StoreAnnouncement,
    StoreFeaturedProduct,
    StoreProductGroup,
    StoreProductGroupItem,
)
from app.modules.stores.operations_schemas import (
    AdminFeaturedProductInput,
    AdminShippingRuleInput,
)


class StoreOperationsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def store(self, store_no: str, *, for_update: bool = False) -> Store | None:
        statement = select(Store).where(Store.store_no == store_no)
        if for_update:
            statement = statement.with_for_update()
        return cast(Store | None, await self.session.scalar(statement))

    async def groups(self, store_id: int) -> list[StoreProductGroup]:
        return list(
            (
                await self.session.scalars(
                    select(StoreProductGroup)
                    .where(StoreProductGroup.store_id == store_id)
                    .order_by(
                        StoreProductGroup.parent_id,
                        StoreProductGroup.sort_order,
                        StoreProductGroup.id,
                    )
                )
            ).all()
        )

    async def group(
        self, store_id: int, group_no: str, *, for_update: bool = False
    ) -> StoreProductGroup | None:
        statement = select(StoreProductGroup).where(
            StoreProductGroup.store_id == store_id,
            StoreProductGroup.group_no == group_no,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(StoreProductGroup | None, await self.session.scalar(statement))

    async def group_items(self, group_ids: list[int]) -> dict[int, list[Product]]:
        if not group_ids:
            return {}
        rows = (
            await self.session.execute(
                select(StoreProductGroupItem.store_product_group_id, Product)
                .join(Product, Product.id == StoreProductGroupItem.product_id)
                .where(StoreProductGroupItem.store_product_group_id.in_(group_ids))
                .order_by(
                    StoreProductGroupItem.store_product_group_id, StoreProductGroupItem.sort_order
                )
            )
        ).all()
        result: dict[int, list[Product]] = {}
        for group_id, product in rows:
            result.setdefault(group_id, []).append(product)
        return result

    async def replace_group_items(self, group: StoreProductGroup, products: list[Product]) -> None:
        await self.session.execute(
            delete(StoreProductGroupItem).where(
                StoreProductGroupItem.store_product_group_id == group.id
            )
        )
        self.session.add_all(
            [
                StoreProductGroupItem(
                    store_product_group_id=group.id,
                    product_id=product.id,
                    store_id=group.store_id,
                    sort_order=index,
                )
                for index, product in enumerate(products)
            ]
        )

    async def products(self, store_id: int, product_nos: list[str]) -> list[Product]:
        if not product_nos:
            return []
        return list(
            (
                await self.session.scalars(
                    select(Product).where(
                        Product.store_id == store_id, Product.product_no.in_(product_nos)
                    )
                )
            ).all()
        )

    async def shipping_templates(self, store_id: int) -> list[ShippingTemplate]:
        return list(
            (
                await self.session.scalars(
                    select(ShippingTemplate)
                    .where(ShippingTemplate.store_id == store_id)
                    .order_by(
                        ShippingTemplate.template_family_no, ShippingTemplate.policy_version.desc()
                    )
                )
            ).all()
        )

    async def shipping_template(
        self, store_id: int, template_no: str, *, for_update: bool = False
    ) -> ShippingTemplate | None:
        statement = select(ShippingTemplate).where(
            ShippingTemplate.store_id == store_id,
            ShippingTemplate.template_no == template_no,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(ShippingTemplate | None, await self.session.scalar(statement))

    async def shipping_rules(
        self, template_ids: list[int]
    ) -> dict[int, list[ShippingTemplateRule]]:
        if not template_ids:
            return {}
        rows = list(
            (
                await self.session.scalars(
                    select(ShippingTemplateRule)
                    .where(ShippingTemplateRule.shipping_template_id.in_(template_ids))
                    .order_by(ShippingTemplateRule.shipping_template_id, ShippingTemplateRule.id)
                )
            ).all()
        )
        result: dict[int, list[ShippingTemplateRule]] = {}
        for rule in rows:
            result.setdefault(rule.shipping_template_id, []).append(rule)
        return result

    async def next_shipping_version(self, store_id: int, family_no: str) -> int:
        value = await self.session.scalar(
            select(func.max(ShippingTemplate.policy_version)).where(
                ShippingTemplate.store_id == store_id,
                ShippingTemplate.template_family_no == family_no,
            )
        )
        return int(value or 0) + 1

    async def replace_shipping_rules(
        self, template: ShippingTemplate, rules: list[AdminShippingRuleInput]
    ) -> None:
        await self.session.execute(
            delete(ShippingTemplateRule).where(
                ShippingTemplateRule.shipping_template_id == template.id
            )
        )
        for raw in rules:
            data = raw.model_dump()
            self.session.add(ShippingTemplateRule(shipping_template_id=template.id, **data))

    async def effective_shipping_template(
        self, store_id: int, family_no: str, *, for_update: bool = False
    ) -> ShippingTemplate | None:
        statement = select(ShippingTemplate).where(
            ShippingTemplate.store_id == store_id,
            ShippingTemplate.template_family_no == family_no,
            ShippingTemplate.template_status == "effective",
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(ShippingTemplate | None, await self.session.scalar(statement))

    async def announcements(self, store_id: int) -> list[StoreAnnouncement]:
        return list(
            (
                await self.session.scalars(
                    select(StoreAnnouncement)
                    .where(StoreAnnouncement.store_id == store_id)
                    .order_by(StoreAnnouncement.sort_order, StoreAnnouncement.id.desc())
                )
            ).all()
        )

    async def announcement(
        self, store_id: int, announcement_no: str, *, for_update: bool = False
    ) -> StoreAnnouncement | None:
        statement = select(StoreAnnouncement).where(
            StoreAnnouncement.store_id == store_id,
            StoreAnnouncement.announcement_no == announcement_no,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(StoreAnnouncement | None, await self.session.scalar(statement))

    async def featured(
        self, store_id: int, slot_type: str
    ) -> list[tuple[StoreFeaturedProduct, Product]]:
        rows = (
            await self.session.execute(
                select(StoreFeaturedProduct, Product)
                .join(Product, Product.id == StoreFeaturedProduct.product_id)
                .where(
                    StoreFeaturedProduct.store_id == store_id,
                    StoreFeaturedProduct.slot_type == slot_type,
                )
                .order_by(StoreFeaturedProduct.sort_order, StoreFeaturedProduct.id)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def replace_featured(
        self,
        store_id: int,
        slot_type: str,
        items: list[tuple[Product, AdminFeaturedProductInput]],
    ) -> None:
        await self.session.execute(
            delete(StoreFeaturedProduct).where(
                StoreFeaturedProduct.store_id == store_id,
                StoreFeaturedProduct.slot_type == slot_type,
            )
        )
        for index, (product, raw) in enumerate(items):
            self.session.add(
                StoreFeaturedProduct(
                    store_id=store_id,
                    product_id=product.id,
                    slot_type=slot_type,
                    sort_order=index,
                    starts_at=raw.starts_at,
                    ends_at=raw.ends_at,
                )
            )
