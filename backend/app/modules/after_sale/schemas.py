from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.api.schemas import StrictRequest
from app.modules.catalog.schemas import Money
from app.modules.rbac.schemas import ApprovalRequiredView

RefundType = Literal["refund_only", "return_and_refund"]
RefundEligibilityAction = Literal["apply_after_sale", "view_active_after_sale"]
RefundApplicationAction = Literal[
    "cancel", "view_events", "create_refund_appeal", "create_new_refund_application"
]
RefundStatus = Literal[
    "submitted",
    "merchant_review",
    "approved",
    "waiting_return",
    "returning",
    "received",
    "refunding",
    "succeeded",
    "rejected",
    "cancelled",
    "closed",
]
RefundShipmentStatus = Literal["submitted", "in_transit", "delivered", "received", "exception"]
RefundPaymentStatus = Literal["pending", "succeeded", "failed", "unknown"]
RefundAppealStatus = Literal["submitted", "reviewing", "upheld", "rejected", "cancelled", "closed"]


class RefundEligibilityItem(StrictRequest):
    order_item_id: str
    purchased_quantity: int
    succeeded_refund_quantity: int
    active_reserved_quantity: int
    available_quantity: int
    available_refundable_amount: Money
    available_actions: list[RefundEligibilityAction]


class RefundEligibilityItemRequest(StrictRequest):
    order_item_id: str = Field(pattern=r"^oit_[0-9A-Z]+$", max_length=40)
    quantity: int = Field(gt=0)


class RefundEligibilityRequest(StrictRequest):
    order_id: str = Field(min_length=5, max_length=40)
    items: list[RefundEligibilityItemRequest] = Field(min_length=1, max_length=50)
    requested_type: RefundType
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")

    @model_validator(mode="after")
    def reject_duplicate_items(self) -> RefundEligibilityRequest:
        item_ids = [item.order_item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("refund order item ids must be unique")
        return self


class RefundEligibilityCheck(StrictRequest):
    eligible: bool
    eligibility_token: str | None = None
    expires_at: datetime
    allowed_types: list[RefundType]
    items: list[RefundEligibilityItem]
    amount_editable: bool = False
    min_refundable_amount: Money
    max_refundable_amount: Money
    suggested_refund_amount: Money
    blocking_reasons: list[str] = Field(default_factory=list)


class RefundApplicationItemRequest(StrictRequest):
    order_item_id: str = Field(pattern=r"^oit_[0-9A-Z]+$", max_length=40)
    quantity: int = Field(gt=0)


class RefundApplicationCreateRequest(StrictRequest):
    eligibility_token: str = Field(min_length=16, max_length=2048)
    items: list[RefundApplicationItemRequest] = Field(min_length=1, max_length=50)
    refund_type: RefundType
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason_detail: str | None = Field(default=None, max_length=500)
    requested_amount: Money
    policy_accepted: bool

    @model_validator(mode="after")
    def require_policy(self) -> RefundApplicationCreateRequest:
        if not self.policy_accepted:
            raise ValueError("policy must be accepted")
        item_ids = [item.order_item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("refund order item ids must be unique")
        return self


class RefundApplicationItemView(StrictRequest):
    order_item_id: str
    quantity: int
    requested_amount: Money


class RefundApplicationView(StrictRequest):
    refund_id: str
    order_id: str
    refund_type: RefundType
    refund_status: RefundStatus
    reason_code: str
    reason_detail: str | None
    requested_amount: Money
    approved_amount: Money
    items: list[RefundApplicationItemView]
    available_actions: list[RefundApplicationAction]
    submitted_at: datetime
    decided_at: datetime | None
    version: int
    claimed: bool = False


class RefundApplicationList(StrictRequest):
    items: list[RefundApplicationView]


class AdminRefundList(StrictRequest):
    items: list[RefundApplicationView]
    next_cursor: str | None = None


class RefundEventView(StrictRequest):
    event_id: str
    from_status: str | None
    to_status: str
    event_code: str
    occurred_at: datetime


class RefundEventList(StrictRequest):
    items: list[RefundEventView]


class RefundCancelRequest(StrictRequest):
    reason: str | None = Field(default=None, max_length=200)


class AdminRefundDecisionRequest(StrictRequest):
    decision: Literal["approve", "reject"]
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=2, max_length=500)
    approved_amount: Money | None = None


class RefundReturnShipmentRequest(StrictRequest):
    carrier_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    tracking_no: str = Field(
        min_length=6,
        max_length=64,
        json_schema_extra={"writeOnly": True},
    )


class RefundReturnShipmentView(StrictRequest):
    refund_id: str
    carrier_code: str
    carrier_name: str
    tracking_no_masked: str
    shipment_status: RefundShipmentStatus
    version: int


class RefundPaymentCallbackAck(StrictRequest):
    accepted: bool
    duplicate: bool = False
    status: RefundPaymentStatus


class FakeRefundWebhook(StrictRequest):
    provider_event_id: str = Field(min_length=1, max_length=128)
    refund_payment_no: str = Field(pattern=r"^rfp_[0-9A-Z]+$", max_length=40)
    status: Literal["pending", "succeeded", "failed", "unknown"]
    amount_minor_units: str = Field(pattern=r"^[0-9]+$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class RefundAppealCreateRequest(StrictRequest):
    reason: str = Field(min_length=2, max_length=1000)


class RefundAppealView(StrictRequest):
    appeal_id: str
    refund_id: str
    appeal_status: RefundAppealStatus
    reason: str
    submitted_at: datetime
    decided_at: datetime | None
    version: int
    claimed: bool = False


class RefundAppealEventView(StrictRequest):
    event_id: str
    event_type: str
    from_status: str | None
    to_status: str
    actor_type: str
    reason_code: str | None
    remark: str | None
    appeal_version: int
    occurred_at: datetime


class RefundAppealEventList(StrictRequest):
    items: list[RefundAppealEventView]


class RefundClaimView(StrictRequest):
    refund_id: str
    version: int
    claimed: bool = True


class RefundAppealClaimView(StrictRequest):
    appeal_id: str
    version: int
    claimed: bool = True


class AdminRefundAppealDecisionRequest(StrictRequest):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=2, max_length=500)


class AdminRefundAppealList(StrictRequest):
    items: list[RefundAppealView]


AdminRefundDecisionResult = RefundApplicationView | ApprovalRequiredView
AdminRefundAppealDecisionResult = RefundAppealView | ApprovalRequiredView
