from fastapi import APIRouter, Response, status

from app.api.dependencies import IdempotencyKey, UserContext
from app.api.schemas import Envelope
from app.modules.identity.router import _etag, _no_store
from app.modules.orders.dependencies import OrderServiceDependency
from app.modules.orders.schemas import OrderCreateRequest, OrderCreateResponse

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "",
    response_model=Envelope[OrderCreateResponse],
    status_code=status.HTTP_201_CREATED,
    operation_id="Order_Create",
)
async def create_order(
    payload: OrderCreateRequest,
    response: Response,
    service: OrderServiceDependency,
    context: UserContext,
    idempotency_key: IdempotencyKey,
) -> Envelope[OrderCreateResponse]:
    item = await service.create(context.user, payload, idempotency_key)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)
