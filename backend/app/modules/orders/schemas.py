from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.api.schemas import StrictRequest
from app.modules.cart.schemas import CartView
from app.modules.catalog.schemas import Money

OrderView = Literal[
    "all",
    "pending_payment",
    "pending_shipment",
    "in_transit",
    "completed",
    "pending_review",
    "after_sale",
    "cancelled",
]


class OrderCreateRequest(StrictRequest):
    checkout_id: str = Field(pattern=r"^chk_[0-9A-Z]+$", max_length=40)
    checkout_version: int = Field(ge=0)


class OrderCreateResponse(StrictRequest):
    trade_order_id: str
    order_ids: list[str]
    payment_deadline_at: datetime
    available_actions: list[str]
    version: int


class OrderActionTarget(StrictRequest):
    type: Literal["route"] = "route"
    name: str
    params: dict[str, str] = Field(default_factory=dict)


class OrderAction(StrictRequest):
    code: Literal[
        "pay",
        "cancel_order",
        "apply_after_sale",
        "view_after_sale",
        "view_logistics",
        "review",
        "delete_order",
        "confirm_receipt",
        "contact_store",
        "repurchase",
    ]
    enabled: bool
    reason_code: str | None = None
    reason_message: str | None = None
    requires_confirmation: bool = False
    target: OrderActionTarget


class OrderStoreView(StrictRequest):
    store_id: str
    store_name: str
    logo_url: str | None = None


class OrderItemView(StrictRequest):
    order_item_id: str
    product_id: str
    sku_id: str
    product_name: str
    sku_name: str
    spec_snapshot: list[dict[str, str]]
    image_url: str | None = None
    quantity: int
    unit_price: Money
    gross_amount: Money
    payable_amount: Money
    refunded_amount: Money
    refunded_quantity: int
    review_status: str
    after_sale_status: str


class SignedMoney(StrictRequest):
    minor_units: str = Field(pattern=r"^-?\d+$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class OrderAmountsView(StrictRequest):
    goods_amount: Money
    freight_amount: Money
    adjustment_amount: SignedMoney
    payable_amount: Money
    paid_amount: Money
    refunded_amount: Money


class OrderListItem(StrictRequest):
    order_id: str
    trade_order_id: str
    order_source: Literal["buy_now", "cart"]
    store: OrderStoreView
    order_status: str
    payment_status: str
    fulfillment_status: str
    after_sale_status: str
    matched_views: list[OrderView]
    items: list[OrderItemView]
    item_count: int
    total_quantity: int
    amounts: OrderAmountsView
    created_at: datetime
    expires_at: datetime
    available_actions: list[OrderAction]
    version: int


class OrderList(StrictRequest):
    items: list[OrderListItem]


class OrderAddressView(StrictRequest):
    recipient_name: str
    phone_masked: str
    country_code: str
    province_code: str
    city_code: str
    district_code: str
    address: str
    postal_code: str | None = None


class OrderEventView(StrictRequest):
    event_id: int
    state_dimension: str
    from_status: str | None = None
    to_status: str
    event_code: str
    actor_type: str
    reason: str | None = None
    order_version: int
    occurred_at: datetime


class OrderEventList(StrictRequest):
    items: list[OrderEventView]


class OrderDetail(OrderListItem):
    buyer_remark: str | None = None
    address: OrderAddressView
    policy_snapshot: dict[str, object]
    events: list[OrderEventView]


class TradeOrderView(StrictRequest):
    trade_order_id: str
    order_source: Literal["buy_now", "cart"]
    trade_status: str
    amounts: OrderAmountsView
    order_count: int
    orders: list[OrderListItem]
    created_at: datetime
    expires_at: datetime
    paid_at: datetime | None = None
    closed_at: datetime | None = None
    available_actions: list[OrderAction]
    version: int


class OrderCancellationRequest(StrictRequest):
    reason_code: Literal[
        "no_longer_needed",
        "wrong_product",
        "wrong_address",
        "price_changed",
        "other",
    ]
    description: str | None = Field(default=None, max_length=200)


class OrderCommandResult(StrictRequest):
    order: OrderListItem
    events: list[OrderEventView]


class OrderHideResult(StrictRequest):
    order_id: str
    undo_until: datetime
    restore_url: str
    version: int


class RepurchaseUnavailableItem(StrictRequest):
    order_item_id: str
    sku_id: str
    product_name: str
    reason_code: str
    reason_message: str


class OrderRepurchaseResult(StrictRequest):
    order_id: str
    added_items: list[str]
    unavailable_items: list[RepurchaseUnavailableItem]
    requires_reselection: bool
    cart: CartView


class AdminOrderSummary(StrictRequest):
    order: OrderListItem
    user_id: str
    user_name_masked: str
    available_admin_actions: list[Literal["adjust_amount", "cancel", "create_shipment"]]


class AdminOrderList(StrictRequest):
    items: list[AdminOrderSummary]


class AdminOrderDetail(AdminOrderSummary):
    events: list[OrderEventView]


class AdminOrderAmountAdjustmentRequest(StrictRequest):
    adjustment_amount: SignedMoney
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=2, max_length=500)


class AdminOrderCancellationRequest(StrictRequest):
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=2, max_length=500)
