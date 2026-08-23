from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class PaymentProviderRequest:
    payment_no: str
    trade_order_no: str
    amount: int
    currency: str
    payment_method: str
    return_url_key: str


@dataclass(frozen=True)
class PaymentProviderAcceptance:
    provider_trade_no: str
    provider_request_id: str
    status: Literal["pending"]
    action_type: Literal["redirect"]
    action_url: str


class PaymentProvider(Protocol):
    async def create_payment(
        self, request: PaymentProviderRequest
    ) -> PaymentProviderAcceptance: ...

    async def close_payment(self, provider_trade_no: str) -> Literal["closed"]: ...


class FakePaymentProvider:
    """Deterministic local provider; it never marks a payment successful."""

    async def create_payment(self, request: PaymentProviderRequest) -> PaymentProviderAcceptance:
        return PaymentProviderAcceptance(
            provider_trade_no=f"fake_{request.payment_no}",
            provider_request_id=f"fake_request_{request.payment_no}",
            status="pending",
            action_type="redirect",
            action_url=f"/payments/{request.payment_no}/result",
        )

    async def close_payment(self, provider_trade_no: str) -> Literal["closed"]:
        if not provider_trade_no.startswith("fake_pay_"):
            raise ValueError("unknown fake provider trade")
        return "closed"


def payment_provider(provider: str) -> PaymentProvider:
    if provider == "fake":
        return FakePaymentProvider()
    raise ValueError(f"unsupported payment provider: {provider}")
