from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.api.schemas import StrictRequest

ShipmentStatus = Literal[
    "created",
    "picked_up",
    "in_transit",
    "delivered",
    "exception",
    "returned",
    "closed",
    "voided",
]


class DeliveryEstimate(StrictRequest):
    type: Literal["delivery"] = "delivery"
    status: Literal["available", "unavailable"]
    min_at: datetime | None = None
    max_at: datetime | None = None
    source: Literal["shipping_template", "carrier"] | None = None
    updated_at: datetime | None = None
    disclaimer: str = "预计时间仅供参考，以承运商实际配送为准。"


class ShipmentItemView(StrictRequest):
    order_item_id: str
    product_name: str
    sku_name: str
    quantity: int


class ShipmentTrackView(StrictRequest):
    track_status: str
    description: str
    location_text: str | None = None
    occurred_at: datetime
    received_at: datetime


class UserOrderShipmentSummary(StrictRequest):
    shipment_id: str
    carrier_code: str
    carrier_name: str
    tracking_no_masked: str
    shipment_status: ShipmentStatus
    items: list[ShipmentItemView]
    delivery_estimate: DeliveryEstimate
    last_track: ShipmentTrackView | None = None
    last_synced_at: datetime | None = None


class UserOrderShipmentList(StrictRequest):
    order_id: str
    items: list[UserOrderShipmentSummary]


class UserShipmentDetail(StrictRequest):
    shipment_id: str
    order_id: str
    carrier_code: str
    carrier_name: str
    tracking_no: str
    tracking_no_masked: str
    shipment_status: ShipmentStatus
    items: list[ShipmentItemView]
    delivery_estimate: DeliveryEstimate
    latest_tracks: list[ShipmentTrackView]
    last_synced_at: datetime | None = None
    version: int


class ShipmentTrackList(StrictRequest):
    shipment_id: str
    items: list[ShipmentTrackView]


class ShipmentRefreshResult(StrictRequest):
    shipment_id: str
    status: Literal["queued"] = "queued"
    requested_at: datetime


class AdminShipmentCreateItem(StrictRequest):
    order_item_id: str = Field(pattern=r"^oit_[0-9A-Z]+$", max_length=40)
    quantity: int = Field(gt=0, le=1_000_000)


class AdminShipmentCreateRequest(StrictRequest):
    carrier_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    carrier_name: str = Field(min_length=1, max_length=64)
    tracking_no: str = Field(
        min_length=6,
        max_length=64,
        json_schema_extra={"writeOnly": True},
    )
    items: list[AdminShipmentCreateItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def reject_duplicate_items(self) -> AdminShipmentCreateRequest:
        item_ids = [item.order_item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("order item ids must be unique within one shipment")
        return self


class AdminShipmentDetail(StrictRequest):
    shipment_id: str
    order_id: str
    store_id: str
    carrier_code: str
    carrier_name: str
    tracking_no_masked: str
    shipment_status: ShipmentStatus
    items: list[ShipmentItemView]
    delivery_estimate: DeliveryEstimate
    latest_tracks: list[ShipmentTrackView]
    shipped_at: datetime
    last_synced_at: datetime | None = None
    version: int
