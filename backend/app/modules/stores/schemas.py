from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.catalog.schemas import ProductCard


class StorePublicView(BaseModel):
    store_id: str
    store_name: str
    logo_url: str | None
    description: str | None
    store_status: str
    visibility_mode: str
    rating_score: str
    rating_count: int
    follower_count: int
    sales_count: int
    opened_at: datetime | None
    active_product_count: int
    is_followed: bool = False
    customer_service_enabled: bool


class StoreProductGroupView(BaseModel):
    group_id: str
    group_name: str
    sort_order: int
    visible_product_count: int
    children: list[StoreProductGroupView] = Field(default_factory=list)


class StoreProductGroupList(BaseModel):
    items: list[StoreProductGroupView]


class StorePolicyView(BaseModel):
    policy_id: str
    policy_type: str
    title: str
    content: str
    policy_version: int
    effective_at: datetime
    expires_at: datetime | None


class StorePolicyList(BaseModel):
    items: list[StorePolicyView]


class StoreHomeContent(BaseModel):
    announcements: list[dict[str, str]]
    recommended_products: list[ProductCard]
    hot_products: list[ProductCard]


class FollowedStoreList(BaseModel):
    items: list[StorePublicView]
