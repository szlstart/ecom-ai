from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.api.schemas import StrictRequest


class AdminProductCompleteness(StrictRequest):
    basic: bool
    sku: bool
    main_image: bool
    attributes: bool
    fulfillment: bool
    detail_content: bool
    missing_requirements: list[str]


class AdminProductSummary(StrictRequest):
    product_id: str
    store_id: str
    store_name: str
    category_id: str
    category_name: str
    brand_id: str | None
    brand_name: str | None
    product_name: str
    subtitle: str | None
    status: str
    min_price: str
    max_price: str
    currency: str
    cover_image_url: str | None = None
    sku_count: int = 0
    available_quantity: int = 0
    sales_count: int = 0
    review_count: int = 0
    rating_score: str = "0.00"
    updated_at: datetime
    version: int


class AdminProductList(StrictRequest):
    items: list[AdminProductSummary]
    next_cursor: str | None


class AdminProductDeletionView(StrictRequest):
    product_id: str
    deleted_at: datetime
    previous_status: str
    version: int


class AdminProductDetail(AdminProductSummary):
    description: str | None
    default_sku_id: str | None
    current_detail_content_version_id: str | None
    published_detail_content_version_id: str | None
    completeness: AdminProductCompleteness
    available_actions: list[str]
    published_at: datetime | None
    off_shelf_at: datetime | None


class AdminProductCreateRequest(StrictRequest):
    store_id: str = Field(min_length=5, max_length=40)
    category_id: str = Field(min_length=5, max_length=40)
    brand_id: str | None = Field(default=None, min_length=5, max_length=40)
    product_name: str = Field(min_length=1, max_length=255)
    subtitle: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)


class AdminProductUpdateRequest(StrictRequest):
    category_id: str | None = Field(default=None, min_length=5, max_length=40)
    brand_id: str | None = Field(default=None, min_length=5, max_length=40)
    product_name: str | None = Field(default=None, min_length=1, max_length=255)
    subtitle: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)


class AdminProductCommandRequest(StrictRequest):
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=2, max_length=500)


class AdminProductModerationRequest(AdminProductCommandRequest):
    decision: Literal["approve", "reject", "request_changes"]


class AdminSkuSpecValue(StrictRequest):
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=128)


class AdminSkuView(StrictRequest):
    sku_id: str
    product_id: str
    merchant_sku_code: str | None
    sku_name: str
    spec_values: list[AdminSkuSpecValue]
    sale_price: str
    market_price: str
    currency: str
    weight_grams: int | None
    barcode: str | None
    status: str
    version: int


class AdminSkuCreateRequest(StrictRequest):
    merchant_sku_code: str | None = Field(default=None, max_length=64)
    sku_name: str = Field(min_length=1, max_length=255)
    spec_values: list[AdminSkuSpecValue] = Field(min_length=1, max_length=20)
    sale_price_amount: int = Field(ge=0, le=999_999_999_999)
    market_price_amount: int = Field(ge=0, le=999_999_999_999)
    currency: Literal["CNY"] = "CNY"
    weight_grams: int | None = Field(default=None, ge=0, le=1_000_000)
    barcode: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_prices_and_specs(self) -> AdminSkuCreateRequest:
        if self.market_price_amount < self.sale_price_amount:
            raise ValueError("market_price_amount must not be lower than sale_price_amount")
        names = [item.name.casefold() for item in self.spec_values]
        if len(names) != len(set(names)):
            raise ValueError("spec value names must be unique")
        return self


class AdminSkuUpdateRequest(StrictRequest):
    merchant_sku_code: str | None = Field(default=None, max_length=64)
    sku_name: str | None = Field(default=None, min_length=1, max_length=255)
    spec_values: list[AdminSkuSpecValue] | None = Field(default=None, min_length=1, max_length=20)
    sale_price_amount: int | None = Field(default=None, ge=0, le=999_999_999_999)
    market_price_amount: int | None = Field(default=None, ge=0, le=999_999_999_999)
    weight_grams: int | None = Field(default=None, ge=0, le=1_000_000)
    barcode: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_specs(self) -> AdminSkuUpdateRequest:
        if self.spec_values:
            names = [item.name.casefold() for item in self.spec_values]
            if len(names) != len(set(names)):
                raise ValueError("spec value names must be unique")
        return self


class AdminSkuStatusRequest(AdminProductCommandRequest):
    action: Literal["enable", "disable"]


class AdminProductImageInput(StrictRequest):
    file_id: str = Field(min_length=5, max_length=40)
    sku_id: str | None = Field(default=None, min_length=5, max_length=40)
    image_type: Literal["main", "gallery", "detail", "spec"]
    alt_text: str | None = Field(default=None, max_length=255)
    sort_order: int = Field(ge=0, le=10_000)


class AdminProductImageSetRequest(StrictRequest):
    items: list[AdminProductImageInput] = Field(min_length=1, max_length=100)


class AdminProductImageView(AdminProductImageInput):
    image_url: str
    width: int
    height: int
    status: str


class AdminProductAttributeInput(StrictRequest):
    attribute_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    attribute_name: str = Field(min_length=1, max_length=128)
    value_text: str = Field(min_length=1, max_length=1000)
    value_normalized: str | None = Field(default=None, max_length=500)
    unit: str | None = Field(default=None, max_length=32)
    is_searchable: bool = False
    sort_order: int = Field(default=0, ge=0, le=10_000)


class AdminProductAttributeSetRequest(StrictRequest):
    items: list[AdminProductAttributeInput] = Field(max_length=100)


class AdminProductFulfillmentRequest(StrictRequest):
    shipping_template_id: str = Field(min_length=5, max_length=40)
    origin_region_code: str = Field(pattern=r"^[A-Z0-9_-]{2,32}$")
    dispatch_min_hours: int = Field(ge=0, le=8760)
    dispatch_max_hours: int = Field(ge=0, le=8760)
    purchase_notice: str | None = Field(default=None, max_length=3000)

    @model_validator(mode="after")
    def validate_dispatch_window(self) -> AdminProductFulfillmentRequest:
        if self.dispatch_max_hours < self.dispatch_min_hours:
            raise ValueError("dispatch_max_hours must not be lower than dispatch_min_hours")
        return self


class AdminProductFulfillmentView(AdminProductFulfillmentRequest):
    profile_version: int
    version: int


class AdminContentVersionCreateRequest(StrictRequest):
    source_format: Literal["plain_text", "structured", "html"]
    source_content: str = Field(min_length=1, max_length=100_000)


class AdminContentVersionView(StrictRequest):
    version_id: str
    content_version: int
    source_format: str
    source_content: str
    public_content_format: str
    safe_blocks: list[dict[str, object]] | None
    safe_html: str | None
    safe_text: str
    security_scan_status: str
    status: str
    created_at: datetime


class AdminFaqCreateRequest(AdminContentVersionCreateRequest):
    question: str = Field(min_length=1, max_length=1000)
    sort_order: int = Field(default=0, ge=0, le=10_000)


class AdminFaqVersionCreateRequest(AdminContentVersionCreateRequest):
    pass


class AdminFaqPublicationRequest(StrictRequest):
    version_id: str = Field(min_length=5, max_length=40)
    reason: str = Field(min_length=2, max_length=500)


class AdminFaqView(StrictRequest):
    faq_id: str
    product_id: str
    question: str
    status: str
    sort_order: int
    current_version_id: str | None
    current_answer_text: str | None = None
    published_version_id: str | None
    published_at: datetime | None
    version: int
