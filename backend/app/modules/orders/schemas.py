from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.api.schemas import StrictRequest


class OrderCreateRequest(StrictRequest):
    checkout_id: str = Field(pattern=r"^chk_[0-9A-Z]+$", max_length=40)
    checkout_version: int = Field(ge=0)


class OrderCreateResponse(StrictRequest):
    trade_order_id: str
    order_ids: list[str]
    payment_deadline_at: datetime
    available_actions: list[str]
    version: int
