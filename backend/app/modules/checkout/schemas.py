from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.api.schemas import StrictRequest
from app.modules.catalog.schemas import Money, ServiceEstimate

CheckoutId = Annotated[str, Field(pattern=r"^chk_[0-9A-Z]+$", max_length=40)]
CartItemId = Annotated[str, Field(pattern=r"^ci_[0-9A-Z]+$", max_length=40)]


class BuyNowSource(StrictRequest):
    source_type: Literal["buy_now"]
    sku_id: str = Field(pattern=r"^sku_[0-9A-Z]+$", max_length=40)
    quantity: int = Field(ge=1, le=99)


class CartSource(StrictRequest):
    source_type: Literal["cart"]
    cart_item_ids: list[CartItemId] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def require_unique_items(self) -> CartSource:
        if len(self.cart_item_ids) != len(set(self.cart_item_ids)):
            raise ValueError("cart_item_ids must be unique")
        return self


class CheckoutCreateRequest(StrictRequest):
    source: Annotated[BuyNowSource | CartSource, Field(discriminator="source_type")]
    address_id: str | None = Field(default=None, pattern=r"^addr_[0-9A-Z]+$", max_length=40)


class BuyerRemark(StrictRequest):
    store_id: str = Field(pattern=r"^sto_[0-9A-Z]+$", max_length=40)
    content: str = Field(max_length=200)

    @model_validator(mode="after")
    def normalize_and_validate(self) -> BuyerRemark:
        value = self.content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("remark contains control characters")
        if re.search(r"[\u202a-\u202e\u2066-\u2069]", value):
            raise ValueError("remark contains bidirectional control characters")
        self.content = value
        return self


class CheckoutPatchRequest(StrictRequest):
    address_id: str | None = Field(default=None, pattern=r"^addr_[0-9A-Z]+$", max_length=40)
    buyer_remarks: list[BuyerRemark] | None = Field(default=None, max_length=100)
    quantity: int | None = Field(default=None, ge=1, le=99)

    @model_validator(mode="after")
    def require_change(self) -> CheckoutPatchRequest:
        if self.address_id is None and self.buyer_remarks is None and self.quantity is None:
            raise ValueError("address_id, buyer_remarks or quantity is required")
        if self.buyer_remarks is not None:
            ids = [item.store_id for item in self.buyer_remarks]
            if len(ids) != len(set(ids)):
                raise ValueError("buyer_remarks store_id must be unique")
        return self


class CheckoutItemView(StrictRequest):
    product_id: str
    sku_id: str
    product_name: str
    sku_name: str
    image_url: str | None = None
    quantity: int
    unit_price: Money
    subtotal: Money
    available_quantity: int


class DeliveryOptionView(StrictRequest):
    option_id: str
    name: str
    freight: Money
    estimate: ServiceEstimate


class CheckoutStoreGroupView(StrictRequest):
    store_id: str
    store_name: str
    items: list[CheckoutItemView]
    goods_amount: Money
    freight_amount: Money
    delivery_options: list[DeliveryOptionView]
    selected_delivery_option: str | None
    buyer_remark: str | None
    policy_versions: dict[str, int]
    customer_service_context: dict[str, str]


class CheckoutAmounts(StrictRequest):
    goods_amount: Money
    freight_amount: Money
    payable_amount: Money


class CheckoutIssue(StrictRequest):
    code: str
    message: str
    store_id: str | None = None
    sku_id: str | None = None


class CheckoutView(StrictRequest):
    checkout_id: str
    source_type: Literal["buy_now", "cart"]
    status: Literal["active", "submitted", "expired", "cancelled"]
    address_id: str | None
    expires_at: datetime
    store_groups: list[CheckoutStoreGroupView]
    amounts: CheckoutAmounts
    warnings: list[CheckoutIssue]
    blocking_issues: list[CheckoutIssue]
    available_actions: list[str]
    pricing_version: str
    version: int
