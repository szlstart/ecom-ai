from __future__ import annotations

from typing import Literal

from app.api.schemas import StrictRequest


class RealtimeTicketView(StrictRequest):
    ticket: str
    expires_in: int
    websocket_path: str = "/ws/v1"
    subprotocol: Literal["ecom.realtime.v1"] = "ecom.realtime.v1"
