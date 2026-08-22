from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response, status

from app.api.dependencies import OptionalUserContext, UserContext
from app.api.schemas import Envelope, ResponseMeta
from app.modules.catalog.schemas import ProductList
from app.modules.stores.dependencies import StoreServiceDependency
from app.modules.stores.schemas import (
    FollowedStoreList,
    StoreHomeContent,
    StorePolicyList,
    StoreProductGroupList,
    StorePublicView,
)

router = APIRouter(prefix="/stores", tags=["stores"])
follow_router = APIRouter(prefix="/users/me", tags=["current-user-favorites"])


@router.get("/{store_id}", response_model=Envelope[StorePublicView], operation_id="Store_Get")
async def get_store(
    store_id: str,
    response: Response,
    context: OptionalUserContext,
    service: StoreServiceDependency,
) -> Envelope[StorePublicView]:
    _cache(response, personalized=context is not None)
    return Envelope(data=await service.store(store_id, context.user.id if context else None))


@router.get(
    "/{store_id}/products",
    response_model=Envelope[ProductList],
    operation_id="StoreProduct_List",
)
async def list_store_products(
    store_id: str,
    response: Response,
    context: OptionalUserContext,
    service: StoreServiceDependency,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    group_id: Annotated[str | None, Query(min_length=5, max_length=40)] = None,
    sort: Literal["relevance", "sales", "newest", "price_asc", "price_desc"] = "relevance",
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> Envelope[ProductList]:
    data, pagination = await service.products(
        store_no=store_id,
        user_id=context.user.id if context else None,
        q=q,
        group_no=group_id,
        sort=sort,
        cursor=cursor,
        limit=limit,
    )
    _cache(response, personalized=context is not None)
    return Envelope(data=data, meta=ResponseMeta(pagination=pagination))


@router.get(
    "/{store_id}/product-groups",
    response_model=Envelope[StoreProductGroupList],
    operation_id="StoreProductGroup_List",
)
async def list_store_product_groups(
    store_id: str,
    response: Response,
    service: StoreServiceDependency,
) -> Envelope[StoreProductGroupList]:
    _cache(response, max_age=300)
    return Envelope(data=await service.product_groups(store_id))


@router.get(
    "/{store_id}/home-content",
    response_model=Envelope[StoreHomeContent],
    operation_id="StoreHomeContent_Get",
)
async def get_store_home_content(
    store_id: str,
    response: Response,
    context: OptionalUserContext,
    service: StoreServiceDependency,
) -> Envelope[StoreHomeContent]:
    _cache(response, personalized=context is not None)
    return Envelope(data=await service.home_content(store_id, context.user.id if context else None))


@router.get(
    "/{store_id}/service-policies",
    response_model=Envelope[StorePolicyList],
    operation_id="StorePolicy_ListPublic",
)
async def list_store_policies(
    store_id: str,
    response: Response,
    service: StoreServiceDependency,
) -> Envelope[StorePolicyList]:
    _cache(response, max_age=300)
    return Envelope(data=await service.policies(store_id))


@follow_router.get(
    "/followed-stores",
    response_model=Envelope[FollowedStoreList],
    operation_id="FollowedStore_ListMine",
)
async def list_followed_stores(
    response: Response,
    context: UserContext,
    service: StoreServiceDependency,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> Envelope[FollowedStoreList]:
    response.headers["Cache-Control"] = "private, no-store"
    return Envelope(data=await service.followed_stores(context.user.id, limit))


@follow_router.put(
    "/followed-stores/{store_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="FollowedStore_Put",
)
async def follow_store(
    store_id: str,
    context: UserContext,
    service: StoreServiceDependency,
) -> None:
    await service.set_follow(context.user.id, store_id, True)


@follow_router.delete(
    "/followed-stores/{store_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="FollowedStore_Delete",
)
async def unfollow_store(
    store_id: str,
    context: UserContext,
    service: StoreServiceDependency,
) -> None:
    await service.set_follow(context.user.id, store_id, False)


def _cache(response: Response, *, personalized: bool = False, max_age: int = 60) -> None:
    if personalized:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Authorization"
    else:
        response.headers["Cache-Control"] = f"public, max-age={max_age}, stale-while-revalidate=30"
