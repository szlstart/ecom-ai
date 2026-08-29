from typing import Annotated

from fastapi import APIRouter, Header, Query, Response

from app.api.schemas import Envelope
from app.modules.after_sale.dependencies import AfterSaleServiceDependency
from app.modules.after_sale.schemas import (
    AdminRefundAppealDecisionRequest,
    AdminRefundAppealDecisionResult,
    AdminRefundAppealList,
    AdminRefundDecisionRequest,
    AdminRefundDecisionResult,
    AdminRefundList,
    RefundAppealView,
    RefundApplicationView,
)
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission
from app.modules.rbac.schemas import ApprovalRequiredView

router = APIRouter(prefix="/admin", tags=["after-sale-administration"])


@router.get(
    "/refund-applications",
    response_model=Envelope[AdminRefundList],
    operation_id="AdminRefund_List",
)
async def list_refunds(
    response: Response,
    service: AfterSaleServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("refunds:read")],
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[AdminRefundList]:
    result = await service.admin_list(access, limit, cursor)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/refund-appeals",
    response_model=Envelope[AdminRefundAppealList],
    operation_id="AdminRefundAppeal_List",
)
async def list_appeals(
    response: Response,
    service: AfterSaleServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("refund_appeals:read")],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[AdminRefundAppealList]:
    result = await service.admin_appeal_list(access, limit)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/refund-appeals/{appeal_id}",
    response_model=Envelope[RefundAppealView],
    operation_id="AdminRefundAppeal_Get",
)
async def get_appeal(
    appeal_id: str,
    response: Response,
    service: AfterSaleServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("refund_appeals:read")],
) -> Envelope[RefundAppealView]:
    result = await service.admin_appeal_detail(access, appeal_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/refund-appeals/{appeal_id}/claims",
    response_model=Envelope[RefundAppealView],
    operation_id="AdminRefundAppeal_Claim",
)
async def claim_appeal(
    appeal_id: str,
    response: Response,
    service: AfterSaleServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("refund_appeals:review")],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[RefundAppealView]:
    result = await service.claim_appeal(
        access,
        appeal_id,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/refund-appeals/{appeal_id}/decisions",
    response_model=Envelope[AdminRefundAppealDecisionResult],
    responses={
        202: {
            "model": Envelope[ApprovalRequiredView],
            "description": "Dual-control approval required",
        }
    },
    operation_id="AdminRefundAppeal_Decide",
)
async def decide_appeal(
    appeal_id: str,
    payload: AdminRefundAppealDecisionRequest,
    response: Response,
    service: AfterSaleServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("refund_appeals:review")],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminRefundAppealDecisionResult]:
    result = await service.request_appeal_decision(
        access,
        appeal_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    if isinstance(result, ApprovalRequiredView):
        response.status_code = 202
    else:
        response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/refund-applications/{refund_id}",
    response_model=Envelope[RefundApplicationView],
    operation_id="AdminRefund_Get",
)
async def get_refund(
    refund_id: str,
    response: Response,
    service: AfterSaleServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("refunds:read")],
) -> Envelope[RefundApplicationView]:
    result = await service.admin_detail(access, refund_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/refund-applications/{refund_id}/claims",
    response_model=Envelope[RefundApplicationView],
    operation_id="AdminRefund_Claim",
)
async def claim_refund(
    refund_id: str,
    response: Response,
    service: AfterSaleServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("refunds:review")],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[RefundApplicationView]:
    result = await service.claim_refund(
        access,
        refund_id,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/refund-applications/{refund_id}/decisions",
    response_model=Envelope[AdminRefundDecisionResult],
    responses={
        202: {
            "model": Envelope[ApprovalRequiredView],
            "description": "Amount-based approval required",
        }
    },
    operation_id="AdminRefund_Decide",
)
async def decide_refund(
    refund_id: str,
    payload: AdminRefundDecisionRequest,
    response: Response,
    service: AfterSaleServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("refunds:review")],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminRefundDecisionResult]:
    result = await service.request_refund_decision(
        access,
        refund_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    if isinstance(result, ApprovalRequiredView):
        response.status_code = 202
    else:
        response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)
