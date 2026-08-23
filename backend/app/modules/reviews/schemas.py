from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ReviewImageView(BaseModel):
    file_id: str
    url: str
    thumbnail_url: str
    width: int
    height: int


class ReviewAppendView(BaseModel):
    content: str
    published_at: datetime


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
