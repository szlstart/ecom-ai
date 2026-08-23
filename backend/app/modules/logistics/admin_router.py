from typing import Annotated

from fastapi import APIRouter, Header, Response, status

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.logistics.dependencies import LogisticsServiceDependency
from app.modules.logistics.schemas import (
    AdminShipmentCreateRequest,
    AdminShipmentDetail,
    AdminShipmentVoidRequest,
    AdminTrackingCorrectionRequest,
)
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission

router = APIRouter(prefix="/admin", tags=["logistics-administration"])


@router.post(
    "/orders/{order_id}/shipments",
    response_model=Envelope[AdminShipmentDetail],
    status_code=status.HTTP_201_CREATED,
    operation_id="AdminShipment_Create",
)
async def create_shipment(
    order_id: str,
    payload: AdminShipmentCreateRequest,
    response: Response,
    service: LogisticsServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("shipments:create")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminShipmentDetail]:
    result = await service.create_shipment(
        access,
        order_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/shipments/{shipment_id}",
    response_model=Envelope[AdminShipmentDetail],
    operation_id="AdminShipment_Get",
)
async def get_shipment(
    shipment_id: str,
    response: Response,
    service: LogisticsServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("shipments:read")],
) -> Envelope[AdminShipmentDetail]:
    result = await service.admin_detail(access, shipment_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/shipments/{shipment_id}/tracking-corrections",
    response_model=Envelope[AdminShipmentDetail],
    operation_id="AdminShipment_CorrectTracking",
)
async def correct_tracking(
    shipment_id: str,
    payload: AdminTrackingCorrectionRequest,
    response: Response,
    service: LogisticsServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("shipments:correct")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminShipmentDetail]:
    result = await service.correct_tracking(
        access,
        shipment_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/shipments/{shipment_id}/voids",
    response_model=Envelope[AdminShipmentDetail],
    operation_id="AdminShipment_Void",
)
async def void_shipment(
    shipment_id: str,
    payload: AdminShipmentVoidRequest,
    response: Response,
    service: LogisticsServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("shipments:void")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminShipmentDetail]:
    result = await service.void_shipment(
        access,
        shipment_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)
