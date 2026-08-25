from typing import Annotated

from fastapi import APIRouter, Header, Query, Response

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.orders.dependencies import OrderServiceDependency
from app.modules.orders.schemas import (
    AdminOrderAmountAdjustmentRequest,
    AdminOrderCancellationRequest,
    AdminOrderDetail,
    AdminOrderList,
)
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission

router = APIRouter(prefix="/admin/orders", tags=["order-administration"])


@router.get("", response_model=Envelope[AdminOrderList], operation_id="AdminOrder_List")
async def list_orders(
    response: Response,
    service: OrderServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("orders:read")],
    q: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    order_status: Annotated[str | None, Query(max_length=32)] = None,
    payment_status: Annotated[str | None, Query(max_length=32)] = None,
    fulfillment_status: Annotated[str | None, Query(max_length=32)] = None,
    after_sale_status: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[AdminOrderList]:
    result = await service.admin_list(
        access,
        query=q,
        order_status=order_status,
        payment_status=payment_status,
        fulfillment_status=fulfillment_status,
        after_sale_status=after_sale_status,
        limit=limit,
    )
    _no_store(response)
    return Envelope(data=result)


@router.get("/{order_id}", response_model=Envelope[AdminOrderDetail], operation_id="AdminOrder_Get")
async def get_order(
    order_id: str,
    response: Response,
    service: OrderServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("orders:read")],
) -> Envelope[AdminOrderDetail]:
    result = await service.admin_detail(access, order_id)
    response.headers["ETag"] = _etag(result.order.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/{order_id}/amount-adjustments",
    response_model=Envelope[AdminOrderDetail],
    operation_id="AdminOrder_AdjustAmount",
)
async def adjust_order_amount(
    order_id: str,
    payload: AdminOrderAmountAdjustmentRequest,
    response: Response,
    service: OrderServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("orders:adjust")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminOrderDetail]:
    result = await service.admin_adjust_amount(
        access, order_id, payload, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(result.order.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/{order_id}/cancellations",
    response_model=Envelope[AdminOrderDetail],
    operation_id="AdminOrder_Cancel",
)
async def cancel_order(
    order_id: str,
    payload: AdminOrderCancellationRequest,
    response: Response,
    service: OrderServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("orders:cancel")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminOrderDetail]:
    result = await service.admin_cancel(
        access, order_id, payload, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(result.order.version)
    _no_store(response)
    return Envelope(data=result)
