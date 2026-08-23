from fastapi import APIRouter, Request, Response, status

from app.api.dependencies import IdempotencyKey, UserContext
from app.api.schemas import Envelope
from app.modules.identity.router import _etag, _no_store
from app.modules.payments.dependencies import PaymentServiceDependency
from app.modules.payments.schemas import PaymentCreateRequest, PaymentList, PaymentView

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
    client_ip = request.client.host if request.client else "unknown"
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
