from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PaginationMeta
from app.core.config import Settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.pagination import CursorCodec, CursorPosition
from app.core.security import utc_now
from app.modules.files.models import FileObject
from app.modules.identity.models import User
from app.modules.orders.models import Order, OrderItem
from app.modules.reviews.models import Review, ReviewImage
from app.modules.reviews.repository import ReviewRepository
from app.modules.reviews.schemas import (
    MyReviewImageView,
    MyReviewView,
    ProductReviewList,
    ProductReviewSummary,
    ProductReviewView,
    ReviewAction,
    ReviewAppendView,
    ReviewCreateRequest,
    ReviewEligibility,
    ReviewImageView,
    ReviewReplyView,
)
from app.modules.system.models import OutboxEvent


class ReviewService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repository = ReviewRepository(session)
        self.idempotency = IdempotencyService(session)
        self.cursor = CursorCodec(settings.security_hmac_secret.get_secret_value())

    async def eligibility(self, user: User, order_item_no: str) -> ReviewEligibility:
        row = await self.repository.user_order_item(user.id, order_item_no)
        if row is None:
            raise _review_not_found()
        item, order = row
        existing = await self.repository.review_for_order_item(item.id)
        return _eligibility(
            item,
            order,
            existing,
            utc_now(),
            submission_window_days=self.settings.review_submission_window_days,
            edit_window_hours=self.settings.review_edit_window_hours,
            append_window_days=self.settings.review_append_window_days,
        )

    async def create(
        self,
        user: User,
        payload: ReviewCreateRequest,
        idempotency_key: str,
    ) -> MyReviewView:
        claim = await self.idempotency.begin(
            scope_key=f"review:create:{user.user_no}:{payload.order_item_id}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="review",
        )
        if claim.replayed and claim.record.response_body is not None:
            return MyReviewView.model_validate(claim.record.response_body)
        row = await self.repository.user_order_item(user.id, payload.order_item_id, for_update=True)
        if row is None:
            raise _review_not_found()
        item, order = row
        existing = await self.repository.review_for_order_item(item.id)
        eligibility = _eligibility(
            item,
            order,
            existing,
            utc_now(),
            submission_window_days=self.settings.review_submission_window_days,
            edit_window_hours=self.settings.review_edit_window_hours,
            append_window_days=self.settings.review_append_window_days,
        )
        if not eligibility.eligible:
            raise ApplicationError(
                status=409,
                code=eligibility.reason_code or "REVIEW_NOT_ELIGIBLE",
                title="Review is not eligible",
                detail=eligibility.reason_message or "当前订单商品不可评价。",
            )
        files = await self.repository.review_files(payload.image_file_ids, for_update=True)
        files = _validate_review_files(user, payload.image_file_ids, files)
        now = utc_now()
        review = Review(
            review_no=new_prefixed_ulid("rev_"),
            order_id=order.id,
            order_item_id=item.id,
            user_id=user.id,
            store_id=order.store_id,
            product_id=item.product_id,
            sku_id=item.sku_id,
            rating=payload.rating,
            content=_normalize_review_content(payload.content),
            is_anonymous=payload.is_anonymous,
            review_status="pending",
            moderation_status="pending",
            published_at=None,
            helpful_count=0,
        )
        self.session.add(review)
        await self.session.flush()
        for sort_order, file in enumerate(files):
            if file.width is None or file.height is None:
                raise RuntimeError(f"review image {file.file_no} has no dimensions")
            self.session.add(
                ReviewImage(
                    review_id=review.id,
                    object_key=file.object_key,
                    sha256=file.sha256,
                    width=file.width,
                    height=file.height,
                    sort_order=sort_order,
                    scan_status="safe",
                    image_status="active",
                )
            )
            file.reference_count += 1
            file.version += 1
        item.review_status = "reviewed"
        item.version += 1
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="review.submitted.v1",
                aggregate_type="review",
                aggregate_no=review.review_no,
                aggregate_version=review.version,
                payload={
                    "review_id": review.review_no,
                    "order_id": order.order_no,
                    "order_item_id": item.order_item_no,
                    "product_id": item.product_no,
                    "image_count": len(files),
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=request_id,
            )
        )
        await self.session.flush()
        await self.session.refresh(review, attribute_names=["created_at"])
        result = _my_review_view(
            review,
            order,
            item,
            files,
            now,
            edit_window_hours=self.settings.review_edit_window_hours,
            append_window_days=self.settings.review_append_window_days,
        )
        self.idempotency.complete(
            claim,
            response_status=201,
            resource_no=review.review_no,
            response_body=result.model_dump(mode="json"),
        )
        await self.session.commit()
        return result

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


