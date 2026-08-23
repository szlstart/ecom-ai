from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PaginationMeta
from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.pagination import CursorCodec, CursorPosition
from app.modules.reviews.models import Review
from app.modules.reviews.repository import ReviewRepository
from app.modules.reviews.schemas import (
    ProductReviewList,
    ProductReviewSummary,
    ProductReviewView,
    ReviewAppendView,
    ReviewImageView,
    ReviewReplyView,
)


class ReviewService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.repository = ReviewRepository(session)
        self.cursor = CursorCodec(settings.security_hmac_secret.get_secret_value())

    async def product_reviews(
        self,
        product_no: str,
        *,
        rating: int | None,
        has_image: bool | None,
        sku_no: str | None,
        sort: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[ProductReviewList, PaginationMeta]:
        product = await self.repository.public_product(product_no)
        if product is None:
            raise ApplicationError(
                status=404,
                code="RESOURCE_NOT_FOUND",
                title="Resource not found",
                detail="未找到该商品。",
            )
        filter_key = json.dumps(
            {
                "product_no": product_no,
                "rating": rating,
                "has_image": has_image,
                "sku_no": sku_no,
                "sort": sort,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        position = self.cursor.decode(cursor, filter_key=filter_key)
        try:
            rows, has_more = await self.repository.public_reviews(
                product_id=product.id,
                rating=rating,
                has_image=has_image,
                sku_no=sku_no,
                sort=sort,
                position=position,
                limit=limit,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ApplicationError(
                status=400,
                code="PAGINATION_CURSOR_INVALID",
                title="Invalid pagination cursor",
                detail="分页位置无效，请重新加载评价列表。",
            ) from exc

        ids = [review.id for review, _, _ in rows]
        images = await self.repository.images(ids)
        appends = await self.repository.appends(ids)
        replies = await self.repository.replies(ids)
        items: list[ProductReviewView] = []
        for review, user, sku in rows:
            append = appends.get(review.id)
            reply = replies.get(review.id)
            items.append(
                ProductReviewView(
                    review_id=review.review_no,
                    user_display_name="匿名用户"
                    if review.is_anonymous
                    else _mask_nickname(user.nickname),
                    sku_id=sku.sku_no,
                    sku_name=sku.sku_name,
                    rating=review.rating,
                    content=review.content,
                    published_at=_required_published_at(review),
                    helpful_count=review.helpful_count,
                    images=[
                        ReviewImageView(
                            file_id=file.file_no,
                            url=f"/api/v1/files/{file.file_no}",
                            thumbnail_url=f"/api/v1/files/{file.file_no}?variant=thumbnail",
                            width=image.width,
                            height=image.height,
                        )
                        for image, file in images.get(review.id, [])
                    ],
                    append=ReviewAppendView(
                        content=append.content,
                        published_at=append.published_at,
                    )
                    if append and append.published_at
                    else None,
                    merchant_reply=ReviewReplyView(
                        content=reply.content,
                        published_at=reply.published_at,
                    )
                    if reply and reply.published_at
                    else None,
                )
            )

        distribution, image_count = await self.repository.rating_distribution(product.id)
        rating_total = sum(stars * count for stars, count in distribution.items())
        review_count = sum(distribution.values())
        average = Decimal(rating_total) / Decimal(review_count) if review_count else Decimal("0")
        summary = ProductReviewSummary(
            review_count=review_count,
            average_rating=format(average.quantize(Decimal("0.01")), "f"),
            rating_distribution={str(stars): distribution.get(stars, 0) for stars in range(1, 6)},
            image_review_count=image_count,
        )
        return ProductReviewList(summary=summary, items=items), _pagination(
            rows=[item[0] for item in rows],
            position=position,
            has_more=has_more,
            filter_key=filter_key,
            limit=limit,
            codec=self.cursor,
        )


def _pagination(
    *,
    rows: list[Review],
    position: CursorPosition | None,
    has_more: bool,
    filter_key: str,
    limit: int,
    codec: CursorCodec,
) -> PaginationMeta:
    backward = position is not None and position.direction == "previous"
    has_previous = has_more if backward else position is not None
    has_next = position is not None if backward else has_more
    previous = (
        codec.encode(
            filter_key=filter_key,
            values=_cursor_values(rows[0]),
            direction="previous",
        )
        if rows and has_previous
        else None
    )
    following = (
        codec.encode(filter_key=filter_key, values=_cursor_values(rows[-1]), direction="next")
        if rows and has_next
        else None
    )
    return PaginationMeta(
        previous_cursor=previous,
        next_cursor=following,
        has_previous=has_previous,
        has_next=has_next,
        limit=limit,
    )


def _cursor_values(review: Review) -> tuple[str, str]:
    return (_required_published_at(review).isoformat(), str(review.id))


def _required_published_at(review: Review) -> datetime:
    if review.published_at is None:
        raise RuntimeError("published review must have published_at")
    return review.published_at


def _mask_nickname(nickname: str) -> str:
    normalized = nickname.strip()
    if not normalized:
        return "用户**"
    if len(normalized) == 1:
        return f"{normalized}*"
    return f"{normalized[0]}{'*' * min(2, len(normalized) - 1)}"
