from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status
from fastapi.responses import StreamingResponse

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.batch_jobs.dependencies import BatchJobServiceDependency
from app.modules.batch_jobs.schemas import (
    BatchJobCancellationRequest,
    BatchJobConfirmationRequest,
    BatchJobItemList,
    BatchJobList,
    BatchJobView,
    ProductImportJobCreateRequest,
    ProductImportTemplateView,
)
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.rbac.dependencies import (
    AdminAccess,
    require_admin_permission,
    require_any_admin_permission,
)

router = APIRouter(prefix="/admin", tags=["batch-job-administration"])


@router.get(
    "/product-import-template",
    response_model=Envelope[ProductImportTemplateView],
    operation_id="AdminProductImportTemplate_Get",
)
async def product_import_template(
    response: Response,
    service: BatchJobServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("products:create")],
) -> Envelope[ProductImportTemplateView]:
    _no_store(response)
    return Envelope(data=service.product_import_template())


@router.get(
    "/product-import-template.csv",
    response_class=StreamingResponse,
    operation_id="AdminProductImportTemplate_Download",
)
async def download_product_import_template(
    service: BatchJobServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("products:create")],
) -> StreamingResponse:
    return StreamingResponse(
        iter([service.product_import_template_csv()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="product-import-v1.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.post(
    "/batch-jobs",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[BatchJobView],
    operation_id="AdminBatchJob_Create",
)
async def create_batch_job(
    payload: ProductImportJobCreateRequest,
    response: Response,
    service: BatchJobServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:create")],
) -> Envelope[BatchJobView]:
    item = await service.create_product_import(access, payload, idempotency_key)
    response.headers["ETag"] = _etag(item.version)
    response.headers["Cache-Control"] = "no-store"
    return Envelope(data=item)


@router.get(
    "/batch-jobs",
    response_model=Envelope[BatchJobList],
    operation_id="AdminBatchJob_List",
)
async def list_batch_jobs(
    response: Response,
    service: BatchJobServiceDependency,
    access: Annotated[
        AdminAccess,
        require_any_admin_permission("jobs:read", "knowledge:read"),
    ],
    job_type: Annotated[str | None, Query(max_length=32)] = None,
    job_status: Annotated[str | None, Query(alias="status", max_length=32)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[BatchJobList]:
    _no_store(response)
    return Envelope(
        data=await service.list_jobs(
            access, job_type=job_type, job_status=job_status, cursor=cursor, limit=limit
        )
    )


@router.get(
    "/batch-jobs/{job_id}",
    response_model=Envelope[BatchJobView],
    operation_id="AdminBatchJob_Get",
)
async def get_batch_job(
    job_id: str,
    response: Response,
    service: BatchJobServiceDependency,
    access: Annotated[
        AdminAccess,
        require_any_admin_permission("jobs:read", "products:create", "knowledge:read"),
    ],
) -> Envelope[BatchJobView]:
    item = await service.get_job(access, job_id)
    response.headers["ETag"] = _etag(item.version)
    response.headers["Cache-Control"] = "no-store"
    return Envelope(data=item)


@router.get(
    "/batch-jobs/{job_id}/items",
    response_model=Envelope[BatchJobItemList],
    operation_id="AdminBatchJobItem_List",
)
async def list_batch_job_items(
    job_id: str,
    response: Response,
    service: BatchJobServiceDependency,
    access: Annotated[
        AdminAccess,
        require_any_admin_permission("jobs:read", "products:create", "knowledge:read"),
    ],
    item_status: Annotated[str | None, Query(alias="status", max_length=16)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> Envelope[BatchJobItemList]:
    _no_store(response)
    return Envelope(
        data=await service.list_items(
            access, job_id, item_status=item_status, cursor=cursor, limit=limit
        )
    )


@router.post(
    "/batch-jobs/{job_id}/confirmations",
    response_model=Envelope[BatchJobView],
    operation_id="AdminBatchJob_Confirm",
)
async def confirm_batch_job(
    job_id: str,
    payload: BatchJobConfirmationRequest,
    response: Response,
    service: BatchJobServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:create")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[BatchJobView]:
    item = await service.confirm(
        access, job_id, payload, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(item.version)
    response.headers["Cache-Control"] = "no-store"
    return Envelope(data=item)


@router.post(
    "/batch-jobs/{job_id}/cancellations",
    response_model=Envelope[BatchJobView],
    operation_id="AdminBatchJob_Cancel",
)
async def cancel_batch_job(
    job_id: str,
    payload: BatchJobCancellationRequest,
    response: Response,
    service: BatchJobServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[
        AdminAccess,
        require_any_admin_permission("jobs:read", "products:create", "knowledge:manage"),
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[BatchJobView]:
    item = await service.cancel(
        access, job_id, payload, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(item.version)
    response.headers["Cache-Control"] = "no-store"
    return Envelope(data=item)
