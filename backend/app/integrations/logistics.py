from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol


@dataclass(frozen=True)
class LogisticsProviderTrack:
    provider_event_id: str | None
    status: Literal["picked_up", "in_transit", "delivered", "exception", "returned"]
    provider_status: str
    description: str
    location_text: str | None
    occurred_at: datetime


@dataclass(frozen=True)
class LogisticsProviderSnapshot:
    provider_request_id: str
    tracks: tuple[LogisticsProviderTrack, ...]
    estimated_delivery_min_at: datetime | None = None
    estimated_delivery_max_at: datetime | None = None


class LogisticsProvider(Protocol):
    async def cancel_shipment(
        self, *, carrier_code: str, tracking_no: str
    ) -> Literal["cancelled"]: ...

    async def query_tracking(
        self, *, carrier_code: str, tracking_no: str
    ) -> LogisticsProviderSnapshot: ...


class FakeLogisticsProvider:
    async def cancel_shipment(self, *, carrier_code: str, tracking_no: str) -> Literal["cancelled"]:
        if carrier_code != "fake_express" or not tracking_no:
            raise ValueError("unknown fake logistics shipment")
        return "cancelled"

    async def query_tracking(
        self, *, carrier_code: str, tracking_no: str
    ) -> LogisticsProviderSnapshot:
        if carrier_code != "fake_express" or not tracking_no:
            raise ValueError("unknown fake logistics shipment")
        # The local fake is deliberately inert. Tests and development can drive
        # deterministic state changes through the signed fake webhook.
        return LogisticsProviderSnapshot(
            provider_request_id=f"fake-query-{tracking_no[-4:]}",
            tracks=(),
        )


def logistics_provider(carrier_code: str) -> LogisticsProvider:
    if carrier_code == "fake_express":
        return FakeLogisticsProvider()
    raise ValueError(f"unsupported logistics provider: {carrier_code}")
