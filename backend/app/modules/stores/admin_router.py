from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.finance.dependencies import AccountDeletionServiceDependency
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.rbac.dependencies import (
    AdminAccess,
    require_admin_permission,
    require_any_admin_permission,
)
from app.modules.stores.admin_schemas import (
    AdminCertificationDecisionRequest,
    AdminCertificationDetail,
    AdminCertificationEventView,
    AdminCertificationList,
    AdminCertificationMaterialRequest,
    AdminPolicyCommandRequest,
    AdminStoreCreateRequest,
    AdminStoreDeleteRequest,
    AdminStoreList,
    AdminStorePolicyCreateRequest,
    AdminStorePolicyUpdateRequest,
    AdminStorePolicyView,
    AdminStoreStatusChangeRequest,
    AdminStoreUpdateRequest,
    AdminStoreView,
)
from app.modules.stores.dependencies import AdminStoreServiceDependency

router = APIRouter(prefix="/admin", tags=["store-administration"])


@router.post(
    "/stores",
    response_model=Envelope[AdminStoreView],
    status_code=status.HTTP_201_CREATED,
    operation_id="AdminStore_Create",
)
async def create_store(
    payload: AdminStoreCreateRequest,
    response: Response,
    service: AdminStoreServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
) -> Envelope[AdminStoreView]:
    item = await service.create_store(access, payload, idempotency_key)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/stores",
    response_model=Envelope[AdminStoreList],
    operation_id="AdminStore_List",
)
async def list_stores(
    response: Response,
    service: AdminStoreServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:read")],
    store_status: Annotated[str | None, Query(alias="status", max_length=32)] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[AdminStoreList]:
    _no_store(response)
    return Envelope(
        data=await service.list_stores(
            access,
            status=store_status,
            q=q,
            cursor=cursor,
            limit=limit,
        )
    )


@router.get(
    "/stores/{store_id}",
    response_model=Envelope[AdminStoreView],
    operation_id="AdminStore_Get",
)
async def get_store(
    store_id: str,
    response: Response,
    service: AdminStoreServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:read")],
) -> Envelope[AdminStoreView]:
    item = await service.get_store(access, store_id)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.patch(
    "/stores/{store_id}",
    response_model=Envelope[AdminStoreView],
    operation_id="AdminStore_Update",
)
async def update_store(
    store_id: str,
    payload: AdminStoreUpdateRequest,
    response: Response,
    service: AdminStoreServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminStoreView]:
    item = await service.update_store(access, store_id, payload, _expected_version(if_match))
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/stores/{store_id}/status-changes",
    response_model=Envelope[AdminStoreView],
    operation_id="AdminStore_ChangeStatus",
)
async def change_store_status(
    store_id: str,
    payload: AdminStoreStatusChangeRequest,
    response: Response,
    service: AdminStoreServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminStoreView]:
    item = await service.change_store_status(
        access,
        store_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.delete(
    "/stores/{store_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="AdminStore_Delete",
)
async def delete_store(
    store_id: str,
    payload: AdminStoreDeleteRequest,
    response: Response,
    service: AdminStoreServiceDependency,
    deletion: AccountDeletionServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> None:
    owner = await service.prepare_store_deletion(
        access, store_id, payload, _expected_version(if_match)
    )
    await deletion.delete_merchant(owner)
    response.status_code = status.HTTP_204_NO_CONTENT
    _no_store(response)


@router.get(
    "/store-certifications",
    response_model=Envelope[AdminCertificationList],
    operation_id="AdminStoreCertification_List",
)
async def list_certifications(
    response: Response,
    service: AdminStoreServiceDependency,
    access: Annotated[
        AdminAccess,
        require_any_admin_permission("stores:review", "stores:read"),
    ],
    review_status: Annotated[str | None, Query(max_length=32)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[AdminCertificationList]:
    _no_store(response)
    return Envelope(
        data=await service.list_certifications(
            access,
            review_status=review_status,
            cursor=cursor,
            limit=limit,
        )
    )


@router.get(
    "/store-certifications/{certification_id}",
    response_model=Envelope[AdminCertificationDetail],
    operation_id="AdminStoreCertification_Get",
)
async def get_certification(
    certification_id: str,
    response: Response,
    service: AdminStoreServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:review")],
) -> Envelope[AdminCertificationDetail]:
    item = await service.get_certification(access, certification_id)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/store-certifications/{certification_id}/events",
    response_model=Envelope[list[AdminCertificationEventView]],
    operation_id="AdminStoreCertificationEvent_List",
)
async def list_certification_events(
    certification_id: str,
    response: Response,
    service: AdminStoreServiceDependency,
    access: Annotated[
        AdminAccess,
        require_any_admin_permission("stores:review", "stores:manage"),
    ],
) -> Envelope[list[AdminCertificationEventView]]:
    _no_store(response)
    return Envelope(data=await service.certification_events(access, certification_id))


@router.post(
    "/store-certifications/{certification_id}/decisions",
    response_model=Envelope[AdminCertificationDetail],
    operation_id="AdminStoreCertification_Decide",
)
async def decide_certification(
    certification_id: str,
    payload: AdminCertificationDecisionRequest,
    response: Response,
    service: AdminStoreServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("stores:review")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminCertificationDetail]:
    item = await service.decide_certification(
        access,
        certification_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/store-certifications/{certification_id}/material-versions",
    response_model=Envelope[AdminCertificationDetail],
    operation_id="AdminStoreCertification_AddMaterialVersion",
)
async def add_certification_material_version(
    certification_id: str,
    payload: AdminCertificationMaterialRequest,
    response: Response,
    service: AdminStoreServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminCertificationDetail]:
    item = await service.add_material_version(
        access,
        certification_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/stores/{store_id}/service-policies",
    response_model=Envelope[list[AdminStorePolicyView]],
    operation_id="AdminStorePolicy_List",
)
async def list_store_policies(
    store_id: str,
    response: Response,
    service: AdminStoreServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("store_policies:read")],
) -> Envelope[list[AdminStorePolicyView]]:
    _no_store(response)
    return Envelope(data=await service.list_policies(access, store_id))


@router.post(
    "/stores/{store_id}/service-policies",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AdminStorePolicyView],
    operation_id="AdminStorePolicy_Create",
)
async def create_store_policy(
    store_id: str,
    payload: AdminStorePolicyCreateRequest,
    response: Response,
    service: AdminStoreServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("store_policies:create")],
) -> Envelope[AdminStorePolicyView]:
    item = await service.create_policy(access, store_id, payload, idempotency_key)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/stores/{store_id}/service-policies/{policy_id}",
    response_model=Envelope[AdminStorePolicyView],
    operation_id="AdminStorePolicy_Get",
)
async def get_store_policy(
    store_id: str,
    policy_id: str,
    response: Response,
    service: AdminStoreServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("store_policies:read")],
) -> Envelope[AdminStorePolicyView]:
    item = await service.get_policy(access, store_id, policy_id)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.patch(
    "/stores/{store_id}/service-policies/{policy_id}",
    response_model=Envelope[AdminStorePolicyView],
    operation_id="AdminStorePolicy_Update",
)
async def update_store_policy(
    store_id: str,
    policy_id: str,
    payload: AdminStorePolicyUpdateRequest,
    response: Response,
    service: AdminStoreServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("store_policies:update")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminStorePolicyView]:
    item = await service.update_policy(
        access,
        store_id,
        policy_id,
        payload,
        _expected_version(if_match),
    )
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/stores/{store_id}/service-policies/{policy_id}/publications",
    response_model=Envelope[AdminStorePolicyView],
    operation_id="AdminStorePolicy_Publish",
)
async def publish_store_policy(
    store_id: str,
    policy_id: str,
    payload: AdminPolicyCommandRequest,
    response: Response,
    service: AdminStoreServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("store_policies:publish")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminStorePolicyView]:
    item = await service.publish_policy(
        access,
        store_id,
        policy_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/stores/{store_id}/service-policies/{policy_id}/withdrawals",
    response_model=Envelope[AdminStorePolicyView],
    operation_id="AdminStorePolicy_Withdraw",
)
async def withdraw_store_policy(
    store_id: str,
    policy_id: str,
    payload: AdminPolicyCommandRequest,
    response: Response,
    service: AdminStoreServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("store_policies:publish")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminStorePolicyView]:
    item = await service.withdraw_policy(
        access,
        store_id,
        policy_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)
