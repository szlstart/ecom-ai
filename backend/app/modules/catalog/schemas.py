from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Money(BaseModel):
    minor_units: str = Field(pattern=r"^\d+$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class PublicImage(BaseModel):
    file_id: str
    url: str
    thumbnail_url: str
    alt_text: str | None
    width: int
    height: int
    sort_order: int


class ProductCard(BaseModel):
    product_id: str
    store_id: str
    store_name: str
    product_name: str
    subtitle: str | None
    price: Money
    price_range: Money | None = None
    sales_count: int
    rating_score: str
    main_image: PublicImage | None
    is_favorited: bool = False


class ProductList(BaseModel):
    items: list[ProductCard]


class StoreSummary(BaseModel):
    store_id: str
    store_name: str
    logo_url: str | None
    store_status: str
    rating_score: str


class SafeContent(BaseModel):
    content_format: Literal["structured_v1", "safe_html_v1"]
    content_version: int
    content_hash: str
    safe_blocks: list[dict[str, object]] | None = None
    safe_html: str | None = None
    safe_text_fallback: str

    @model_validator(mode="after")
    def validate_discriminated_payload(self) -> SafeContent:
        if self.content_format == "structured_v1":
            if self.safe_blocks is None or self.safe_html is not None:
                raise ValueError("structured content must contain blocks only")
        elif self.safe_html is None or self.safe_blocks is not None:
            raise ValueError("safe HTML content must contain safe_html only")
        return self


class ServiceEstimate(BaseModel):
    estimate_type: Literal["dispatch", "delivery"]
    status: Literal["available", "unavailable"]
    min_at: datetime | None = None
    max_at: datetime | None = None
    source: Literal["store_fulfillment_profile", "shipping_template", "carrier"] | None = None
    source_updated_at: datetime | None = None
    calculated_at: datetime
    timezone: str
    disclaimer_code: str | None = None
    unavailable_reason_code: str | None = None


class ProductDetail(BaseModel):
    product_id: str
    product_name: str
    subtitle: str | None
    description: str | None
    product_status: str
    category_id: str
    brand_id: str | None
    store: StoreSummary
    price_range: tuple[Money, Money]
    sales_count: int
    review_count: int
    rating_score: str
    public_images: list[PublicImage]
    default_sku_id: str | None
    detail_content: SafeContent | None
    attributes: list[dict[str, str | bool | None]]
    origin_region_code: str | None
    dispatch_estimate: ServiceEstimate
    purchase_notice: str | None
    fulfillment_profile_version: int | None
    is_favorited: bool = False


class ProductSkuView(BaseModel):
    sku_id: str
    sku_name: str
    spec_values: list[dict[str, str]]
    sale_price: Money
    market_price: Money
    sku_status: str
    stock_status: Literal["in_stock", "low_stock", "out_of_stock", "frozen"]
    low_stock_remaining: int | None = None
    max_purchase_quantity: int
    sales_count: int
    images: list[PublicImage]
    image_fallback: Literal["none", "product_public_images"]


class ProductSkuList(BaseModel):
    items: list[ProductSkuView]


class ProductFaqView(BaseModel):
    faq_id: str
    question: str
    answer_content: SafeContent


class ProductFaqList(BaseModel):
    items: list[ProductFaqView]


class CategoryView(BaseModel):
    category_id: str
    parent_id: str | None
    category_name: str
    category_code: str
    level: int
    sort_order: int
    icon_url: str | None
    children: list[CategoryView] = Field(default_factory=list)


class BrandView(BaseModel):
    brand_id: str
    brand_name: str
    logo_url: str | None
    description: str | None


class SearchSuggestionList(BaseModel):
    items: list[str]


class HomepageSection(BaseModel):
    section: Literal["recommended", "hot", "new_arrival"]
    title: str
    status: Literal["available", "unavailable"]
    items: list[ProductCard]
    next_cursor: str | None = None
    error_code: str | None = None


class HomepageView(BaseModel):
    feed_version: str
    announcements: list[dict[str, str]]
    banners: list[dict[str, object]]
    categories: list[CategoryView]
    sections: list[HomepageSection]
