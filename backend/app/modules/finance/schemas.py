from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.api.schemas import StrictRequest
from app.modules.catalog.schemas import Money


class WalletView(StrictRequest):
    wallet_id: str
    balance: Money
    total_recharged: Money
    wallet_status: Literal["active", "frozen"]
    version: int


class WalletRechargeRequest(StrictRequest):
    channel: Literal["wechat", "alipay"]
    amount: Money

    @model_validator(mode="after")
    def validate_recharge(self) -> "WalletRechargeRequest":
        if self.amount.currency != "CNY":
            raise ValueError("only CNY recharge is supported")
        amount = int(self.amount.minor_units)
        if not 100 <= amount <= 5_000_000:
            raise ValueError("recharge amount must be between CNY 1.00 and CNY 50,000.00")
        return self


class WalletRechargeView(StrictRequest):
    recharge_id: str
    channel: Literal["wechat", "alipay"]
    amount: Money
    recharge_status: Literal["succeeded"]
    is_simulated: bool
    completed_at: datetime


class WalletRechargeResult(StrictRequest):
    recharge: WalletRechargeView
    wallet: WalletView


class WalletTransactionView(StrictRequest):
    transaction_id: str
    transaction_type: str
    direction: Literal["credit", "debit"]
    amount: Money
    balance_after: Money
    channel: str | None
    description: str
    occurred_at: datetime


class WalletTransactionList(StrictRequest):
    items: list[WalletTransactionView] = Field(max_length=100)


class MerchantRevenueView(StrictRequest):
    store_id: str
    gross_sales: Money
    refunded_amount: Money
    net_revenue: Money
    today_revenue: Money
    yesterday_revenue: Money
    last_30_days_revenue: Money
    all_order_count: int
    completed_order_count: int
    pending_payment_count: int
    pending_shipment_count: int
    in_transit_count: int
    after_sale_pending_count: int
    cancelled_count: int


class AdminStoreRevenueView(MerchantRevenueView):
    product_count: int


class AccountDeletionRequest(StrictRequest):
    confirmation: Literal["DELETE_MY_ACCOUNT"]


class MerchantAccountDeletionRequest(StrictRequest):
    confirmation: Literal["DELETE_MY_STORE_AND_ACCOUNT"]
