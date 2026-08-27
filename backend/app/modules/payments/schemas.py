from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field

from app.api.schemas import StrictRequest
from app.modules.catalog.schemas import Money

PaymentStatus = Literal[
    "created",
    "pending",
    "succeeded",
    "failed",
    "closed",
    "partially_refunded",
    "refunded",
]


class PaymentCreateRequest(StrictRequest):
    trade_order_id: str = Field(min_length=5, max_length=32)
    provider: Literal["fake"]
    payment_method: Literal["fake_balance", "wallet_balance"]
    return_url_key: Literal["payment_result"]


class PaymentAction(StrictRequest):
    type: Literal["redirect"]
    url: str


class PaymentEventView(StrictRequest):
    event_id: str
    event_type: str
    from_status: str | None
    to_status: str
    amount: Money
    source_type: str
    occurred_at: datetime


class PaymentView(StrictRequest):
    payment_id: str
    trade_order_id: str
    provider: str
    payment_method: str
    payment_status: PaymentStatus
    display_status: Literal["confirming", "succeeded", "failed", "closed", "refunded"]
    requested_amount: Money
    paid_amount: Money
    refunded_amount: Money
    expires_at: datetime
    paid_at: datetime | None
    closed_at: datetime | None
    failure_code: str | None
    failure_message: str | None
    action: PaymentAction | None = None
    events: list[PaymentEventView] = Field(default_factory=list)
    version: int


class PaymentList(StrictRequest):
    items: list[PaymentView]


class FakePaymentWebhook(StrictRequest):
    provider_event_id: str = Field(min_length=5, max_length=128)
    payment_id: str = Field(min_length=5, max_length=32)
    provider_trade_no: str = Field(min_length=5, max_length=128)
    status: Literal["succeeded", "failed"]
    amount_minor_units: str = Field(pattern=r"^(0|[1-9][0-9]{0,18})$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    occurred_at: AwareDatetime
    failure_code: str | None = Field(default=None, max_length=64)


class PaymentWebhookAck(StrictRequest):
    accepted: bool = True
    duplicate: bool = False
    callback_id: str


class PaymentClosureResult(StrictRequest):
    payment: PaymentView


class AdminPaymentView(StrictRequest):
    payment: PaymentView
    user_id: str
    store_ids: list[str]
    provider_trade_no_masked: str | None
    available_admin_actions: list[Literal["reconcile"]]


class AdminPaymentList(StrictRequest):
    items: list[AdminPaymentView]


class AdminPaymentReconciliationRequest(StrictRequest):
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=2, max_length=500)


class AdminPaymentReconciliationResult(StrictRequest):
    payment: AdminPaymentView
    provider_status: Literal["pending", "succeeded", "failed", "closed"]
    result: Literal["no_change", "status_updated"]
    reconciled_at: datetime
