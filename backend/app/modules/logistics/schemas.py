from __future__ import annotations

from datetime import datetime
from typing import Literal

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
