from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.api.schemas import StrictRequest


class AdminStoreProductGroupView(StrictRequest):
    group_id: str
    store_id: str
    parent_group_id: str | None
    group_name: str
    status: str
    sort_order: int
    product_ids: list[str]
    version: int


class AdminStoreProductGroupCreateRequest(StrictRequest):
    parent_group_id: str | None = Field(default=None, max_length=40)
    group_name: str = Field(min_length=1, max_length=64)
    sort_order: int = Field(default=0, ge=0, le=100_000)


class AdminStoreProductGroupUpdateRequest(StrictRequest):
    parent_group_id: str | None = Field(default=None, max_length=40)
    group_name: str | None = Field(default=None, min_length=1, max_length=64)
    status: Literal["active", "disabled"] | None = None
    sort_order: int | None = Field(default=None, ge=0, le=100_000)


class AdminStoreProductGroupProductsRequest(StrictRequest):
    product_ids: list[str] = Field(max_length=200)


class AdminShippingRuleInput(StrictRequest):
    region_scope: dict[str, object]
    first_unit: int = Field(ge=1, le=1_000_000)
    additional_unit: int = Field(ge=1, le=1_000_000)
    first_fee_amount: int = Field(ge=0, le=100_000_000)
    additional_fee_amount: int = Field(ge=0, le=100_000_000)
    estimated_min_days: int | None = Field(default=None, ge=0, le=365)
    estimated_max_days: int | None = Field(default=None, ge=0, le=365)

    @model_validator(mode="after")
    def validate_estimate(self) -> AdminShippingRuleInput:
        if (
            self.estimated_min_days is not None
            and self.estimated_max_days is not None
            and self.estimated_min_days > self.estimated_max_days
        ):
            raise ValueError("estimated_min_days must not exceed estimated_max_days")
        return self


class AdminShippingTemplateView(StrictRequest):
    template_id: str
    template_family_id: str
    store_id: str
    template_name: str
    delivery_type: str
    charge_mode: str
    currency: str
    status: str
    dispatch_min_hours: int
    dispatch_max_hours: int
    policy_version: int
    rules: list[AdminShippingRuleInput]
    version: int


class AdminShippingTemplateCreateRequest(StrictRequest):
    template_family_id: str | None = Field(default=None, max_length=40)
    template_name: str = Field(min_length=1, max_length=128)
    delivery_type: Literal["express", "same_day", "self_pickup"]
    charge_mode: Literal["fixed", "by_item", "by_weight"]
    currency: Literal["CNY"] = "CNY"
    dispatch_min_hours: int = Field(ge=0, le=8760)
    dispatch_max_hours: int = Field(ge=0, le=8760)
    rules: list[AdminShippingRuleInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_dispatch(self) -> AdminShippingTemplateCreateRequest:
        if self.dispatch_min_hours > self.dispatch_max_hours:
            raise ValueError("dispatch_min_hours must not exceed dispatch_max_hours")
        return self


class AdminShippingTemplateUpdateRequest(StrictRequest):
    template_name: str | None = Field(default=None, min_length=1, max_length=128)
    delivery_type: Literal["express", "same_day", "self_pickup"] | None = None
    charge_mode: Literal["fixed", "by_item", "by_weight"] | None = None
    dispatch_min_hours: int | None = Field(default=None, ge=0, le=8760)
    dispatch_max_hours: int | None = Field(default=None, ge=0, le=8760)
    rules: list[AdminShippingRuleInput] | None = Field(default=None, min_length=1, max_length=100)


class AdminShippingTemplatePublicationRequest(StrictRequest):
    reason: str = Field(min_length=2, max_length=500)


class AdminStoreAnnouncementView(StrictRequest):
    announcement_id: str
    store_id: str
    title: str
    content: str
    status: str
    starts_at: datetime | None
    ends_at: datetime | None
    sort_order: int
    version: int


class AdminStoreAnnouncementCreateRequest(StrictRequest):
    title: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=2000)
    status: Literal["draft", "published", "disabled"] = "draft"
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    sort_order: int = Field(default=0, ge=0, le=100_000)

    @model_validator(mode="after")
    def validate_window(self) -> AdminStoreAnnouncementCreateRequest:
        if (
            self.starts_at is not None
            and self.ends_at is not None
            and self.ends_at <= self.starts_at
        ):
            raise ValueError("ends_at must be later than starts_at")
        return self


class AdminStoreAnnouncementUpdateRequest(StrictRequest):
    title: str | None = Field(default=None, min_length=1, max_length=128)
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    status: Literal["draft", "published", "disabled"] | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    sort_order: int | None = Field(default=None, ge=0, le=100_000)


class AdminFeaturedProductInput(StrictRequest):
    product_id: str = Field(max_length=40)
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @model_validator(mode="after")
    def validate_window(self) -> AdminFeaturedProductInput:
        if (
            self.starts_at is not None
            and self.ends_at is not None
            and self.ends_at <= self.starts_at
        ):
            raise ValueError("ends_at must be later than starts_at")
        return self


class AdminFeaturedProductSetRequest(StrictRequest):
    slot_type: Literal["recommended", "hot"]
    items: list[AdminFeaturedProductInput] = Field(max_length=12)


class AdminFeaturedProductView(AdminFeaturedProductInput):
    slot_type: str
    sort_order: int
