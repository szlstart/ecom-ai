from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response, status

from app.api.dependencies import OptionalUserContext, UserContext
from app.api.schemas import Envelope, ResponseMeta
from app.modules.catalog.dependencies import CatalogServiceDependency
from app.modules.catalog.schemas import (
    BrandView,
    CategoryView,
    HomepageView,
    ProductDetail,
    ProductFaqList,
    ProductList,
    ProductSkuList,
    SearchSuggestionList,
)

router = APIRouter(tags=["catalog"])
favorite_router = APIRouter(prefix="/users/me", tags=["current-user-favorites"])


@router.get("/homepage", response_model=Envelope[HomepageView], operation_id="Homepage_Get")
async def homepage(
    response: Response,
    context: OptionalUserContext,
    service: CatalogServiceDependency,
) -> Envelope[HomepageView]:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    return Envelope(data=await service.homepage(context.user.id if context else None))


@router.get("/products", response_model=Envelope[ProductList], operation_id="Product_Search")
async def search_products(
    response: Response,
    context: OptionalUserContext,
    service: CatalogServiceDependency,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    category_id: Annotated[str | None, Query(min_length=5, max_length=40)] = None,
    brand_id: Annotated[str | None, Query(min_length=5, max_length=40)] = None,
    store_id: Annotated[str | None, Query(min_length=5, max_length=40)] = None,
    price_min: Annotated[int | None, Query(ge=0)] = None,
    price_max: Annotated[int | None, Query(ge=0)] = None,
    sort: Literal[
        "relevance", "sales", "newest", "price_asc", "price_desc", "random"
    ] = "relevance",
    recommendation_seed: Annotated[int, Query(ge=0, le=2_147_483_647)] = 0,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> Envelope[ProductList]:
    data, pagination = await service.search(
        user_id=context.user.id if context else None,
        q=q,
        category_no=category_id,
        brand_no=brand_id,
        store_no=store_id,
        group_no=None,
        price_min=price_min,
        price_max=price_max,
        sort=sort,
        cursor=cursor,
        limit=limit,
        random_seed=recommendation_seed,
    )
    _public_cache(response, personalized=context is not None)
    return Envelope(data=data, meta=ResponseMeta(pagination=pagination))


@router.get(
    "/products/{product_id}",
    response_model=Envelope[ProductDetail],
    operation_id="Product_Get",
)
async def get_product(
    product_id: str,
    response: Response,
    context: OptionalUserContext,
    service: CatalogServiceDependency,
) -> Envelope[ProductDetail]:
    _public_cache(response, personalized=context is not None)
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; img-src 'self'; style-src 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return Envelope(
        data=await service.product_detail(product_id, context.user.id if context else None)
    )


@router.get(
    "/products/{product_id}/skus",
    response_model=Envelope[ProductSkuList],
    operation_id="ProductSku_List",
)
async def list_product_skus(
    product_id: str,
    response: Response,
    service: CatalogServiceDependency,
) -> Envelope[ProductSkuList]:
    _public_cache(response)
    return Envelope(data=await service.product_skus(product_id))


@router.get(
    "/products/{product_id}/faqs",
    response_model=Envelope[ProductFaqList],
    operation_id="ProductFaq_List",
)
async def list_product_faqs(
    product_id: str,
    response: Response,
    service: CatalogServiceDependency,
) -> Envelope[ProductFaqList]:
    _public_cache(response)
    return Envelope(data=await service.product_faqs(product_id))


@router.get(
    "/categories", response_model=Envelope[list[CategoryView]], operation_id="Category_List"
)
async def list_categories(
    response: Response, service: CatalogServiceDependency
) -> Envelope[list[CategoryView]]:
    _public_cache(response, max_age=300)
    return Envelope(data=await service.categories())


@router.get("/brands", response_model=Envelope[list[BrandView]], operation_id="Brand_List")
async def list_brands(
    response: Response,
    service: CatalogServiceDependency,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[list[BrandView]]:
    _public_cache(response, max_age=300)
    return Envelope(data=await service.brands(q, limit))


@router.get(
    "/search/suggestions",
    response_model=Envelope[SearchSuggestionList],
    operation_id="SearchSuggestion_List",
)
async def search_suggestions(
    response: Response,
    service: CatalogServiceDependency,
    q: Annotated[str, Query(min_length=1, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> Envelope[SearchSuggestionList]:
    _public_cache(response, max_age=30)
    return Envelope(data=await service.suggestions(q, limit))


@favorite_router.get(
    "/favorite-products",
    response_model=Envelope[ProductList],
    operation_id="FavoriteProduct_ListMine",
)
async def list_favorite_products(
    response: Response,
    context: UserContext,
    service: CatalogServiceDependency,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> Envelope[ProductList]:
    response.headers["Cache-Control"] = "private, no-store"
    return Envelope(data=await service.favorite_products(context.user.id, limit))


@favorite_router.put(
    "/favorite-products/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="FavoriteProduct_Put",
)
async def put_favorite_product(
    product_id: str,
    context: UserContext,
    service: CatalogServiceDependency,
) -> None:
    await service.set_favorite(context.user.id, product_id, True)


@favorite_router.delete(
    "/favorite-products/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="FavoriteProduct_Delete",
)
async def delete_favorite_product(
    product_id: str,
    context: UserContext,
    service: CatalogServiceDependency,
) -> None:
    await service.set_favorite(context.user.id, product_id, False)


def _public_cache(response: Response, *, personalized: bool = False, max_age: int = 60) -> None:
    if personalized:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Authorization"
    else:
        response.headers["Cache-Control"] = f"public, max-age={max_age}, stale-while-revalidate=30"
