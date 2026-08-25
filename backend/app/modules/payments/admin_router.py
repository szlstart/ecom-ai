from typing import Annotated

from fastapi import APIRouter, Header, Query, Response

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.payments.dependencies import PaymentServiceDependency
from app.modules.payments.schemas import (
    AdminPaymentList,
    AdminPaymentReconciliationRequest,
    AdminPaymentReconciliationResult,
    AdminPaymentView,
)
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission

router = APIRouter(prefix="/admin/payments", tags=["payment-administration"])


@router.get("", response_model=Envelope[AdminPaymentList], operation_id="AdminPayment_List")
async def list_payments(
    response: Response,
    service: PaymentServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("payments:read")],
    q: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    payment_status: Annotated[str | None, Query(max_length=32)] = None,
    provider: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[AdminPaymentList]:
    result = await service.admin_list(
        access,
        query=q,
        payment_status=payment_status,
        provider=provider,
        limit=limit,
    )
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/{payment_id}",
    response_model=Envelope[AdminPaymentView],
    operation_id="AdminPayment_Get",
)
async def get_payment(
    payment_id: str,
    response: Response,
    service: PaymentServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("payments:read")],
) -> Envelope[AdminPaymentView]:
    result = await service.admin_detail(access, payment_id)
    response.headers["ETag"] = _etag(result.payment.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/{payment_id}/reconciliations",
    response_model=Envelope[AdminPaymentReconciliationResult],
    operation_id="AdminPayment_Reconcile",
)
async def reconcile_payment(
    payment_id: str,
    payload: AdminPaymentReconciliationRequest,
    response: Response,
    service: PaymentServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("payments:reconcile")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminPaymentReconciliationResult]:
    result = await service.admin_reconcile(
        access, payment_id, payload, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(result.payment.payment.version)
    _no_store(response)
    return Envelope(data=result)
