from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response

from app.api.schemas import Envelope, ResponseMeta
from app.modules.reviews.dependencies import ReviewServiceDependency
from app.modules.reviews.schemas import ProductReviewList

router = APIRouter(prefix="/products", tags=["reviews"])


@router.get(
    "/{product_id}/reviews",
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
