from __future__ import annotations

from typing import Annotated

from pydantic import Field, model_validator

from app.api.schemas import StrictRequest
from app.modules.catalog.schemas import Money

CartItemId = Annotated[str, Field(pattern=r"^ci_[0-9A-Z]+$", max_length=40)]


class CartItemCreateRequest(StrictRequest):
    sku_id: str = Field(pattern=r"^sku_[0-9A-Z]+$", max_length=40)
    quantity: int = Field(ge=1, le=99)


class CartItemPatchRequest(StrictRequest):
    quantity: int | None = Field(default=None, ge=1, le=99)
    is_selected: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> CartItemPatchRequest:
        if self.quantity is None and self.is_selected is None:
            raise ValueError("quantity or is_selected is required")
        return self


class CartSelectionReplaceRequest(StrictRequest):
    cart_item_ids: list[CartItemId] = Field(min_length=1, max_length=100)
    is_selected: bool

    @model_validator(mode="after")
    def unique_item_ids(self) -> CartSelectionReplaceRequest:
        if len(self.cart_item_ids) != len(set(self.cart_item_ids)):
            raise ValueError("cart_item_ids must be unique")
        return self


class CartItemView(StrictRequest):
    cart_item_id: str
    product_id: str
    sku_id: str
    product_name: str
    sku_name: str
    quantity: int
    is_selected: bool
    added_price: Money
    current_price: Money
    price_changed: bool
    available_quantity: int
    is_valid: bool
    invalid_reason: str | None


class CartStoreGroupView(StrictRequest):
    store_id: str
    store_name: str
    items: list[CartItemView]
    selected_quantity: int
    selected_amount: Money


class CartAmountSummary(StrictRequest):
    selected_goods_amount: Money


class CartView(StrictRequest):
    cart_id: str | None
    groups: list[CartStoreGroupView]
    cart_total_quantity: int
    selected_quantity: int
    valid_item_count: int
    amount_summary: CartAmountSummary
    version: int
