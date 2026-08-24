from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import IdempotencyKey, OptionalUserContext, UserContext
from app.api.schemas import Envelope, ResponseMeta
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.reviews.dependencies import ReviewServiceDependency
from app.modules.reviews.schemas import (
    MyReviewList,
    MyReviewView,
    ProductReviewList,
    ProductReviewView,
    ReviewAppendCreateRequest,
    ReviewCreateRequest,
    ReviewEligibility,
    ReviewUpdateRequest,
)

router = APIRouter(tags=["reviews"])


@router.get(
    "/products/{product_id}/reviews",
    response_model=Envelope[ProductReviewList],
    operation_id="ProductReview_List",
)
async def list_product_reviews(
    product_id: str,
    response: Response,
    service: ReviewServiceDependency,
    rating: Annotated[int | None, Query(ge=1, le=5)] = None,
    has_image: bool | None = None,
    sku_id: Annotated[str | None, Query(min_length=5, max_length=40)] = None,
    sort: Literal["newest", "oldest"] = "newest",
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> Envelope[ProductReviewList]:
    data, pagination = await service.product_reviews(
        product_id,
        rating=rating,
        has_image=has_image,
        sku_no=sku_id,
        sort=sort,
        cursor=cursor,
        limit=limit,
    )
    response.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=30"
    return Envelope(data=data, meta=ResponseMeta(pagination=pagination))


@router.get(
    "/review-eligibilities/{order_item_id}",
    response_model=Envelope[ReviewEligibility],
    operation_id="ReviewEligibility_Get",
)
async def get_review_eligibility(
    order_item_id: str,
    response: Response,
    context: UserContext,
    service: ReviewServiceDependency,
) -> Envelope[ReviewEligibility]:
    result = await service.eligibility(context.user, order_item_id)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/users/me/reviews",
    response_model=Envelope[MyReviewList],
    operation_id="Review_ListMine",
)
async def list_my_reviews(
    response: Response,
    context: UserContext,
    service: ReviewServiceDependency,
    view: Literal["pending", "published"] = "pending",
    order_id: Annotated[str | None, Query(max_length=40)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> Envelope[MyReviewList]:
    data, pagination = await service.list_mine(
        context.user,
        view=view,
        order_no=order_id,
        cursor=cursor,
        limit=limit,
    )
    _no_store(response)
    return Envelope(data=data, meta=ResponseMeta(pagination=pagination))


@router.post(
    "/reviews",
    response_model=Envelope[MyReviewView],
    status_code=status.HTTP_201_CREATED,
    operation_id="Review_Create",
)
async def create_review(
    payload: ReviewCreateRequest,
    response: Response,
    context: UserContext,
    service: ReviewServiceDependency,
    idempotency_key: IdempotencyKey,
) -> Envelope[MyReviewView]:
    result = await service.create(context.user, payload, idempotency_key)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/reviews/{review_id}",
    response_model=Envelope[MyReviewView | ProductReviewView],
    operation_id="Review_GetMine",
)
async def get_review(
    review_id: str,
    response: Response,
    context: OptionalUserContext,
    service: ReviewServiceDependency,
) -> Envelope[MyReviewView | ProductReviewView]:
    result = await service.detail(context.user if context is not None else None, review_id)
    if isinstance(result, MyReviewView):
        response.headers["ETag"] = _etag(result.version)
        _no_store(response)
    else:
        response.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=30"
    return Envelope(data=result)


@router.patch(
    "/reviews/{review_id}",
    response_model=Envelope[MyReviewView],
    operation_id="Review_Update",
)
async def update_review(
    review_id: str,
    payload: ReviewUpdateRequest,
    response: Response,
    context: UserContext,
    service: ReviewServiceDependency,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[MyReviewView]:
    result = await service.update(
        context.user,
        review_id,
        payload,
        _expected_version(if_match),
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/reviews/{review_id}/append-records",
    response_model=Envelope[MyReviewView],
    status_code=status.HTTP_201_CREATED,
    operation_id="Review_Append",
)
async def append_review(
    review_id: str,
    payload: ReviewAppendCreateRequest,
    response: Response,
    context: UserContext,
    service: ReviewServiceDependency,
    idempotency_key: IdempotencyKey,
) -> Envelope[MyReviewView]:
    result = await service.append(context.user, review_id, payload, idempotency_key)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)
