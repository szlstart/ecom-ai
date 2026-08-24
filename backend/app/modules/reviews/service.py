from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PaginationMeta
from app.core.config import Settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.pagination import CursorCodec, CursorPosition
from app.core.security import utc_now
from app.modules.catalog.models import Product, ProductSku
from app.modules.files.models import FileObject
from app.modules.identity.models import User
from app.modules.orders.models import Order, OrderItem
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.reviews.models import (
    Review,
    ReviewAppendImage,
    ReviewAppendRecord,
    ReviewGovernanceRecord,
    ReviewImage,
    ReviewReply,
    ReviewRevisionRecord,
)
from app.modules.reviews.repository import ReviewRepository
from app.modules.reviews.schemas import (
    AdminReviewGovernanceView,
    AdminReviewList,
    AdminReviewModerationRequest,
    AdminReviewReplyRequest,
    AdminReviewView,
    MyReviewAppendView,
    MyReviewImageView,
    MyReviewList,
    MyReviewListItem,
    MyReviewView,
    ProductReviewList,
    ProductReviewSummary,
    ProductReviewView,
    ReviewAction,
    ReviewAppendCreateRequest,
    ReviewAppendView,
    ReviewCreateRequest,
    ReviewEligibility,
    ReviewImageView,
    ReviewReplyView,
    ReviewUpdateRequest,
)
from app.modules.stores.models import Store
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

    async def admin_list(
        self,
        access: AdminAccess,
        *,
        review_status: str | None,
        limit: int,
    ) -> AdminReviewList:
        rows = await self.repository.admin_reviews(
            scopes=access.scopes,
            review_status=review_status,
            limit=limit,
        )
        return AdminReviewList(items=[await self._admin_view(row) for row in rows])

    async def admin_detail(self, access: AdminAccess, review_no: str) -> AdminReviewView:
        row = await self.repository.admin_review_detail(review_no)
        if row is None:
            raise _review_not_found()
        access.require_scope("store", row[0].store_id)
        return await self._admin_view(row)

    async def admin_reply(
        self,
        access: AdminAccess,
        review_no: str,
        payload: AdminReviewReplyRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminReviewView:
        claim = await self.idempotency.begin(
            scope_key=f"admin:review-reply:{review_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="review_reply",
        )
        row = await self.repository.admin_review_detail(review_no, for_update=True)
        if row is None:
            raise _review_not_found()
        review = row[0]
        access.require_scope("store", review.store_id)
        if claim.replayed:
            return await self._admin_view(row)
        if review.version != expected_version:
            raise _review_version_precondition(review.version)
        if review.review_status not in {"published", "hidden"}:
            raise ApplicationError(
                status=409,
                code="REVIEW_REPLY_NOT_ALLOWED",
                title="Review reply not allowed",
                detail="评价尚未发布，当前不能回复。",
            )
        if await self.repository.reply_for_review(review.id) is not None:
            raise ApplicationError(
                status=409,
                code="REVIEW_REPLY_ALREADY_EXISTS",
                title="Review reply already exists",
                detail="该评价已经回复过。",
            )
        now = utc_now()
        self.session.add(
            ReviewReply(
                review_id=review.id,
                store_id=review.store_id,
                replier_user_id=access.context.user.id,
                content=payload.content.strip(),
                reply_status="published",
                published_at=now,
            )
        )
        review.version += 1
        record_admin_operation(
            self.session,
            access,
            action="review.reply",
            target_type="review",
            target_no=review.review_no,
            after={"reply_status": "published"},
            scope_type="store",
            scope_id=review.store_id,
        )
        self.session.add(
            _review_outbox(
                review,
                "review.replied.v1",
                {"review_id": review.review_no},
                now,
                request_id_context.get() or new_prefixed_ulid("req_"),
            )
        )
        await self.session.flush()
        result = await self._admin_view(row)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=review.review_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def admin_moderate(
        self,
        access: AdminAccess,
        review_no: str,
        payload: AdminReviewModerationRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminReviewView:
        claim = await self.idempotency.begin(
            scope_key=f"admin:review-moderate:{review_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="review_governance",
        )
        row = await self.repository.admin_review_detail(review_no, for_update=True)
        if row is None:
            raise _review_not_found()
        review = row[0]
        access.require_scope("store", review.store_id)
        if claim.replayed:
            return await self._admin_view(row)
        if review.version != expected_version:
            raise _review_version_precondition(review.version)
        expected_status = "published" if payload.action == "hide" else "hidden"
        target_status = "hidden" if payload.action == "hide" else "published"
        if review.review_status != expected_status:
            raise ApplicationError(
                status=409,
                code="REVIEW_MODERATION_NOT_ALLOWED",
                title="Review moderation not allowed",
                detail="评价当前状态不允许执行该治理动作。",
            )
        now = utc_now()
        previous = review.review_status
        review.review_status = target_status
        review.moderation_status = "blocked" if payload.action == "hide" else "passed"
        review.hidden_at = now if payload.action == "hide" else None
        review.version += 1
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        self.session.add(
            ReviewGovernanceRecord(
                governance_no=new_prefixed_ulid("rgo_"),
                review_id=review.id,
                action=payload.action,
                from_status=previous,
                to_status=target_status,
                rule_code=payload.rule_code,
                reason=payload.reason,
                actor_user_id=access.context.user.id,
                scope_type="store",
                scope_id=review.store_id,
                review_version=review.version,
                request_id=request_id,
                trace_id=request_id,
            )
        )
        record_admin_operation(
            self.session,
            access,
            action=f"review.{payload.action}",
            target_type="review",
            target_no=review.review_no,
            reason=payload.reason,
            before={"review_status": previous},
            after={"review_status": target_status, "rule_code": payload.rule_code},
            scope_type="store",
            scope_id=review.store_id,
        )
        self.session.add(
            _review_outbox(
                review,
                "review.hidden.v1" if payload.action == "hide" else "review.restored.v1",
                {"review_id": review.review_no, "rule_code": payload.rule_code},
                now,
                request_id,
            )
        )
        await self.session.flush()
        result = await self._admin_view(row)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=review.review_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

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

    async def list_mine(
        self,
        user: User,
        *,
        view: Literal["pending", "published"],
        order_no: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[MyReviewList, PaginationMeta]:
        filter_key = json.dumps(
            {"user": user.user_no, "view": view, "order_id": order_no},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        position = self.cursor.decode(cursor, filter_key=filter_key)
        now = utc_now()
        try:
            if view == "pending":
                rows, has_more = await self.repository.user_pending_items(
                    user_id=user.id,
                    completed_since=now
                    - timedelta(days=self.settings.review_submission_window_days),
                    order_no=order_no,
                    position=position,
                    limit=limit,
                )
                items = [
                    MyReviewListItem(
                        item_type="pending",
                        order_id=order.order_no,
                        order_item_id=item.order_item_no,
                        product_id=item.product_no,
                        sku_id=item.sku_no,
                        product_name=item.product_name,
                        sku_name=item.sku_name,
                        order_completed_at=order.completed_at,
                        eligibility=_eligibility(
                            item,
                            order,
                            None,
                            now,
                            submission_window_days=self.settings.review_submission_window_days,
                            edit_window_hours=self.settings.review_edit_window_hours,
                            append_window_days=self.settings.review_append_window_days,
                        ),
                    )
                    for item, order in rows
                ]
                values = [
                    (_required_datetime(order.completed_at).isoformat(), str(item.id))
                    for item, order in rows
                ]
            else:
                review_rows, has_more = await self.repository.user_reviews(
                    user_id=user.id,
                    order_no=order_no,
                    position=position,
                    limit=limit,
                )
                review_ids = [review.id for review, _, _ in review_rows]
                images = await self.repository.owner_images_many(review_ids)
                appends = await self.repository.owner_appends(review_ids)
                append_images = await self.repository.owner_append_images_many(
                    [append.id for append in appends.values()]
                )
                replies = await self.repository.replies(review_ids)
                items = [
                    MyReviewListItem(
                        item_type="review",
                        order_id=order.order_no,
                        order_item_id=item.order_item_no,
                        product_id=item.product_no,
                        sku_id=item.sku_no,
                        product_name=item.product_name,
                        sku_name=item.sku_name,
                        order_completed_at=order.completed_at,
                        eligibility=_eligibility(
                            item,
                            order,
                            review,
                            now,
                            submission_window_days=self.settings.review_submission_window_days,
                            edit_window_hours=self.settings.review_edit_window_hours,
                            append_window_days=self.settings.review_append_window_days,
                        ),
                        review=_my_review_view(
                            review,
                            order,
                            item,
                            [file for _, file in images.get(review.id, [])],
                            now,
                            edit_window_hours=self.settings.review_edit_window_hours,
                            append_window_days=self.settings.review_append_window_days,
                            append=appends.get(review.id),
                            append_files=[
                                file
                                for _, file in append_images.get(
                                    appends[review.id].id if review.id in appends else -1,
                                    [],
                                )
                            ],
                            reply=replies.get(review.id),
                        ),
                    )
                    for review, order, item in review_rows
                ]
                values = [
                    (review.created_at.isoformat(), str(review.id)) for review, _, _ in review_rows
                ]
        except (TypeError, ValueError, OverflowError) as exc:
            raise ApplicationError(
                status=400,
                code="PAGINATION_CURSOR_INVALID",
                title="Invalid pagination cursor",
                detail="分页位置无效，请重新加载评价列表。",
            ) from exc
        return MyReviewList(items=items), _owned_pagination(
            values=values,
            position=position,
            has_more=has_more,
            filter_key=filter_key,
            limit=limit,
            codec=self.cursor,
        )

    async def detail(
        self,
        viewer: User | None,
        review_no: str,
    ) -> MyReviewView | ProductReviewView:
        row = await self.repository.review_detail(review_no)
        if row is None:
            raise _review_not_found()
        review, order, item, author, sku = row
        now = utc_now()
        if viewer is not None and viewer.id == review.user_id:
            images = await self.repository.owner_images(review.id)
            append = await self.repository.append_for_review(review.id)
            append_images = (
                await self.repository.owner_append_images(append.id) if append is not None else []
            )
            reply = await self.repository.reply_for_review(review.id)
            return _my_review_view(
                review,
                order,
                item,
                [file for _, file in images],
                now,
                edit_window_hours=self.settings.review_edit_window_hours,
                append_window_days=self.settings.review_append_window_days,
                append=append,
                append_files=[file for _, file in append_images],
                reply=reply,
            )
        if review.review_status != "published" or review.published_at is None:
            raise _review_not_found()
        return await self._public_detail(review, author, sku)

    async def update(
        self,
        user: User,
        review_no: str,
        payload: ReviewUpdateRequest,
        expected_version: int,
    ) -> MyReviewView:
        row = await self.repository.review_detail(review_no, for_update=True)
        if row is None or row[0].user_id != user.id:
            raise _review_not_found()
        review, order, item, _, _ = row
        now = utc_now()
        if review.version != expected_version:
            raise _review_version_conflict(review.version)
        if (
            review.review_status not in {"pending", "rejected"}
            or review.created_at + timedelta(hours=self.settings.review_edit_window_hours) < now
        ):
            raise ApplicationError(
                status=409,
                code="REVIEW_EDIT_NOT_ALLOWED",
                title="Review cannot be edited",
                detail="当前评价状态或修改期限不允许编辑。",
            )
        current_images = await self.repository.owner_images(review.id)
        current_files = [file for _, file in current_images]
        requested_files = await self.repository.review_files(
            payload.image_file_ids,
            for_update=True,
        )
        files = _validate_review_files(
            user,
            payload.image_file_ids,
            requested_files,
            allowed_bound_keys={file.object_key for file in current_files},
        )
        before = _review_snapshot(review, current_files)
        current_by_key = {file.object_key: file for file in current_files}
        requested_by_key = {file.object_key: file for file in files}
        for image, _ in current_images:
            await self.session.delete(image)
        for object_key, file in current_by_key.items():
            if object_key not in requested_by_key:
                file.reference_count = max(0, file.reference_count - 1)
                file.version += 1
        await self.session.flush()
        for sort_order, file in enumerate(files):
            if file.object_key not in current_by_key:
                file.reference_count += 1
                file.version += 1
            self.session.add(_review_image(review.id, file, sort_order))
        review.rating = payload.rating
        review.content = _normalize_review_content(payload.content)
        review.is_anonymous = payload.is_anonymous
        review.review_status = "pending"
        review.moderation_status = "pending"
        review.hidden_at = None
        review.version += 1
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        after = _review_snapshot(review, files)
        self.session.add(
            ReviewRevisionRecord(
                revision_no=new_prefixed_ulid("rrv_"),
                review_id=review.id,
                actor_user_id=user.id,
                action="update",
                before_snapshot=before,
                after_snapshot=after,
                request_id=request_id,
                trace_id=request_id,
            )
        )
        self.session.add(
            _review_outbox(
                review,
                "review.updated.v1",
                {"review_id": review.review_no, "changed_by": user.user_no},
                now,
                request_id,
            )
        )
        await self.session.flush()
        reply = await self.repository.reply_for_review(review.id)
        result = _my_review_view(
            review,
            order,
            item,
            files,
            now,
            edit_window_hours=self.settings.review_edit_window_hours,
            append_window_days=self.settings.review_append_window_days,
            reply=reply,
        )
        await self.session.commit()
        return result

    async def append(
        self,
        user: User,
        review_no: str,
        payload: ReviewAppendCreateRequest,
        idempotency_key: str,
    ) -> MyReviewView:
        claim = await self.idempotency.begin(
            scope_key=f"review:append:{user.user_no}:{review_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="review_append",
        )
        if claim.replayed and claim.record.response_body is not None:
            return MyReviewView.model_validate(claim.record.response_body)
        row = await self.repository.review_detail(review_no, for_update=True)
        if row is None or row[0].user_id != user.id:
            raise _review_not_found()
        review, order, item, _, _ = row
        now = utc_now()
        if review.review_status != "published":
            raise _append_not_allowed("主评价尚未发布，暂时不能追评。")
        if review.created_at + timedelta(days=self.settings.review_append_window_days) < now:
            raise _append_not_allowed("当前评价已超过追评期限。")
        if await self.repository.append_for_review(review.id, for_update=True) is not None:
            raise ApplicationError(
                status=409,
                code="REVIEW_APPEND_ALREADY_EXISTS",
                title="Review append already exists",
                detail="该评价已经追评过一次。",
            )
        files = _validate_review_files(
            user,
            payload.image_file_ids,
            await self.repository.review_files(payload.image_file_ids, for_update=True),
        )
        content = _normalize_review_content(payload.content)
        if content is None:
            raise ApplicationError(
                status=422,
                code="REVIEW_APPEND_CONTENT_REQUIRED",
                title="Review append content required",
                detail="追评内容不能为空。",
            )
        append = ReviewAppendRecord(
            append_no=new_prefixed_ulid("rpa_"),
            review_id=review.id,
            user_id=user.id,
            content=content,
            append_status="pending",
            moderation_status="pending",
            published_at=None,
        )
        self.session.add(append)
        await self.session.flush()
        for sort_order, file in enumerate(files):
            if file.width is None or file.height is None:
                raise RuntimeError(f"review image {file.file_no} has no dimensions")
            self.session.add(
                ReviewAppendImage(
                    append_record_id=append.id,
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
        review.version += 1
        request_id = request_id_context.get() or new_prefixed_ulid("req_")
        self.session.add(
            _review_outbox(
                review,
                "review.append_submitted.v1",
                {
                    "review_id": review.review_no,
                    "append_id": append.append_no,
                    "image_count": len(files),
                },
                now,
                request_id,
            )
        )
        await self.session.flush()
        await self.session.refresh(append, attribute_names=["created_at"])
        primary_images = await self.repository.owner_images(review.id)
        reply = await self.repository.reply_for_review(review.id)
        result = _my_review_view(
            review,
            order,
            item,
            [file for _, file in primary_images],
            now,
            edit_window_hours=self.settings.review_edit_window_hours,
            append_window_days=self.settings.review_append_window_days,
            append=append,
            append_files=files,
            reply=reply,
        )
        self.idempotency.complete(
            claim,
            response_status=201,
            resource_no=append.append_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def _admin_view(
        self,
        row: tuple[Review, Order, OrderItem, User, Product, ProductSku, Store],
    ) -> AdminReviewView:
        review, order, item, user, product, sku, store = row
        reply = await self.repository.reply_for_review(review.id)
        history = await self.repository.governance_history(review.id)
        return AdminReviewView(
            review_id=review.review_no,
            order_id=order.order_no,
            order_item_id=item.order_item_no,
            user_id=user.user_no,
            user_name=user.nickname,
            store_id=store.store_no,
            store_name=store.store_name,
            product_id=product.product_no,
            product_name=product.product_name,
            sku_id=sku.sku_no,
            sku_name=sku.sku_name,
            rating=review.rating,
            content=review.content,
            is_anonymous=review.is_anonymous,
            review_status=cast(Any, review.review_status),
            moderation_status=cast(Any, review.moderation_status),
            merchant_reply=(
                ReviewReplyView(
                    content=reply.content,
                    published_at=_required_datetime(reply.published_at),
                )
                if reply is not None and reply.published_at is not None
                else None
            ),
            governance_history=[
                AdminReviewGovernanceView(
                    governance_id=record.governance_no,
                    action=cast(Any, record.action),
                    from_status=record.from_status,
                    to_status=record.to_status,
                    rule_code=record.rule_code,
                    reason=record.reason,
                    occurred_at=record.created_at,
                )
                for record in history
            ],
            submitted_at=review.created_at,
            published_at=review.published_at,
            version=review.version,
        )

    async def _public_detail(
        self,
        review: Review,
        author: User,
        sku: ProductSku,
    ) -> ProductReviewView:
        images = await self.repository.images([review.id])
        appends = await self.repository.appends([review.id])
        replies = await self.repository.replies([review.id])
        append = appends.get(review.id)
        append_images = (
            (await self.repository.append_images([append.id])).get(append.id, [])
            if append is not None
            else []
        )
        return ProductReviewView(
            review_id=review.review_no,
            user_display_name=(
                "匿名用户" if review.is_anonymous else _mask_nickname(author.nickname)
            ),
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
                published_at=_required_datetime(append.published_at),
                images=[
                    ReviewImageView(
                        file_id=file.file_no,
                        url=f"/api/v1/files/{file.file_no}",
                        thumbnail_url=f"/api/v1/files/{file.file_no}?variant=thumbnail",
                        width=image.width,
                        height=image.height,
                    )
                    for image, file in append_images
                ],
            )
            if append is not None
            else None,
            merchant_reply=ReviewReplyView(
                content=replies[review.id].content,
                published_at=_required_datetime(replies[review.id].published_at),
            )
            if review.id in replies
            else None,
        )

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
        append_images = await self.repository.append_images(
            [append.id for append in appends.values()]
        )
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
                        images=[
                            ReviewImageView(
                                file_id=file.file_no,
                                url=f"/api/v1/files/{file.file_no}",
                                thumbnail_url=(f"/api/v1/files/{file.file_no}?variant=thumbnail"),
                                width=image.width,
                                height=image.height,
                            )
                            for image, file in append_images.get(append.id, [])
                        ],
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
    *,
    allowed_bound_keys: set[str] | None = None,
) -> list[FileObject]:
    allowed_bound_keys = allowed_bound_keys or set()
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
        if file.reference_count != 0 and file.object_key not in allowed_bound_keys:
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
    append: ReviewAppendRecord | None = None,
    append_files: list[FileObject] | None = None,
    reply: ReviewReply | None = None,
) -> MyReviewView:
    edit_deadline = review.created_at + timedelta(hours=edit_window_hours)
    append_deadline = review.created_at + timedelta(days=append_window_days)
    actions: list[ReviewAction] = ["view"]
    if review.review_status in {"pending", "rejected"} and edit_deadline >= now:
        actions.append("edit")
    if review.review_status == "published" and append_deadline >= now and append is None:
        actions.append("append")
    append_files = append_files or []
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
        append=MyReviewAppendView(
            append_id=append.append_no,
            content=append.content,
            append_status=append.append_status,
            moderation_status=append.moderation_status,
            images=[
                MyReviewImageView(
                    file_id=file.file_no,
                    width=file.width or 0,
                    height=file.height or 0,
                )
                for file in append_files
            ],
            submitted_at=append.created_at,
            published_at=append.published_at,
        )
        if append is not None
        else None,
        merchant_reply=ReviewReplyView(
            content=reply.content,
            published_at=_required_datetime(reply.published_at),
        )
        if reply is not None and reply.published_at is not None
        else None,
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


def _owned_pagination(
    *,
    values: list[tuple[str, str]],
    position: CursorPosition | None,
    has_more: bool,
    filter_key: str,
    limit: int,
    codec: CursorCodec,
) -> PaginationMeta:
    backward = position is not None and position.direction == "previous"
    has_previous = has_more if backward else position is not None
    has_next = position is not None if backward else has_more
    return PaginationMeta(
        previous_cursor=codec.encode(
            filter_key=filter_key,
            values=values[0],
            direction="previous",
        )
        if values and has_previous
        else None,
        next_cursor=codec.encode(
            filter_key=filter_key,
            values=values[-1],
            direction="next",
        )
        if values and has_next
        else None,
        has_previous=has_previous,
        has_next=has_next,
        limit=limit,
    )


def _required_datetime(value: datetime | None) -> datetime:
    if value is None:
        raise RuntimeError("required review timestamp is missing")
    return value


def _review_image(review_id: int, file: FileObject, sort_order: int) -> ReviewImage:
    if file.width is None or file.height is None:
        raise RuntimeError(f"review image {file.file_no} has no dimensions")
    return ReviewImage(
        review_id=review_id,
        object_key=file.object_key,
        sha256=file.sha256,
        width=file.width,
        height=file.height,
        sort_order=sort_order,
        scan_status="safe",
        image_status="active",
    )


def _review_snapshot(review: Review, files: list[FileObject]) -> dict[str, object]:
    return {
        "rating": review.rating,
        "content": review.content,
        "is_anonymous": review.is_anonymous,
        "review_status": review.review_status,
        "moderation_status": review.moderation_status,
        "image_file_ids": [file.file_no for file in files],
        "version": review.version,
    }


def _review_outbox(
    review: Review,
    event_type: str,
    payload: dict[str, object],
    now: datetime,
    request_id: str,
) -> OutboxEvent:
    return OutboxEvent(
        event_no=new_prefixed_ulid("evt_"),
        event_type=event_type,
        aggregate_type="review",
        aggregate_no=review.review_no,
        aggregate_version=review.version,
        payload=payload,
        event_status="pending",
        available_at=now,
        attempt_count=0,
        trace_id=request_id,
    )


def _review_version_conflict(current_version: int) -> ApplicationError:
    return ApplicationError(
        status=409,
        code="VERSION_CONFLICT",
        title="Version conflict",
        detail="评价已发生变化，请刷新后重试。",
        headers={"ETag": f'"v{current_version}"'},
    )


def _review_version_precondition(current_version: int) -> ApplicationError:
    return ApplicationError(
        status=412,
        code="RESOURCE_VERSION_CONFLICT",
        title="Version conflict",
        detail="评价已发生变化，请刷新后重试。",
        headers={"ETag": f'"v{current_version}"'},
    )


def _append_not_allowed(detail: str) -> ApplicationError:
    return ApplicationError(
        status=409,
        code="REVIEW_APPEND_NOT_ALLOWED",
        title="Review append not allowed",
        detail=detail,
    )
