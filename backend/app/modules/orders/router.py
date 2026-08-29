from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import IdempotencyKey, UserContext
from app.api.schemas import Envelope, ResponseMeta
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.orders.dependencies import OrderServiceDependency
from app.modules.orders.schemas import (
    OrderCancellationRequest,
    OrderCommandResult,
    OrderCreateRequest,
    OrderCreateResponse,
    OrderDetail,
    OrderEventList,
    OrderHideResult,
    OrderList,
    OrderListItem,
    OrderRepurchaseResult,
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


@router.post(
    "/orders/{order_id}/cancellations",
    response_model=Envelope[OrderCommandResult],
    operation_id="Order_CancelMine",
)
async def cancel_my_order(
    order_id: str,
    payload: OrderCancellationRequest,
    response: Response,
    service: OrderServiceDependency,
    context: UserContext,
    idempotency_key: IdempotencyKey,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[OrderCommandResult]:
    item = await service.cancel(
        context.user,
        order_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.order.version)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/orders/{order_id}/receipt-confirmations",
    response_model=Envelope[OrderCommandResult],
    operation_id="Order_ConfirmReceiptMine",
)
async def confirm_my_order_receipt(
    order_id: str,
    response: Response,
    service: OrderServiceDependency,
    context: UserContext,
    idempotency_key: IdempotencyKey,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[OrderCommandResult]:
    item = await service.confirm_receipt(
        context.user, order_id, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(item.order.version)
    _no_store(response)
    return Envelope(data=item)


@router.delete(
    "/users/me/orders/{order_id}",
    response_model=Envelope[OrderHideResult],
    operation_id="Order_HideMine",
)
async def hide_my_order(
    order_id: str,
    response: Response,
    service: OrderServiceDependency,
    context: UserContext,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[OrderHideResult]:
    item = await service.hide(context.user, order_id, _expected_version(if_match))
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/users/me/orders/{order_id}/restorations",
    response_model=Envelope[OrderListItem],
    operation_id="Order_RestoreMine",
)
async def restore_my_order(
    order_id: str,
    response: Response,
    service: OrderServiceDependency,
    context: UserContext,
    idempotency_key: IdempotencyKey,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[OrderListItem]:
    item = await service.restore(
        context.user, order_id, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/orders/{order_id}/repurchases",
    response_model=Envelope[OrderRepurchaseResult],
    operation_id="Order_RepurchaseMine",
)
async def repurchase_my_order(
    order_id: str,
    response: Response,
    service: OrderServiceDependency,
    context: UserContext,
    idempotency_key: IdempotencyKey,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[OrderRepurchaseResult]:
    item = await service.repurchase(
        context.user, order_id, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(item.cart.version)
    _no_store(response)
    return Envelope(data=item)
