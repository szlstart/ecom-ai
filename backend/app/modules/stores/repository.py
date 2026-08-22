from __future__ import annotations

from typing import cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utc_now
from app.modules.catalog.models import Product
from app.modules.stores.models import (
    Store,
    StoreAnnouncement,
    StoreFeaturedProduct,
    StoreFollow,
    StoreProductGroup,
    StoreProductGroupItem,
    StoreServicePolicy,
)


class StoreRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def public_store(self, store_no: str, *, for_update: bool = False) -> Store | None:
        statement = select(Store).where(
            Store.store_no == store_no,
            Store.store_status.in_(("active", "suspended", "closed")),
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(Store | None, await self.session.scalar(statement))

    async def active_product_count(self, store_id: int) -> int:
        return int(
            await self.session.scalar(
                select(func.count(Product.id)).where(
                    Product.store_id == store_id,
                    Product.product_status == "on_sale",
                )
            )
            or 0
        )

    async def follow(
        self, user_id: int, store_id: int, *, for_update: bool = False
    ) -> StoreFollow | None:
        statement = (
            select(StoreFollow)
            .where(StoreFollow.user_id == user_id, StoreFollow.store_id == store_id)
            .order_by(StoreFollow.id.desc())
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(StoreFollow | None, await self.session.scalar(statement))

    async def followed_store_ids(self, user_id: int, store_ids: list[int]) -> set[int]:
        if not store_ids:
            return set()
        return set(
            (
                await self.session.scalars(
                    select(StoreFollow.store_id).where(
                        StoreFollow.user_id == user_id,
                        StoreFollow.store_id.in_(store_ids),
                        StoreFollow.deleted_at.is_(None),
                    )
                )
            ).all()
        )

    async def followed_stores(self, user_id: int, limit: int) -> list[Store]:
        return list(
            (
                await self.session.scalars(
                    select(Store)
                    .join(StoreFollow, StoreFollow.store_id == Store.id)
                    .where(
                        StoreFollow.user_id == user_id,
                        StoreFollow.deleted_at.is_(None),
                        Store.store_status.in_(("active", "suspended", "closed")),
                    )
                    .order_by(StoreFollow.followed_at.desc(), StoreFollow.id.desc())
                    .limit(limit)
                )
            ).all()
        )

    async def product_groups(self, store_id: int) -> list[tuple[StoreProductGroup, int]]:
        count_expression = func.count(Product.id)
        rows = (
            await self.session.execute(
                select(StoreProductGroup, count_expression)
                .outerjoin(
                    StoreProductGroupItem,
                    StoreProductGroupItem.store_product_group_id == StoreProductGroup.id,
                )
                .outerjoin(
                    Product,
                    and_(
                        Product.id == StoreProductGroupItem.product_id,
                        Product.store_id == store_id,
                        Product.product_status == "on_sale",
                    ),
                )
                .where(
                    StoreProductGroup.store_id == store_id,
                    StoreProductGroup.group_status == "active",
                )
                .group_by(StoreProductGroup.id)
                .order_by(
                    StoreProductGroup.parent_id,
                    StoreProductGroup.sort_order,
                    StoreProductGroup.id,
                )
            )
        ).all()
        return [(row[0], int(row[1])) for row in rows]

    async def public_policies(self, store_id: int) -> list[StoreServicePolicy]:
        now = utc_now()
        return list(
            (
                await self.session.scalars(
                    select(StoreServicePolicy)
                    .where(
                        StoreServicePolicy.store_id == store_id,
                        StoreServicePolicy.policy_status == "published",
                        StoreServicePolicy.effective_at.is_not(None),
                        StoreServicePolicy.effective_at <= now,
                        or_(
                            StoreServicePolicy.expires_at.is_(None),
                            StoreServicePolicy.expires_at > now,
                        ),
                    )
                    .order_by(
                        StoreServicePolicy.policy_type, StoreServicePolicy.policy_version.desc()
                    )
                )
            ).all()
        )

    async def public_announcements(self, store_id: int, limit: int = 5) -> list[StoreAnnouncement]:
        now = utc_now()
        return list(
            (
                await self.session.scalars(
                    select(StoreAnnouncement)
                    .where(
                        StoreAnnouncement.store_id == store_id,
                        StoreAnnouncement.announcement_status == "published",
                        or_(
                            StoreAnnouncement.starts_at.is_(None),
                            StoreAnnouncement.starts_at <= now,
                        ),
                        or_(StoreAnnouncement.ends_at.is_(None), StoreAnnouncement.ends_at > now),
                    )
                    .order_by(StoreAnnouncement.sort_order, StoreAnnouncement.id.desc())
                    .limit(limit)
                )
            ).all()
        )

    async def featured_products(
        self, store_id: int, slot_type: str, limit: int = 12
    ) -> list[tuple[Product, Store]]:
        now = utc_now()
        rows = (
            await self.session.execute(
                select(Product, Store)
                .join(Store, Store.id == Product.store_id)
                .join(StoreFeaturedProduct, StoreFeaturedProduct.product_id == Product.id)
                .where(
                    StoreFeaturedProduct.store_id == store_id,
                    StoreFeaturedProduct.slot_type == slot_type,
                    or_(
                        StoreFeaturedProduct.starts_at.is_(None),
                        StoreFeaturedProduct.starts_at <= now,
                    ),
                    or_(
                        StoreFeaturedProduct.ends_at.is_(None),
                        StoreFeaturedProduct.ends_at > now,
                    ),
                    Product.product_status == "on_sale",
                    Store.store_status == "active",
                )
                .order_by(StoreFeaturedProduct.sort_order, StoreFeaturedProduct.id)
                .limit(limit)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]
