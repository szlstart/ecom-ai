from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.cart.models import Cart, CartItem
from app.modules.catalog.models import Product, ProductFulfillmentProfile, ProductSku
from app.modules.checkout.models import CheckoutSession, CheckoutSnapshot
from app.modules.identity.models import UserAddress
from app.modules.inventory.models import Inventory
from app.modules.stores.models import ShippingTemplate, ShippingTemplateRule, Store

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