def _eligibility(
    item: OrderItem,
    order: Order,
    existing: Review | None,
    now: datetime,
    *,
    submission_window_days: int,
    edit_window_hours: int,
    append_window_days: int,
) -> ReviewEligibility:
    completed_at = order.completed_at
    deadline = (
        completed_at + timedelta(days=submission_window_days) if completed_at is not None else None
    )
    if existing is not None:
        actions: list[ReviewAction] = ["view"]
        if (
            existing.review_status in {"pending", "rejected"}
            and existing.created_at + timedelta(hours=edit_window_hours) >= now
        ):
            actions.append("edit")
        if (
            existing.review_status == "published"
            and existing.created_at + timedelta(days=append_window_days) >= now
        ):
            actions.append("append")
        return ReviewEligibility(
            order_item_id=item.order_item_no,
            order_id=order.order_no,
            product_id=item.product_no,
            sku_id=item.sku_no,
            product_name=item.product_name,
            sku_name=item.sku_name,
            order_completed_at=completed_at,
            review_deadline_at=deadline,
            eligible=False,
            reason_code="REVIEW_ALREADY_EXISTS",
            reason_message="该订单商品已经提交过评价。",
            existing_review_id=existing.review_no,
            available_actions=actions,
        )
    reason: tuple[str, str] | None = None
    if order.order_status != "completed" or order.fulfillment_status != "received":
        reason = ("ORDER_NOT_COMPLETED", "确认收货并完成订单后才能评价。")
    elif item.review_status != "pending":
        reason = ("ORDER_ITEM_REVIEW_CLOSED", "该订单商品的评价入口已关闭。")
    elif deadline is None or deadline < now:
        reason = ("REVIEW_WINDOW_EXPIRED", "该订单商品已超过首次评价期限。")
    return ReviewEligibility(
        order_item_id=item.order_item_no,
        order_id=order.order_no,
        product_id=item.product_no,
        sku_id=item.sku_no,
        product_name=item.product_name,
        sku_name=item.sku_name,
        order_completed_at=completed_at,
        review_deadline_at=deadline,
        eligible=reason is None,
        reason_code=reason[0] if reason else None,
        reason_message=reason[1] if reason else None,
        existing_review_id=None,
        available_actions=["create"] if reason is None else [],
    )


def _validate_review_files(
    user: User,
    requested_file_nos: list[str],
    files: list[FileObject],
) -> list[FileObject]:
    by_no = {item.file_no: item for item in files}
    if len(by_no) != len(requested_file_nos):
        raise _review_file_error("REVIEW_IMAGE_NOT_AVAILABLE", "部分评价图片不存在或不可用。")
    ordered = [by_no[item] for item in requested_file_nos]
    for file in ordered:
        valid = (
            file.purpose == "review_image"
            and file.owner_type == "user"
            and file.owner_no == user.user_no
            and file.parent_file_id is not None
            and file.file_status == "active"
            and file.scan_status == "safe"
            and file.detected_mime_type.startswith("image/")
            and file.width is not None
            and file.height is not None
            and file.visibility in {"private", "public_derivative"}
        )
        if not valid:
            raise _review_file_error(
                "REVIEW_IMAGE_NOT_AVAILABLE", "部分评价图片尚未通过安全处理或不属于当前用户。"
            )
        if file.reference_count != 0:
            raise _review_file_error(
                "REVIEW_IMAGE_ALREADY_BOUND", "评价图片已经绑定到其他业务记录。"
            )
    return ordered


def _normalize_review_content(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = "".join(
        character
        if character == "\n" or not unicodedata.category(character).startswith("C")
        else " "
        for character in normalized
    )
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.split("\n")]
    result = "\n".join(lines).strip()
    if len(result) > 500:
        raise ApplicationError(
            status=422,
            code="REVIEW_CONTENT_TOO_LONG",
            title="Review content too long",
            detail="评价正文不能超过 500 个字符。",
        )
    return result or None


def _my_review_view(
    review: Review,
    order: Order,
    item: OrderItem,
    files: list[FileObject],
    now: datetime,
    *,
    edit_window_hours: int,
    append_window_days: int,
) -> MyReviewView:
    edit_deadline = review.created_at + timedelta(hours=edit_window_hours)
    append_deadline = review.created_at + timedelta(days=append_window_days)
    actions: list[ReviewAction] = ["view"]
    if review.review_status in {"pending", "rejected"} and edit_deadline >= now:
        actions.append("edit")
    if review.review_status == "published" and append_deadline >= now:
        actions.append("append")
    return MyReviewView(
        review_id=review.review_no,
        order_id=order.order_no,
        order_item_id=item.order_item_no,
        product_id=item.product_no,
        sku_id=item.sku_no,
        product_name=item.product_name,
        sku_name=item.sku_name,
        rating=review.rating,
        content=review.content,
        is_anonymous=review.is_anonymous,
        review_status=review.review_status,
        moderation_status=review.moderation_status,
        images=[
            MyReviewImageView(
                file_id=file.file_no,
                width=file.width or 0,
                height=file.height or 0,
            )
            for file in files
        ],
        submitted_at=review.created_at,
        published_at=review.published_at,
        edit_deadline_at=edit_deadline,
        append_deadline_at=append_deadline,
        available_actions=actions,
        version=review.version,
    )


def _review_not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="评价资源不存在。",
    )


def _review_file_error(code: str, detail: str) -> ApplicationError:
    return ApplicationError(
        status=409,
        code=code,
        title="Review image unavailable",
        detail=detail,
    )
