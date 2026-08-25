from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status

from app.api.dependencies import IdempotencyKey, UserContext
from app.api.schemas import Envelope, ResponseMeta
from app.modules.after_sale.dependencies import AfterSaleServiceDependency
from app.modules.after_sale.schemas import (
    RefundAppealCreateRequest,
    RefundAppealEventList,
    RefundAppealView,
    RefundApplicationCreateRequest,
    RefundApplicationList,
    RefundApplicationView,
    RefundCancelRequest,
    RefundEligibilityCheck,
    RefundEligibilityRequest,
    RefundEventList,
    RefundPaymentCallbackAck,
    RefundReturnShipmentRequest,
    RefundReturnShipmentView,
)
from app.modules.identity.router import _etag, _expected_version, _no_store

router = APIRouter(tags=["after-sale"])


@router.put(
    "/refund-applications/{refund_id}/return-shipment",
    response_model=Envelope[RefundReturnShipmentView],
    operation_id="RefundReturnShipment_Upsert",
)
async def upsert_return_shipment(
    refund_id: str,
    payload: RefundReturnShipmentRequest,
    response: Response,
    context: UserContext,
    service: AfterSaleServiceDependency,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[RefundReturnShipmentView]:
    result = await service.upsert_return_shipment(
        context.user, refund_id, payload, _expected_version(if_match)
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/webhooks/refunds/{provider}",
    response_model=Envelope[RefundPaymentCallbackAck],
    operation_id="RefundPaymentWebhook_Process",
)
async def process_refund_webhook(
    provider: str,
    request: Request,
    response: Response,
    service: AfterSaleServiceDependency,
    signature: Annotated[str, Header(alias="X-Refund-Signature")] = "",
    timestamp_header: Annotated[str, Header(alias="X-Refund-Timestamp")] = "",
) -> Envelope[RefundPaymentCallbackAck]:
    result = await service.process_refund_webhook(
        provider, await request.body(), signature, timestamp_header
    )
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/refund-eligibility-checks",
    response_model=Envelope[RefundEligibilityCheck],
    operation_id="RefundEligibility_Check",
)
async def check_refund_eligibility(
    payload: RefundEligibilityRequest,
    response: Response,
    context: UserContext,
    service: AfterSaleServiceDependency,
) -> Envelope[RefundEligibilityCheck]:
    result = await service.eligibility(context.user, payload)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/refund-applications",
    response_model=Envelope[RefundApplicationView],
    status_code=status.HTTP_201_CREATED,
    operation_id="RefundApplication_Create",
)
async def create_refund(
    payload: RefundApplicationCreateRequest,
    response: Response,
    context: UserContext,
    service: AfterSaleServiceDependency,
    idempotency_key: IdempotencyKey,
) -> Envelope[RefundApplicationView]:
    result = await service.create(context.user, payload, idempotency_key)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/users/me/refund-applications",
    response_model=Envelope[RefundApplicationList],
    operation_id="RefundApplication_ListMine",
)
async def list_refunds(
    response: Response,
    context: UserContext,
    service: AfterSaleServiceDependency,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> Envelope[RefundApplicationList]:
    data, pagination = await service.list_mine(context.user, limit)
    _no_store(response)
    return Envelope(data=data, meta=ResponseMeta(pagination=pagination))


@router.get(
    "/refund-applications/{refund_id}",
    response_model=Envelope[RefundApplicationView],
    operation_id="RefundApplication_GetMine",
)
async def get_refund(
    refund_id: str, response: Response, context: UserContext, service: AfterSaleServiceDependency
) -> Envelope[RefundApplicationView]:
    result = await service.detail(context.user, refund_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/refund-applications/{refund_id}/events",
    response_model=Envelope[RefundEventList],
    operation_id="RefundEvent_ListMine",
)
async def list_refund_events(
    refund_id: str, response: Response, context: UserContext, service: AfterSaleServiceDependency
) -> Envelope[RefundEventList]:
    result = await service.events(context.user, refund_id)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/refund-applications/{refund_id}/cancellations",
    response_model=Envelope[RefundApplicationView],
    operation_id="RefundApplication_Cancel",
)
async def cancel_refund(
    refund_id: str,
    _payload: RefundCancelRequest,
    response: Response,
    context: UserContext,
    service: AfterSaleServiceDependency,
    idempotency_key: IdempotencyKey,
) -> Envelope[RefundApplicationView]:
    result = await service.cancel(context.user, refund_id, idempotency_key)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/refund-applications/{refund_id}/appeals",
    response_model=Envelope[RefundAppealView],
    status_code=status.HTTP_201_CREATED,
    operation_id="RefundAppeal_Create",
)
async def create_refund_appeal(
    refund_id: str,
    payload: RefundAppealCreateRequest,
    response: Response,
    context: UserContext,
    service: AfterSaleServiceDependency,
    idempotency_key: IdempotencyKey,
) -> Envelope[RefundAppealView]:
    result = await service.create_appeal(
        context.user,
        refund_id,
        payload,
        idempotency_key,
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/refund-appeals/{appeal_id}",
    response_model=Envelope[RefundAppealView],
    operation_id="RefundAppeal_GetMine",
)
async def get_refund_appeal(
    appeal_id: str,
    response: Response,
    context: UserContext,
    service: AfterSaleServiceDependency,
) -> Envelope[RefundAppealView]:
    result = await service.appeal_detail(context.user, appeal_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/refund-appeals/{appeal_id}/events",
    response_model=Envelope[RefundAppealEventList],
    operation_id="RefundAppealEvent_ListMine",
)
async def list_refund_appeal_events(
    appeal_id: str,
    response: Response,
    context: UserContext,
    service: AfterSaleServiceDependency,
) -> Envelope[RefundAppealEventList]:
    result = await service.appeal_events(context.user, appeal_id)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/refund-appeals/{appeal_id}/cancellations",
    response_model=Envelope[RefundAppealView],
    operation_id="RefundAppeal_Cancel",
)
async def cancel_refund_appeal(
    appeal_id: str,
    response: Response,
    context: UserContext,
    service: AfterSaleServiceDependency,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[RefundAppealView]:
    result = await service.cancel_appeal(
        context.user,
        appeal_id,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)
