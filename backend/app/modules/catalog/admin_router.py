from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.catalog.admin_schemas import (
    AdminBrandCreateRequest,
    AdminBrandUpdateRequest,
    AdminBrandView,
    AdminCategoryCreateRequest,
    AdminCategoryUpdateRequest,
    AdminCategoryView,
    AdminInventoryAdjustmentRequest,
    AdminInventoryAdjustmentView,
    AdminInventoryList,
    AdminInventoryView,
)
from app.modules.catalog.dependencies import AdminCatalogServiceDependency
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission

router = APIRouter(prefix="/admin", tags=["catalog-administration"])


@router.get(
    "/categories",
    response_model=Envelope[list[AdminCategoryView]],
    operation_id="AdminCategory_List",
)
async def list_categories(
    response: Response,
    service: AdminCatalogServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("catalog_taxonomy:manage")],
) -> Envelope[list[AdminCategoryView]]:
    _no_store(response)
    return Envelope(data=await service.list_categories(access))


@router.post(
    "/categories",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AdminCategoryView],
    operation_id="AdminCategory_Upsert",
)
async def create_category(
    payload: AdminCategoryCreateRequest,
    response: Response,
    service: AdminCatalogServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("catalog_taxonomy:manage")],
) -> Envelope[AdminCategoryView]:
    item = await service.create_category(access, payload, idempotency_key)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.patch(
    "/categories/{category_id}",
    response_model=Envelope[AdminCategoryView],
    operation_id="AdminCategory_Update",
)
async def update_category(
    category_id: str,
    payload: AdminCategoryUpdateRequest,
    response: Response,
    service: AdminCatalogServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("catalog_taxonomy:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminCategoryView]:
    item = await service.update_category(access, category_id, payload, _expected_version(if_match))
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/brands",
    response_model=Envelope[list[AdminBrandView]],
    operation_id="AdminBrand_List",
)
async def list_brands(
    response: Response,
    service: AdminCatalogServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("catalog_taxonomy:manage")],
) -> Envelope[list[AdminBrandView]]:
    _no_store(response)
    return Envelope(data=await service.list_brands(access))


@router.post(
    "/brands",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AdminBrandView],
    operation_id="AdminBrand_Upsert",
)
async def create_brand(
    payload: AdminBrandCreateRequest,
    response: Response,
    service: AdminCatalogServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("catalog_taxonomy:manage")],
) -> Envelope[AdminBrandView]:
    item = await service.create_brand(access, payload, idempotency_key)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.patch(
    "/brands/{brand_id}",
    response_model=Envelope[AdminBrandView],
    operation_id="AdminBrand_Update",
)
async def update_brand(
    brand_id: str,
    payload: AdminBrandUpdateRequest,
    response: Response,
    service: AdminCatalogServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("catalog_taxonomy:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminBrandView]:
    item = await service.update_brand(access, brand_id, payload, _expected_version(if_match))
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/inventories",
    response_model=Envelope[AdminInventoryList],
    operation_id="AdminInventory_List",
)
async def list_inventories(
    response: Response,
    service: AdminCatalogServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("inventories:read")],
    store_id: Annotated[str | None, Query(min_length=5, max_length=40)] = None,
    product_id: Annotated[str | None, Query(min_length=5, max_length=40)] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[AdminInventoryList]:
    _no_store(response)
    return Envelope(
        data=await service.list_inventories(
            access, store_no=store_id, product_no=product_id, q=q, limit=limit
        )
    )


@router.get(
    "/inventories/{sku_id}",
    response_model=Envelope[AdminInventoryView],
    operation_id="AdminInventory_Get",
)
async def get_inventory(
    sku_id: str,
    response: Response,
    service: AdminCatalogServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("inventories:read")],
) -> Envelope[AdminInventoryView]:
    item = await service.get_inventory(access, sku_id)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/inventory-adjustments",
    response_model=Envelope[AdminInventoryAdjustmentView],
    operation_id="AdminInventory_Adjust",
)
async def adjust_inventory(
    payload: AdminInventoryAdjustmentRequest,
    response: Response,
    service: AdminCatalogServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("inventories:adjust")],
) -> Envelope[AdminInventoryAdjustmentView]:
    item = await service.adjust_inventory(access, payload, idempotency_key)
    response.headers["ETag"] = _etag(item.inventory.version)
    _no_store(response)
    return Envelope(data=item)
