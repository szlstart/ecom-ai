from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.api.schemas import StrictRequest

ReviewAction = Literal["create", "view", "edit", "append"]


class ReviewImageView(BaseModel):
    file_id: str
    url: str
    thumbnail_url: str
    width: int
    height: int


class ReviewAppendView(BaseModel):
    content: str
    published_at: datetime
    images: list[ReviewImageView] = Field(default_factory=list)


class ReviewReplyView(BaseModel):
    content: str
    published_at: datetime


class ProductReviewView(BaseModel):
    review_id: str
    user_display_name: str
    sku_id: str
    sku_name: str
    rating: int
    content: str | None
    published_at: datetime
    helpful_count: int
    images: list[ReviewImageView]
    append: ReviewAppendView | None
    merchant_reply: ReviewReplyView | None


class ProductReviewSummary(BaseModel):
    review_count: int
    average_rating: str
    rating_distribution: dict[str, int]
    image_review_count: int


class ProductReviewList(BaseModel):
    summary: ProductReviewSummary
    items: list[ProductReviewView]


class ReviewEligibility(StrictRequest):
    order_item_id: str
    order_id: str
    product_id: str
    sku_id: str
    product_name: str
    sku_name: str
    order_completed_at: datetime | None
    review_deadline_at: datetime | None
    eligible: bool
    reason_code: str | None = None
    reason_message: str | None = None
    existing_review_id: str | None = None
    available_actions: list[ReviewAction]


class ReviewCreateRequest(StrictRequest):
    order_item_id: str = Field(pattern=r"^oit_[0-9A-Z]+$", max_length=40)
    rating: int = Field(ge=1, le=5)
    content: str | None = Field(default=None, max_length=500)
    is_anonymous: bool = False
    image_file_ids: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("image_file_ids")
    @classmethod
    def reject_duplicate_images(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("review image file ids must be unique")
        if any(not item.startswith("file_") or len(item) > 40 for item in value):
            raise ValueError("review image file id is invalid")
        return value


class ReviewUpdateRequest(StrictRequest):
    rating: int = Field(ge=1, le=5)
    content: str | None = Field(default=None, max_length=500)
    is_anonymous: bool = False
    image_file_ids: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("image_file_ids")
    @classmethod
    def reject_duplicate_images(cls, value: list[str]) -> list[str]:
        return ReviewCreateRequest.reject_duplicate_images(value)


class ReviewAppendCreateRequest(StrictRequest):
    content: str = Field(min_length=1, max_length=500)
    image_file_ids: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("image_file_ids")
    @classmethod
    def reject_duplicate_images(cls, value: list[str]) -> list[str]:
        return ReviewCreateRequest.reject_duplicate_images(value)


class MyReviewImageView(StrictRequest):
    file_id: str
    width: int
    height: int


class MyReviewAppendView(StrictRequest):
    append_id: str
    content: str
    append_status: Literal["pending", "published", "hidden", "rejected"]
    moderation_status: Literal["pending", "passed", "blocked", "manual"]
    images: list[MyReviewImageView]
    submitted_at: datetime
    published_at: datetime | None


class MyReviewView(StrictRequest):
    review_id: str
    order_id: str
    order_item_id: str
    product_id: str
    sku_id: str
    product_name: str
    sku_name: str
    rating: int
    content: str | None
    is_anonymous: bool
    review_status: Literal["pending", "published", "hidden", "rejected"]
    moderation_status: Literal["pending", "passed", "blocked", "manual"]
    images: list[MyReviewImageView]
    append: MyReviewAppendView | None = None
    merchant_reply: ReviewReplyView | None = None
    submitted_at: datetime
    published_at: datetime | None
    edit_deadline_at: datetime
    append_deadline_at: datetime
    available_actions: list[ReviewAction]
    version: int


class MyReviewListItem(StrictRequest):
    item_type: Literal["pending", "review"]
    order_id: str
    order_item_id: str
    product_id: str
    sku_id: str
    product_name: str
    sku_name: str
    order_completed_at: datetime | None
    eligibility: ReviewEligibility
    review: MyReviewView | None = None


class MyReviewList(StrictRequest):
    items: list[MyReviewListItem]


class AdminReviewReplyRequest(StrictRequest):
    content: str = Field(min_length=2, max_length=500)


class AdminReviewModerationRequest(StrictRequest):
    action: Literal["hide", "restore"]
    rule_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=2, max_length=1000)


class AdminReviewGovernanceView(StrictRequest):
    governance_id: str
    action: Literal["hide", "restore"]
    from_status: str
    to_status: str
    rule_code: str
    reason: str
    occurred_at: datetime


class AdminReviewView(StrictRequest):
    review_id: str
    order_id: str
    order_item_id: str
    user_id: str
    user_name: str
    store_id: str
    store_name: str
    product_id: str
    product_name: str
    sku_id: str
    sku_name: str
    rating: int
    content: str | None
    is_anonymous: bool
    review_status: Literal["pending", "published", "hidden", "rejected"]
    moderation_status: Literal["pending", "passed", "blocked", "manual"]
    merchant_reply: ReviewReplyView | None
    governance_history: list[AdminReviewGovernanceView]
    submitted_at: datetime
    published_at: datetime | None
    version: int


class AdminReviewList(StrictRequest):
    items: list[AdminReviewView]
