from __future__ import annotations

from typing import Literal, Protocol


class LogisticsProvider(Protocol):
    async def cancel_shipment(
        self, *, carrier_code: str, tracking_no: str
    ) -> Literal["cancelled"]: ...


class FakeLogisticsProvider:
    async def cancel_shipment(self, *, carrier_code: str, tracking_no: str) -> Literal["cancelled"]:
        if carrier_code != "fake_express" or not tracking_no:
            raise ValueError("unknown fake logistics shipment")
        return "cancelled"


def logistics_provider(carrier_code: str) -> LogisticsProvider:
    if carrier_code == "fake_express":
        return FakeLogisticsProvider()
    raise ValueError(f"unsupported logistics provider: {carrier_code}")
