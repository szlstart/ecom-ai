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
    require_admin_permission_without_step_up,
)
from app.modules.stores.admin_schemas import (
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
    access: Annotated[AdminAccess, require_admin_permission_without_step_up("stores:manage")],
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
