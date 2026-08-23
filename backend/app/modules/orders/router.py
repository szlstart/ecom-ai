from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.dependencies import IdempotencyKey, UserContext
from app.api.schemas import Envelope, ResponseMeta
from app.modules.identity.router import _etag, _no_store
from app.modules.orders.dependencies import OrderServiceDependency
from app.modules.orders.schemas import (
    OrderCreateRequest,
    OrderCreateResponse,
    OrderDetail,
    OrderEventList,
    OrderList,
    OrderView,
    TradeOrderView,
)

router = APIRouter(tags=["orders"])


@router.post(
    "/orders",
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


@router.get(
    "/users/me/orders",
    response_model=Envelope[OrderList],
    operation_id="Order_ListMine",
)
async def list_my_orders(
    response: Response,
    service: OrderServiceDependency,
    context: UserContext,
    view: OrderView = "all",
    q: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> Envelope[OrderList]:
    result, pagination = await service.list_mine(
        context.user,
        view=view,
        query=q,
        created_from=created_from,
        created_to=created_to,
        cursor=cursor,
        limit=limit,
    )
    _no_store(response)
    return Envelope(data=result, meta=ResponseMeta(pagination=pagination))


@router.get(
    "/orders/{order_id}",
    response_model=Envelope[OrderDetail],
    operation_id="Order_GetMine",
)
async def get_my_order(
    order_id: str,
    response: Response,
    service: OrderServiceDependency,
    context: UserContext,
) -> Envelope[OrderDetail]:
    item = await service.detail_mine(context.user, order_id)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/orders/{order_id}/events",
    response_model=Envelope[OrderEventList],
    operation_id="OrderEvent_ListMine",
)
async def list_my_order_events(
    order_id: str,
    response: Response,
    service: OrderServiceDependency,
    context: UserContext,
) -> Envelope[OrderEventList]:
    item = await service.events_mine(context.user, order_id)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/trade-orders/{trade_order_id}",
    response_model=Envelope[TradeOrderView],
    operation_id="TradeOrder_GetMine",
)
async def get_my_trade_order(
    trade_order_id: str,
    response: Response,
    service: OrderServiceDependency,
    context: UserContext,
) -> Envelope[TradeOrderView]:
    item = await service.trade_mine(context.user, trade_order_id)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)
