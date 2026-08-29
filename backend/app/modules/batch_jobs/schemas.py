from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.api.schemas import StrictRequest


class ProductImportJobCreateRequest(StrictRequest):
    job_type: Literal["product_import"]
    store_id: str = Field(min_length=5, max_length=40)
    input_file_id: str = Field(min_length=6, max_length=40)
    schema_version: Literal["product-import-v1"]


class BatchJobConfirmationRequest(StrictRequest):
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class BatchJobCancellationRequest(StrictRequest):
    reason: str = Field(min_length=2, max_length=500)


class BatchJobItemView(StrictRequest):
    item_key: str
    item_status: str
    resource_type: str | None
    resource_id: str | None
    error_code: str | None
    error_message: str | None


class BatchJobItemList(StrictRequest):
    items: list[BatchJobItemView]
    next_cursor: str | None = None


class BatchJobView(StrictRequest):
    job_id: str
    job_type: str
    store_id: str
    schema_version: str
    status: str
    total_count: int
    success_count: int
    failure_count: int
    preview_hash: str | None
    input_file_id: str | None
    result_file_id: str | None
    error_file_id: str | None
    error_code: str | None
    error_summary: str | None
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    expires_at: datetime | None
    available_actions: list[str]
    version: int


class BatchJobList(StrictRequest):
    items: list[BatchJobView]
    next_cursor: str | None = None


class ProductImportTemplateColumn(StrictRequest):
    name: str
    required: bool
    description: str
    example: str


class ProductImportTemplateView(StrictRequest):
    schema_version: str
    supported_file_types: list[str]
    maximum_rows: int
    columns: list[ProductImportTemplateColumn]


class ParsedProductImportRow(StrictRequest):
    row_number: int
    product_key: str
    product_name: str
    subtitle: str | None = None
    category_id: str
    brand_id: str | None = None
    merchant_sku_code: str
    sku_name: str
    spec_values: list[dict[str, str]]
    sale_price_amount: int
    market_price_amount: int
    currency: Literal["CNY"] = "CNY"
    weight_grams: int | None = None
    barcode: str | None = None
    initial_stock: int = 0

    @model_validator(mode="after")
    def validate_price_and_specs(self) -> ParsedProductImportRow:
        if self.market_price_amount < self.sale_price_amount:
            raise ValueError(
                "market_price_amount must be greater than or equal to sale_price_amount"
            )
        if not self.spec_values:
            raise ValueError("spec_values_json must contain at least one specification")
        keys = [item.get("name", "").strip().casefold() for item in self.spec_values]
        if any(not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("specification names must be non-empty and unique")
        if any(not item.get("value", "").strip() for item in self.spec_values):
            raise ValueError("specification values must be non-empty")
        return self
