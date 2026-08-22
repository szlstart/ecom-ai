from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PaginationMeta
from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.modules.catalog.schemas import ProductList
from app.modules.catalog.service import CatalogService, ProductSort
from app.modules.stores.models import Store, StoreFollow
from app.modules.stores.repository import StoreRepository
from app.modules.stores.schemas import (
    FollowedStoreList,
    StoreHomeContent,
    StorePolicyList,
    StorePolicyView,
    StoreProductGroupList,
    StoreProductGroupView,
    StorePublicView,
)


class StoreService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.repository = StoreRepository(session)
        self.catalog = CatalogService(session, settings)

    async def store(self, store_no: str, user_id: int | None) -> StorePublicView:
        store = await self._store_or_404(store_no)
        return await self._store_view(store, user_id)

    async def products(
        self,
        *,
        store_no: str,
        user_id: int | None,
        q: str | None,
        group_no: str | None,
        sort: ProductSort,
        cursor: str | None,
        limit: int,
    ) -> tuple[ProductList, PaginationMeta]:
        store = await self._store_or_404(store_no)
        if store.store_status != "active":
            return ProductList(items=[]), PaginationMeta(limit=limit)
        return await self.catalog.search(
            user_id=user_id,
            q=q,
            category_no=None,
            brand_no=None,
            store_no=store_no,
            group_no=group_no,
            price_min=None,
            price_max=None,
            sort=sort,
            cursor=cursor,
            limit=limit,
        )

    async def product_groups(self, store_no: str) -> StoreProductGroupList:
        store = await self._store_or_404(store_no)
        if store.store_status != "active":
            return StoreProductGroupList(items=[])
        rows = await self.repository.product_groups(store.id)
        views = {
            group.id: StoreProductGroupView(
                group_id=group.group_no,
                group_name=group.group_name,
                sort_order=group.sort_order,
                visible_product_count=count,
            )
            for group, count in rows
        }
        roots: list[StoreProductGroupView] = []
        for group, _ in rows:
            view = views[group.id]
            parent = views.get(group.parent_id) if group.parent_id else None
            if parent is None:
                roots.append(view)
            else:
                parent.children.append(view)
        return StoreProductGroupList(items=roots)

    async def policies(self, store_no: str) -> StorePolicyList:
        store = await self._store_or_404(store_no)
        rows = await self.repository.public_policies(store.id)
        return StorePolicyList(
            items=[
                StorePolicyView(
                    policy_id=row.policy_no,
                    policy_type=row.policy_type,
                    title=row.title,
                    content=row.content,
                    policy_version=row.policy_version,
                    effective_at=row.effective_at,
                    expires_at=row.expires_at,
                )
                for row in rows
                if row.effective_at is not None
            ]
        )

    async def home_content(self, store_no: str, user_id: int | None) -> StoreHomeContent:
        store = await self._store_or_404(store_no)
        if store.store_status != "active":
            return StoreHomeContent(announcements=[], recommended_products=[], hot_products=[])
        announcements = await self.repository.public_announcements(store.id)
        recommended = await self.catalog.product_cards(
            await self.repository.featured_products(store.id, "recommended"),
            user_id=user_id,
        )
        hot = await self.catalog.product_cards(
            await self.repository.featured_products(store.id, "hot"),
            user_id=user_id,
        )
        return StoreHomeContent(
            announcements=[
                {
                    "announcement_id": item.announcement_no,
                    "title": item.title,
                    "content": item.content,
                }
                for item in announcements
            ],
            recommended_products=recommended,
            hot_products=hot,
        )

    async def set_follow(self, user_id: int, store_no: str, enabled: bool) -> None:
        store = await self.repository.public_store(store_no, for_update=True)
        if store is None:
            raise _not_found()
        if enabled and store.store_status != "active":
            raise ApplicationError(
                status=409,
                code="STORE_NOT_FOLLOWABLE",
                title="Store cannot be followed",
                detail="当前店铺不可收藏。",
            )
        follow = await self.repository.follow(user_id, store.id, for_update=True)
        now = utc_now()
        if enabled and (follow is None or follow.deleted_at is not None):
            if follow is None:
                self.session.add(
                    StoreFollow(
                        user_id=user_id,
                        store_id=store.id,
                        followed_at=now,
                        deleted_at=None,
                    )
                )
            else:
                follow.deleted_at = None
                follow.followed_at = now
                follow.version += 1
            store.follower_count += 1
            store.version += 1
        elif not enabled and follow is not None and follow.deleted_at is None:
            follow.deleted_at = now
            follow.version += 1
            store.follower_count = max(store.follower_count - 1, 0)
            store.version += 1
        await self.session.commit()

    async def followed_stores(self, user_id: int, limit: int) -> FollowedStoreList:
        rows = await self.repository.followed_stores(user_id, limit)
        return FollowedStoreList(items=[await self._store_view(store, user_id) for store in rows])

    async def _store_view(self, store: Store, user_id: int | None) -> StorePublicView:
        logo = await self.catalog.repository.public_file_by_object_key(store.logo_object_key)
        followed = bool(
            user_id is not None
            and store.id in await self.repository.followed_store_ids(user_id, [store.id])
        )
        active = store.store_status == "active"
        return StorePublicView(
            store_id=store.store_no,
            store_name=store.store_name,
            logo_url=f"/api/v1/files/{logo.file_no}" if logo else None,
            description=store.description,
            store_status=store.store_status,
            visibility_mode="public" if active else "historical_limited",
            rating_score=format(store.rating_score, "f"),
            rating_count=store.rating_count,
            follower_count=store.follower_count,
            sales_count=store.sales_count,
            opened_at=store.opened_at,
            active_product_count=(await self.repository.active_product_count(store.id))
            if active
            else 0,
            is_followed=followed,
            customer_service_enabled=active,
        )

    async def _store_or_404(self, store_no: str) -> Store:
        store = await self.repository.public_store(store_no)
        if store is None:
            raise _not_found()
        return store


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="未找到该资源。",
    )
