from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Response

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission
from app.modules.reviews.dependencies import ReviewServiceDependency
from app.modules.reviews.schemas import (
    AdminReviewList,
    AdminReviewModerationRequest,
    AdminReviewReplyRequest,
    AdminReviewView,
)

router = APIRouter(prefix="/admin/reviews", tags=["review-administration"])


@router.get("", response_model=Envelope[AdminReviewList], operation_id="AdminReview_List")
async def list_reviews(
    response: Response,
    service: ReviewServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("reviews:read")],
    review_status: Annotated[
        Literal["pending", "published", "hidden", "rejected"] | None,
        Query(),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[AdminReviewList]:
    result = await service.admin_list(
        access,
        review_status=review_status,
        limit=limit,
    )
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/{review_id}",
    response_model=Envelope[AdminReviewView],
    operation_id="AdminReview_Get",
)
async def get_review(
    review_id: str,
    response: Response,
    service: ReviewServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("reviews:read")],
) -> Envelope[AdminReviewView]:
    result = await service.admin_detail(access, review_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/{review_id}/replies",
    response_model=Envelope[AdminReviewView],
    operation_id="AdminReview_Reply",
)
async def reply_review(
    review_id: str,
    payload: AdminReviewReplyRequest,
    response: Response,
    service: ReviewServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("reviews:reply")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminReviewView]:
    result = await service.admin_reply(
        access,
        review_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/{review_id}/moderations",
    response_model=Envelope[AdminReviewView],
    operation_id="AdminReview_Moderate",
)
async def moderate_review(
    review_id: str,
    payload: AdminReviewModerationRequest,
    response: Response,
    service: ReviewServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("reviews:moderate")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminReviewView]:
    result = await service.admin_moderate(
        access,
        review_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)
