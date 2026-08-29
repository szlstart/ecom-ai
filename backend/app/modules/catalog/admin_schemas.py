from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.api.schemas import StrictRequest


class AdminCategoryView(StrictRequest):
    category_id: str
    parent_id: str | None
    category_name: str
    category_code: str
    path: str
    level: int
    sort_order: int
    icon_url: str | None
    status: str
    version: int


class AdminCategoryCreateRequest(StrictRequest):
    parent_id: str | None = Field(default=None, max_length=40)
    category_name: str = Field(min_length=1, max_length=64)
    category_code: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    sort_order: int = Field(default=0, ge=0, le=1_000_000)
    icon_file_id: str | None = Field(default=None, max_length=40)


class AdminCategoryUpdateRequest(StrictRequest):
    parent_id: str | None = Field(default=None, max_length=40)
    category_name: str | None = Field(default=None, min_length=1, max_length=64)
    category_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,63}$")
    sort_order: int | None = Field(default=None, ge=0, le=1_000_000)
    icon_file_id: str | None = Field(default=None, max_length=40)
    status: Literal["active", "disabled"] | None = None


class AdminBrandView(StrictRequest):
    brand_id: str
    brand_name: str
    logo_url: str | None
    description: str | None
    status: str
    version: int


class AdminBrandCreateRequest(StrictRequest):
    brand_name: str = Field(min_length=1, max_length=128)
    logo_file_id: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=2000)


class AdminBrandUpdateRequest(StrictRequest):
    brand_name: str | None = Field(default=None, min_length=1, max_length=128)
    logo_file_id: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "disabled"] | None = None


class AdminInventoryView(StrictRequest):
    sku_id: str
    sku_name: str
    product_id: str
    product_name: str
    store_id: str
    store_name: str
    on_hand_quantity: int
    reserved_quantity: int
    safety_stock_quantity: int
    available_quantity: int
    sold_quantity: int
    status: str
    last_reconciled_at: datetime | None
    version: int


class AdminInventoryList(StrictRequest):
    items: list[AdminInventoryView]


class AdminInventoryAdjustmentRequest(StrictRequest):
    sku_id: str = Field(min_length=5, max_length=40)
    on_hand_delta: int = Field(ge=-1_000_000_000, le=1_000_000_000)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=2, max_length=500)
    reference_no: str = Field(min_length=2, max_length=64)
    expected_version: int = Field(ge=0)

    @model_validator(mode="after")
    def reject_zero_delta(self) -> AdminInventoryAdjustmentRequest:
        if self.on_hand_delta == 0:
            raise ValueError("on_hand_delta must not be zero")
        return self


class AdminInventoryAdjustmentView(StrictRequest):
    adjustment_id: str
    inventory: AdminInventoryView
    on_hand_delta: int
    reason_code: str
    reason: str
    reference_no: str
    adjusted_at: datetime
