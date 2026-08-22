from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission
from app.modules.stores.dependencies import StoreOperationsServiceDependency
from app.modules.stores.operations_schemas import (
    AdminFeaturedProductSetRequest,
    AdminFeaturedProductView,
    AdminShippingTemplateCreateRequest,
    AdminShippingTemplatePublicationRequest,
    AdminShippingTemplateUpdateRequest,
    AdminShippingTemplateView,
    AdminStoreAnnouncementCreateRequest,
    AdminStoreAnnouncementUpdateRequest,
    AdminStoreAnnouncementView,
    AdminStoreProductGroupCreateRequest,
    AdminStoreProductGroupProductsRequest,
    AdminStoreProductGroupUpdateRequest,
    AdminStoreProductGroupView,
)

router = APIRouter(prefix="/admin/stores/{store_id}", tags=["store-operations"])


@router.get(
    "/product-groups",
    response_model=Envelope[list[AdminStoreProductGroupView]],
    operation_id="AdminStoreProductGroup_List",
)
async def list_product_groups(
    store_id: str,
    response: Response,
    service: StoreOperationsServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:read")],
) -> Envelope[list[AdminStoreProductGroupView]]:
    _no_store(response)
    return Envelope(data=await service.groups(access, store_id))


@router.post(
    "/product-groups",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AdminStoreProductGroupView],
    operation_id="AdminStoreProductGroup_Create",
)
async def create_product_group(
    store_id: str,
    payload: AdminStoreProductGroupCreateRequest,
    response: Response,
    service: StoreOperationsServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
) -> Envelope[AdminStoreProductGroupView]:
    item = await service.create_group(access, store_id, payload, idempotency_key)
    return _resource(response, item, item.version)


@router.patch(
    "/product-groups/{group_id}",
    response_model=Envelope[AdminStoreProductGroupView],
    operation_id="AdminStoreProductGroup_Update",
)
async def update_product_group(
    store_id: str,
    group_id: str,
    payload: AdminStoreProductGroupUpdateRequest,
    response: Response,
    service: StoreOperationsServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminStoreProductGroupView]:
    item = await service.update_group(
        access, store_id, group_id, payload, _expected_version(if_match)
    )
    return _resource(response, item, item.version)


@router.put(
    "/product-groups/{group_id}/products",
    response_model=Envelope[AdminStoreProductGroupView],
    operation_id="AdminStoreProductGroup_ReplaceProducts",
)
async def replace_product_group_products(
    store_id: str,
    group_id: str,
    payload: AdminStoreProductGroupProductsRequest,
    response: Response,
    service: StoreOperationsServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminStoreProductGroupView]:
    item = await service.replace_group_products(
        access, store_id, group_id, payload, _expected_version(if_match)
    )
    return _resource(response, item, item.version)


@router.get(
    "/shipping-templates",
    response_model=Envelope[list[AdminShippingTemplateView]],
    operation_id="AdminShippingTemplate_List",
)
async def list_shipping_templates(
    store_id: str,
    response: Response,
    service: StoreOperationsServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:read")],
) -> Envelope[list[AdminShippingTemplateView]]:
    _no_store(response)
    return Envelope(data=await service.shipping_templates(access, store_id))


@router.post(
    "/shipping-templates",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AdminShippingTemplateView],
    operation_id="AdminShippingTemplate_Create",
)
async def create_shipping_template(
    store_id: str,
    payload: AdminShippingTemplateCreateRequest,
    response: Response,
    service: StoreOperationsServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
) -> Envelope[AdminShippingTemplateView]:
    item = await service.create_shipping_template(access, store_id, payload, idempotency_key)
    return _resource(response, item, item.version)


@router.patch(
    "/shipping-templates/{template_id}",
    response_model=Envelope[AdminShippingTemplateView],
    operation_id="AdminShippingTemplate_Update",
)
async def update_shipping_template(
    store_id: str,
    template_id: str,
    payload: AdminShippingTemplateUpdateRequest,
    response: Response,
    service: StoreOperationsServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminShippingTemplateView]:
    item = await service.update_shipping_template(
        access, store_id, template_id, payload, _expected_version(if_match)
    )
    return _resource(response, item, item.version)


@router.post(
    "/shipping-templates/{template_id}/publications",
    response_model=Envelope[AdminShippingTemplateView],
    operation_id="AdminShippingTemplate_Publish",
)
async def publish_shipping_template(
    store_id: str,
    template_id: str,
    payload: AdminShippingTemplatePublicationRequest,
    response: Response,
    service: StoreOperationsServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminShippingTemplateView]:
    item = await service.publish_shipping_template(
        access,
        store_id,
        template_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    return _resource(response, item, item.version)


@router.get(
    "/announcements",
    response_model=Envelope[list[AdminStoreAnnouncementView]],
    operation_id="AdminStoreAnnouncement_List",
)
async def list_announcements(
    store_id: str,
    response: Response,
    service: StoreOperationsServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:read")],
) -> Envelope[list[AdminStoreAnnouncementView]]:
    _no_store(response)
    return Envelope(data=await service.announcements(access, store_id))


@router.post(
    "/announcements",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AdminStoreAnnouncementView],
    operation_id="AdminStoreAnnouncement_Create",
)
async def create_announcement(
    store_id: str,
    payload: AdminStoreAnnouncementCreateRequest,
    response: Response,
    service: StoreOperationsServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
) -> Envelope[AdminStoreAnnouncementView]:
    item = await service.create_announcement(access, store_id, payload, idempotency_key)
    return _resource(response, item, item.version)


@router.patch(
    "/announcements/{announcement_id}",
    response_model=Envelope[AdminStoreAnnouncementView],
    operation_id="AdminStoreAnnouncement_Update",
)
async def update_announcement(
    store_id: str,
    announcement_id: str,
    payload: AdminStoreAnnouncementUpdateRequest,
    response: Response,
    service: StoreOperationsServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminStoreAnnouncementView]:
    item = await service.update_announcement(
        access, store_id, announcement_id, payload, _expected_version(if_match)
    )
    return _resource(response, item, item.version)


@router.get(
    "/featured-products",
    response_model=Envelope[list[AdminFeaturedProductView]],
    operation_id="AdminStoreFeaturedProduct_List",
)
async def list_featured_products(
    store_id: str,
    response: Response,
    service: StoreOperationsServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:read")],
    slot_type: Annotated[str, Query(pattern="^(recommended|hot)$")] = "recommended",
) -> Envelope[list[AdminFeaturedProductView]]:
    _no_store(response)
    return Envelope(data=await service.featured(access, store_id, slot_type))


@router.put(
    "/featured-products",
    response_model=Envelope[list[AdminFeaturedProductView]],
    operation_id="AdminStoreFeaturedProduct_Replace",
)
async def replace_featured_products(
    store_id: str,
    payload: AdminFeaturedProductSetRequest,
    response: Response,
    service: StoreOperationsServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[list[AdminFeaturedProductView]]:
    items = await service.replace_featured(access, store_id, payload, _expected_version(if_match))
    response.headers["ETag"] = _etag(await service.store_version(access, store_id))
    _no_store(response)
    return Envelope(data=items)


def _resource[ResourceT](response: Response, item: ResourceT, version: int) -> Envelope[ResourceT]:
    response.headers["ETag"] = _etag(version)
    _no_store(response)
    return Envelope(data=item)
