from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.modules.catalog.models import Product
from app.modules.reviews.models import Review, ReviewAppendRecord
from app.modules.stores.models import Store
from app.modules.system.models import OutboxEvent

REVIEW_MODERATION_EVENT_TYPES = frozenset(
    {"review.submitted.v1", "review.updated.v1", "review.append_submitted.v1"}
)
_BLOCK_PATTERNS = (
    re.compile(r"(?:出售|售卖|购买|贩卖).{0,8}毒品", re.I),
    re.compile(r"(?:杀人教程|如何杀人|教.{0,4}杀人)", re.I),
)
_MANUAL_PATTERN = re.compile(
    r"(?:毒品|杀人|(?:加|联系).{0,5}(?:微信|vx|v信|qq)|https?://|www\.|\b1[3-9]\d{9}\b)",
    re.I,
)


@dataclass(frozen=True)
class ModerationDecision:
    status: str
    rule_code: str


def classify_review_content(content: str | None) -> ModerationDecision:
    normalized = " ".join((content or "").split())
    if any(pattern.search(normalized) for pattern in _BLOCK_PATTERNS):
        return ModerationDecision("blocked", "HIGH_CONFIDENCE_ILLEGAL_CONTENT")
    if _MANUAL_PATTERN.search(normalized):
        return ModerationDecision("manual", "AMBIGUOUS_OR_CONTACT_CONTENT")
    return ModerationDecision("passed", "DETERMINISTIC_SAFE_CONTENT")


class ReviewModerationProcessor:
    """Idempotently consumes review submission Outbox commands in MySQL."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def process_batch(self, limit: int = 50) -> int:
        processed = 0
        for _ in range(limit):
            event = await self.session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type.in_(REVIEW_MODERATION_EVENT_TYPES),
                    OutboxEvent.event_status == "pending",
                    OutboxEvent.available_at <= utc_now(),
                )
                .order_by(OutboxEvent.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if event is None:
                break
            event.attempt_count += 1
            try:
                await self._process(event)
                await self.session.commit()
                processed += 1
            except Exception:
                event_no = event.event_no
                await self.session.rollback()
                await self._record_failure(event_no)
        return processed

    async def _process(self, event: OutboxEvent) -> None:
        review = await self.session.scalar(
            select(Review).where(Review.review_no == event.aggregate_no).with_for_update()
        )
        if review is None:
            raise LookupError("review referenced by moderation event does not exist")
        # A follow-up review shares the parent review aggregate version. Later,
        # unrelated governance on the parent must not strand an otherwise pending
        # follow-up forever, so append idempotency is decided from the append row.
        if (
            event.event_type != "review.append_submitted.v1"
            and event.aggregate_version != review.version
        ):
            self._finish_event(event, "STALE_REVIEW_VERSION")
            return
        if event.event_type != "review.append_submitted.v1":
            await self._moderate_review(review, event)
            return
        append_no = event.payload.get("append_id")
        if not isinstance(append_no, str):
            raise ValueError("append moderation event has no append_id")
        append = await self.session.scalar(
            select(ReviewAppendRecord)
            .where(
                ReviewAppendRecord.review_id == review.id,
                ReviewAppendRecord.append_no == append_no,
            )
            .with_for_update()
        )
        if append is None:
            raise LookupError("review append referenced by event does not exist")
        await self._moderate_append(review, append, event)

    async def _moderate_review(self, review: Review, event: OutboxEvent) -> None:
        if review.moderation_status != "pending" or review.review_status != "pending":
            self._finish_event(event, "ALREADY_MODERATED")
            return
        decision = classify_review_content(review.content)
        now = utc_now()
        review.moderation_status = decision.status
        if decision.status == "passed":
            review.review_status = "published"
            review.published_at = now
        elif decision.status == "blocked":
            review.review_status = "rejected"
            review.published_at = None
        review.version += 1
        await self.session.flush()
        if decision.status == "passed":
            await refresh_review_ratings(self.session, review.product_id, review.store_id)
        self._emit_result(review, event, decision, now, resource_type="review")
        self._finish_event(event, decision.rule_code)

    async def _moderate_append(
        self,
        review: Review,
        append: ReviewAppendRecord,
        event: OutboxEvent,
    ) -> None:
        if append.moderation_status != "pending" or append.append_status != "pending":
            self._finish_event(event, "ALREADY_MODERATED")
            return
        decision = classify_review_content(append.content)
        now = utc_now()
        append.moderation_status = decision.status
        if decision.status == "passed":
            append.append_status = "published"
            append.published_at = now
        elif decision.status == "blocked":
            append.append_status = "rejected"
            append.published_at = None
        append.version += 1
        review.version += 1
        self._emit_result(review, event, decision, now, resource_type="review_append")
        self._finish_event(event, decision.rule_code)

    def _emit_result(
        self,
        review: Review,
        source: OutboxEvent,
        decision: ModerationDecision,
        now: datetime,
        *,
        resource_type: str,
    ) -> None:
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="review.moderated.v1",
                aggregate_type="review",
                aggregate_no=review.review_no,
                aggregate_version=review.version,
                payload={
                    "review_id": review.review_no,
                    "resource_type": resource_type,
                    "moderation_status": decision.status,
                    "rule_code": decision.rule_code,
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=source.trace_id,
            )
        )

    def _finish_event(self, event: OutboxEvent, result_code: str) -> None:
        event.event_status = "published"
        event.published_at = utc_now()
        event.last_error_code = None
        event.payload = {**event.payload, "processor_result_code": result_code}

    async def _record_failure(self, event_no: str) -> None:
        event = await self.session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_no == event_no).with_for_update()
        )
        if event is None or event.event_status != "pending":
            return
        event.attempt_count += 1
        event.last_error_code = "REVIEW_MODERATION_FAILED"
        if event.attempt_count >= 8:
            event.event_status = "failed"
            event.published_at = utc_now()
        else:
            delay_seconds = min(5 * (2 ** (event.attempt_count - 1)), 300)
            event.available_at = utc_now() + timedelta(seconds=delay_seconds)
        await self.session.commit()


def _rating(value: object) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"))


async def refresh_review_ratings(session: AsyncSession, product_id: int, store_id: int) -> None:
    product_count, product_average = (
        await session.execute(
            select(func.count(Review.id), func.avg(Review.rating)).where(
                Review.product_id == product_id,
                Review.review_status == "published",
            )
        )
    ).one()
    store_count, store_average = (
        await session.execute(
            select(func.count(Review.id), func.avg(Review.rating)).where(
                Review.store_id == store_id,
                Review.review_status == "published",
            )
        )
    ).one()
    product = await session.get(Product, product_id)
    store = await session.get(Store, store_id)
    if product is not None:
        product.review_count = int(product_count or 0)
        product.rating_score = _rating(product_average)
        product.version += 1
    if store is not None:
        store.rating_count = int(store_count or 0)
        store.rating_score = _rating(store_average)
        store.version += 1
