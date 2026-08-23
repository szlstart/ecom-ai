from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.dependencies import IdempotencyKey, UserContext
from app.api.schemas import Envelope, ResponseMeta
from app.modules.identity.router import _etag, _no_store
from app.modules.logistics.dependencies import LogisticsServiceDependency
from app.modules.logistics.schemas import (
    ShipmentRefreshResult,
    ShipmentTrackList,
    UserOrderShipmentList,
    UserShipmentDetail,
)

router = APIRouter(tags=["logistics"])


@router.get(
    "/orders/{order_id}/shipments",
    response_model=Envelope[UserOrderShipmentList],
    operation_id="Shipment_ListMine",
)
async def list_my_order_shipments(
    order_id: str,
    response: Response,
    service: LogisticsServiceDependency,
    context: UserContext,
) -> Envelope[UserOrderShipmentList]:
    result = await service.list_for_order(context.user, order_id)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/shipments/{shipment_id}",
    response_model=Envelope[UserShipmentDetail],
    operation_id="Shipment_GetMine",
)
async def get_my_shipment(
    shipment_id: str,
    response: Response,
    service: LogisticsServiceDependency,
    context: UserContext,
) -> Envelope[UserShipmentDetail]:
    result = await service.detail(context.user, shipment_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/shipments/{shipment_id}/tracks",
    response_model=Envelope[ShipmentTrackList],
    operation_id="ShipmentTrack_ListMine",
)
async def list_my_shipment_tracks(
    shipment_id: str,
    response: Response,
    service: LogisticsServiceDependency,
    context: UserContext,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Envelope[ShipmentTrackList]:
    result, pagination = await service.tracks(context.user, shipment_id, cursor=cursor, limit=limit)
    _no_store(response)
    return Envelope(data=result, meta=ResponseMeta(pagination=pagination))


@router.post(
    "/shipments/{shipment_id}/refreshes",
    response_model=Envelope[ShipmentRefreshResult],
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="ShipmentRefresh_Create",
)
async def refresh_my_shipment(
    shipment_id: str,
    response: Response,
    service: LogisticsServiceDependency,
    context: UserContext,
    idempotency_key: IdempotencyKey,
) -> Envelope[ShipmentRefreshResult]:
    result = await service.request_refresh(context.user, shipment_id, idempotency_key)
    _no_store(response)
    return Envelope(data=result)
