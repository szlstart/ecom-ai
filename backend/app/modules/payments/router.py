from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status

from app.api.dependencies import IdempotencyKey, UserContext
from app.api.schemas import Envelope
from app.core.client_ip import request_client_ip
from app.core.exceptions import ApplicationError
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.payments.dependencies import PaymentServiceDependency
from app.modules.payments.schemas import (
    PaymentCreateRequest,
    PaymentList,
    PaymentView,
    PaymentWebhookAck,
)

router = APIRouter(tags=["payments"])


@router.post(
    "/payments",
    response_model=Envelope[PaymentView],
    status_code=status.HTTP_201_CREATED,
    operation_id="Payment_Create",
)
async def create_payment(
    payload: PaymentCreateRequest,
    request: Request,
    response: Response,
    context: UserContext,
    service: PaymentServiceDependency,
    idempotency_key: IdempotencyKey,
) -> Envelope[PaymentView]:
    client_ip = request_client_ip(request)
    item = await service.create(context.user, payload, idempotency_key, client_ip)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/payments/{payment_id}",
    response_model=Envelope[PaymentView],
    operation_id="Payment_GetMine",
)
async def get_payment(
    payment_id: str,
    response: Response,
    context: UserContext,
    service: PaymentServiceDependency,
) -> Envelope[PaymentView]:
    item = await service.get(context.user, payment_id)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/payments/{payment_id}/closures",
    response_model=Envelope[PaymentView],
    operation_id="Payment_CloseMine",
)
async def close_payment(
    payment_id: str,
    response: Response,
    context: UserContext,
    service: PaymentServiceDependency,
    idempotency_key: IdempotencyKey,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[PaymentView]:
    item = await service.close(
        context.user,
        payment_id,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/trade-orders/{trade_order_id}/payments",
    response_model=Envelope[PaymentList],
    operation_id="Payment_ListForTradeOrder",
)
async def list_trade_payments(
    trade_order_id: str,
    response: Response,
    context: UserContext,
    service: PaymentServiceDependency,
) -> Envelope[PaymentList]:
    item = await service.list_for_trade(context.user, trade_order_id)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/webhooks/payments/{provider}",
    response_model=Envelope[PaymentWebhookAck],
    operation_id="PaymentWebhook_Process",
)
async def process_payment_webhook(
    provider: str,
    request: Request,
    response: Response,
    service: PaymentServiceDependency,
    signature: Annotated[str, Header(alias="X-Payment-Signature")] = "",
    timestamp_header: Annotated[str, Header(alias="X-Payment-Timestamp")] = "",
) -> Envelope[PaymentWebhookAck]:
    raw_body = await request.body()
    if len(raw_body) > 65_536:
        raise ApplicationError(
            status=413,
            code="PAYMENT_WEBHOOK_TOO_LARGE",
            title="Payment webhook too large",
            detail="支付回调报文超过大小限制。",
        )
    item = await service.process_webhook(provider, raw_body, signature, timestamp_header)
    _no_store(response)
    return Envelope(data=item)
