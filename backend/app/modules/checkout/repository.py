from __future__ import annotations

from typing import cast

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utc_now
from app.modules.cart.models import Cart, CartItem
from app.modules.catalog.models import Product, ProductFulfillmentProfile, ProductImage, ProductSku
from app.modules.checkout.models import CheckoutSession, CheckoutSnapshot
from app.modules.files.models import FileObject
from app.modules.identity.models import UserAddress
from app.modules.inventory.models import Inventory
from app.modules.stores.models import (
    ShippingTemplate,
    ShippingTemplateRule,
    Store,
    StoreServicePolicy,
)

ItemContext = tuple[
    CartItem | None,
    ProductSku,
    Product,
    Store,
    Inventory | None,
    ProductFulfillmentProfile | None,
    ShippingTemplate | None,
]


class CheckoutRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def checkout(
        self, user_id: int, checkout_no: str, *, for_update: bool = False
    ) -> CheckoutSession | None:
        statement = select(CheckoutSession).where(
            CheckoutSession.user_id == user_id, CheckoutSession.checkout_no == checkout_no
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(CheckoutSession | None, await self.session.scalar(statement))

    async def current_snapshot(self, checkout_id: int) -> CheckoutSnapshot | None:
        return cast(
            CheckoutSnapshot | None,
            await self.session.scalar(
                select(CheckoutSnapshot).where(
                    CheckoutSnapshot.checkout_session_id == checkout_id,
                    CheckoutSnapshot.invalidated_at.is_(None),
                )
            ),
        )

    async def address(self, user_id: int, address_no: str) -> UserAddress | None:
        return cast(
            UserAddress | None,
            await self.session.scalar(
                select(UserAddress).where(
                    UserAddress.user_id == user_id,
                    UserAddress.address_no == address_no,
                    UserAddress.deleted_at.is_(None),
                )
            ),
        )

    async def default_address(self, user_id: int) -> UserAddress | None:
        return cast(
            UserAddress | None,
            await self.session.scalar(
                select(UserAddress).where(
                    UserAddress.user_id == user_id,
                    UserAddress.is_default.is_(True),
                    UserAddress.deleted_at.is_(None),
                )
            ),
        )

    async def buy_now_context(self, sku_no: str) -> ItemContext | None:
        row = (
            await self.session.execute(
                select(
                    ProductSku,
                    Product,
                    Store,
                    Inventory,
                    ProductFulfillmentProfile,
                    ShippingTemplate,
                )
                .join(Product, Product.id == ProductSku.product_id)
                .join(Store, Store.id == ProductSku.store_id)
                .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
                .outerjoin(
                    ProductFulfillmentProfile, ProductFulfillmentProfile.product_id == Product.id
                )
                .outerjoin(
                    ShippingTemplate,
                    ShippingTemplate.id == ProductFulfillmentProfile.shipping_template_id,
                )
                .where(ProductSku.sku_no == sku_no)
            )
        ).one_or_none()
        if row is None:
            return None
        return (None, *row)

    async def cart_contexts(self, user_id: int, item_nos: list[str]) -> list[ItemContext]:
        rows = (
            await self.session.execute(
                select(
                    CartItem,
                    ProductSku,
                    Product,
                    Store,
                    Inventory,
                    ProductFulfillmentProfile,
                    ShippingTemplate,
                )
                .join(Cart, Cart.id == CartItem.cart_id)
                .join(ProductSku, ProductSku.id == CartItem.sku_id)
                .join(Product, Product.id == ProductSku.product_id)
                .join(Store, Store.id == ProductSku.store_id)
                .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
                .outerjoin(
                    ProductFulfillmentProfile, ProductFulfillmentProfile.product_id == Product.id
                )
                .outerjoin(
                    ShippingTemplate,
                    ShippingTemplate.id == ProductFulfillmentProfile.shipping_template_id,
                )
                .where(Cart.user_id == user_id, CartItem.cart_item_no.in_(item_nos))
                .order_by(Store.id, CartItem.id)
            )
        ).all()
        return cast(list[ItemContext], rows)

    async def rules(self, template_ids: set[int]) -> dict[int, list[ShippingTemplateRule]]:
        if not template_ids:
            return {}
        rows = list(
            (
                await self.session.scalars(
                    select(ShippingTemplateRule)
                    .where(
                        ShippingTemplateRule.shipping_template_id.in_(template_ids),
                        ShippingTemplateRule.rule_status == "active",
                    )
                    .order_by(ShippingTemplateRule.id)
                )
            ).all()
        )
        result: dict[int, list[ShippingTemplateRule]] = {}
        for row in rows:
            result.setdefault(row.shipping_template_id, []).append(row)
        return result

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

    async def policy_versions(self, store_ids: set[int]) -> dict[int, dict[str, int]]:
        if not store_ids:
            return {}
        now = utc_now()
        rows = (
            await self.session.execute(
                select(
                    StoreServicePolicy.store_id,
                    StoreServicePolicy.policy_type,
                    StoreServicePolicy.policy_version,
                ).where(
                    StoreServicePolicy.store_id.in_(store_ids),
                    StoreServicePolicy.policy_status == "published",
                    or_(
                        StoreServicePolicy.effective_at.is_(None),
                        StoreServicePolicy.effective_at <= now,
                    ),
                    or_(
                        StoreServicePolicy.expires_at.is_(None),
                        StoreServicePolicy.expires_at > now,
                    ),
                )
            )
        ).all()
        result: dict[int, dict[str, int]] = {}
        for store_id, policy_type, version in rows:
            current = result.setdefault(store_id, {})
            current[policy_type] = max(current.get(policy_type, 0), version)
        return result
