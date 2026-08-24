from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY, INTEGER, TINYINT
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import AppendOnlyMySQLModel, MutableMySQLModel, MySQLBase


class Review(MutableMySQLModel, MySQLBase):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("review_no", name="uk_reviews_no"),
        UniqueConstraint("order_item_id", name="uk_reviews_order_item"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),
        Index("idx_reviews_product_published", "product_id", "review_status", "published_at", "id"),
        Index("idx_reviews_user_time", "user_id", "created_at", "id"),
    )

    review_no: Mapped[str] = mapped_column(String(40), nullable=False)
    order_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("orders.id"), nullable=False
    )
    order_item_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("order_items.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("products.id"), nullable=False
    )
    sku_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("product_skus.id"), nullable=False
    )
    rating: Mapped[int] = mapped_column(TINYINT(unsigned=True), nullable=False)
    content: Mapped[str | None] = mapped_column(String(500))
    is_anonymous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    moderation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    helpful_count: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default="0"
    )


class ReviewImage(MutableMySQLModel, MySQLBase):
    __tablename__ = "review_images"
    __table_args__ = (
        UniqueConstraint("review_id", "sort_order", name="uk_review_images_order"),
        UniqueConstraint("object_key", name="uk_review_images_object_key"),
        Index("idx_review_images_review", "review_id", "sort_order", "id"),
    )

    review_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("reviews.id"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    width: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    height: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    sort_order: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    scan_status: Mapped[str] = mapped_column(String(16), nullable=False)
    image_status: Mapped[str] = mapped_column(String(16), nullable=False)


class ReviewReply(MutableMySQLModel, MySQLBase):
    __tablename__ = "review_replies"
    __table_args__ = (UniqueConstraint("review_id", name="uk_review_replies_review"),)

    review_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("reviews.id"), nullable=False
    )
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    replier_user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    content: Mapped[str] = mapped_column(String(2000), nullable=False)
    reply_status: Mapped[str] = mapped_column(String(16), nullable=False, default="published")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class ReviewAppendRecord(MutableMySQLModel, MySQLBase):
    __tablename__ = "review_append_records"
    __table_args__ = (
        UniqueConstraint("append_no", name="uk_review_append_records_no"),
        UniqueConstraint("review_id", name="uk_review_append_review"),
    )

    append_no: Mapped[str] = mapped_column(String(40), nullable=False)
    review_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("reviews.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    content: Mapped[str] = mapped_column(String(500), nullable=False)
    append_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    moderation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class ReviewAppendImage(MutableMySQLModel, MySQLBase):
    __tablename__ = "review_append_images"
    __table_args__ = (
        UniqueConstraint("append_record_id", "sort_order", name="uk_review_append_images_order"),
        UniqueConstraint("object_key", name="uk_review_append_images_object_key"),
        Index(
            "idx_review_append_images_append",
            "append_record_id",
            "sort_order",
            "id",
        ),
    )

    append_record_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("review_append_records.id"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    width: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    height: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    sort_order: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    scan_status: Mapped[str] = mapped_column(String(16), nullable=False)
    image_status: Mapped[str] = mapped_column(String(16), nullable=False)


class ReviewRevisionRecord(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "review_revision_records"
    __table_args__ = (
        UniqueConstraint("revision_no", name="uk_review_revision_records_no"),
        Index(
            "idx_review_revision_records_review",
            "review_id",
            "created_at",
            "id",
        ),
    )

    revision_no: Mapped[str] = mapped_column(String(40), nullable=False)
    review_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("reviews.id"), nullable=False
    )
    actor_user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    before_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    after_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
