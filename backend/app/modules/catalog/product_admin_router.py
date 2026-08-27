from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.catalog.dependencies import ProductAdminServiceDependency
from app.modules.catalog.product_admin_schemas import (
    AdminContentVersionCreateRequest,
    AdminContentVersionView,
    AdminFaqCreateRequest,
    AdminFaqPublicationRequest,
    AdminFaqVersionCreateRequest,
    AdminFaqView,
    AdminProductAttributeInput,
    AdminProductAttributeSetRequest,
    AdminProductCommandRequest,
    AdminProductCreateRequest,
    AdminProductDeletionEligibility,
    AdminProductDeletionView,
    AdminProductDetail,
    AdminProductFulfillmentRequest,
    AdminProductFulfillmentView,
    AdminProductImageSetRequest,
    AdminProductImageView,
    AdminProductList,
    AdminProductModerationRequest,
    AdminProductUpdateRequest,
    AdminSkuCreateRequest,
    AdminSkuStatusRequest,
    AdminSkuUpdateRequest,
    AdminSkuView,
)
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission

router = APIRouter(prefix="/admin/products", tags=["product-administration"])


@router.get("", response_model=Envelope[AdminProductList], operation_id="AdminProduct_List")
async def list_products(
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:read")],
    store_id: Annotated[str | None, Query(max_length=40)] = None,
    product_status: Annotated[str | None, Query(alias="status", max_length=32)] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[AdminProductList]:
    _no_store(response)
    return Envelope(
        data=await service.list_products(
            access,
            store_no=store_id,
            product_status=product_status,
            q=q,
            cursor=cursor,
            limit=limit,
        )
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AdminProductDetail],
    operation_id="AdminProduct_Create",
)
async def create_product(
    payload: AdminProductCreateRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:create")],
) -> Envelope[AdminProductDetail]:
    item = await service.create_product(access, payload, idempotency_key)
    return _resource(response, item, item.version)


@router.get(
    "/{product_id}", response_model=Envelope[AdminProductDetail], operation_id="AdminProduct_Get"
)
async def get_product(
    product_id: str,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:read")],
) -> Envelope[AdminProductDetail]:
    item = await service.get_product(access, product_id)
    return _resource(response, item, item.version)


@router.patch(
    "/{product_id}", response_model=Envelope[AdminProductDetail], operation_id="AdminProduct_Update"
)
async def update_product(
    product_id: str,
    payload: AdminProductUpdateRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminProductDetail]:
    item = await service.update_product(access, product_id, payload, _expected_version(if_match))
    return _resource(response, item, item.version)


@router.get(
    "/{product_id}/deletion-eligibility",
    response_model=Envelope[AdminProductDeletionEligibility],
    operation_id="AdminProductDeletionEligibility_Check",
)
async def check_product_deletion_eligibility(
    product_id: str,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
) -> Envelope[AdminProductDeletionEligibility]:
    item = await service.deletion_eligibility(access, product_id)
    _no_store(response)
    return Envelope(data=item)


@router.delete(
    "/{product_id}",
    response_model=Envelope[AdminProductDeletionView],
    operation_id="AdminProduct_Delete",
)
async def delete_product(
    product_id: str,
    response: Response,
    service: ProductAdminServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminProductDeletionView]:
    item = await service.delete_product(
        access, product_id, _expected_version(if_match), idempotency_key
    )
    _no_store(response)
    return Envelope(data=item)


async def _product_command(
    product_id: str,
    payload: AdminProductCommandRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    access: AdminAccess,
    if_match: str | None,
    key: str,
    command: str,
) -> Envelope[AdminProductDetail]:
    method = {
        "submit": service.submit_review,
        "publish": service.publish,
        "off_shelf": service.off_shelf,
    }[command]
    item = await method(access, product_id, payload, _expected_version(if_match), key)
    return _resource(response, item, item.version)


@router.post(
    "/{product_id}/review-submissions",
    response_model=Envelope[AdminProductDetail],
    operation_id="AdminProduct_Submit",
)
async def submit_product(
    product_id: str,
    payload: AdminProductCommandRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminProductDetail]:
    return await _product_command(
        product_id, payload, response, service, access, if_match, idempotency_key, "submit"
    )


@router.post(
    "/{product_id}/moderation-decisions",
    response_model=Envelope[AdminProductDetail],
    operation_id="AdminProduct_Moderate",
)
async def moderate_product(
    product_id: str,
    payload: AdminProductModerationRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:review")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminProductDetail]:
    item = await service.moderate(
        access, product_id, payload, _expected_version(if_match), idempotency_key
    )
    return _resource(response, item, item.version)


@router.post(
    "/{product_id}/publications",
    response_model=Envelope[AdminProductDetail],
    operation_id="AdminProduct_Publish",
)
async def publish_product(
    product_id: str,
    payload: AdminProductCommandRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:publish")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminProductDetail]:
    return await _product_command(
        product_id, payload, response, service, access, if_match, idempotency_key, "publish"
    )


@router.post(
    "/{product_id}/off-shelf-commands",
    response_model=Envelope[AdminProductDetail],
    operation_id="AdminProduct_OffShelf",
)
async def off_shelf_product(
    product_id: str,
    payload: AdminProductCommandRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:publish")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminProductDetail]:
    return await _product_command(
        product_id, payload, response, service, access, if_match, idempotency_key, "off_shelf"
    )


@router.get(
    "/{product_id}/skus",
    response_model=Envelope[list[AdminSkuView]],
    operation_id="AdminProductSku_List",
)
async def list_skus(
    product_id: str,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:read")],
) -> Envelope[list[AdminSkuView]]:
    _no_store(response)
    return Envelope(data=await service.skus(access, product_id))


@router.post(
    "/{product_id}/skus",
    status_code=201,
    response_model=Envelope[AdminSkuView],
    operation_id="AdminProductSku_Create",
)
async def create_sku(
    product_id: str,
    payload: AdminSkuCreateRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
) -> Envelope[AdminSkuView]:
    item = await service.create_sku(access, product_id, payload, idempotency_key)
    return _resource(response, item, item.version)


@router.patch(
    "/{product_id}/skus/{sku_id}",
    response_model=Envelope[AdminSkuView],
    operation_id="AdminProductSku_Update",
)
async def update_sku(
    product_id: str,
    sku_id: str,
    payload: AdminSkuUpdateRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminSkuView]:
    item = await service.update_sku(
        access, product_id, sku_id, payload, _expected_version(if_match)
    )
    return _resource(response, item, item.version)


@router.post(
    "/{product_id}/skus/{sku_id}/status-changes",
    response_model=Envelope[AdminSkuView],
    operation_id="AdminProductSku_ChangeStatus",
)
async def change_sku_status(
    product_id: str,
    sku_id: str,
    payload: AdminSkuStatusRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminSkuView]:
    item = await service.change_sku_status(
        access, product_id, sku_id, payload, _expected_version(if_match), idempotency_key
    )
    return _resource(response, item, item.version)


@router.get(
    "/{product_id}/images",
    response_model=Envelope[list[AdminProductImageView]],
    operation_id="AdminProductImage_List",
)
async def list_images(
    product_id: str,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:read")],
) -> Envelope[list[AdminProductImageView]]:
    _no_store(response)
    return Envelope(data=await service.images(access, product_id))


@router.get(
    "/{product_id}/fulfillment-profile",
    response_model=Envelope[AdminProductFulfillmentView | None],
    operation_id="AdminProductFulfillment_Get",
)
async def get_fulfillment(
    product_id: str,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:read")],
) -> Envelope[AdminProductFulfillmentView | None]:
    _no_store(response)
    return Envelope(data=await service.fulfillment(access, product_id))


@router.put(
    "/{product_id}/images",
    response_model=Envelope[list[AdminProductImageView]],
    operation_id="AdminProductImage_Replace",
)
async def replace_images(
    product_id: str,
    payload: AdminProductImageSetRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[list[AdminProductImageView]]:
    items = await service.replace_images(access, product_id, payload, _expected_version(if_match))
    latest = await service.get_product(access, product_id)
    response.headers["ETag"] = _etag(latest.version)
    _no_store(response)
    return Envelope(data=items)


@router.get(
    "/{product_id}/attributes",
    response_model=Envelope[list[AdminProductAttributeInput]],
    operation_id="AdminProductAttribute_List",
)
async def list_attributes(
    product_id: str,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:read")],
) -> Envelope[list[AdminProductAttributeInput]]:
    _no_store(response)
    return Envelope(data=await service.attributes(access, product_id))


@router.put(
    "/{product_id}/attributes",
    response_model=Envelope[list[AdminProductAttributeInput]],
    operation_id="AdminProductAttribute_Replace",
)
async def replace_attributes(
    product_id: str,
    payload: AdminProductAttributeSetRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[list[AdminProductAttributeInput]]:
    items = await service.replace_attributes(
        access, product_id, payload, _expected_version(if_match)
    )
    latest = await service.get_product(access, product_id)
    response.headers["ETag"] = _etag(latest.version)
    _no_store(response)
    return Envelope(data=items)


@router.put(
    "/{product_id}/fulfillment-profile",
    response_model=Envelope[AdminProductFulfillmentView],
    operation_id="AdminProductFulfillment_Upsert",
)
async def set_fulfillment(
    product_id: str,
    payload: AdminProductFulfillmentRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminProductFulfillmentView]:
    item = await service.set_fulfillment(access, product_id, payload, _expected_version(if_match))
    latest = await service.get_product(access, product_id)
    return _resource(response, item, latest.version)


@router.post(
    "/{product_id}/detail-content-versions",
    status_code=201,
    response_model=Envelope[AdminContentVersionView],
    operation_id="AdminProductContentVersion_Create",
)
async def create_content_version(
    product_id: str,
    payload: AdminContentVersionCreateRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
) -> Envelope[AdminContentVersionView]:
    item = await service.create_content_version(access, product_id, payload, idempotency_key)
    latest = await service.get_product(access, product_id)
    response.headers["ETag"] = _etag(latest.version)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/{product_id}/detail-content-versions/{version_id}",
    response_model=Envelope[AdminContentVersionView],
    operation_id="AdminProductContentVersion_Get",
)
async def get_content_version(
    product_id: str,
    version_id: str,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:read")],
) -> Envelope[AdminContentVersionView]:
    _no_store(response)
    return Envelope(data=await service.content_version(access, product_id, version_id))


@router.get(
    "/{product_id}/faqs",
    response_model=Envelope[list[AdminFaqView]],
    operation_id="AdminProductFaq_List",
)
async def list_faqs(
    product_id: str,
    response: Response,
    service: ProductAdminServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("products:read")],
) -> Envelope[list[AdminFaqView]]:
    _no_store(response)
    return Envelope(data=await service.faqs(access, product_id))


@router.post(
    "/{product_id}/faqs",
    status_code=201,
    response_model=Envelope[AdminFaqView],
    operation_id="AdminProductFaq_Create",
)
async def create_faq(
    product_id: str,
    payload: AdminFaqCreateRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
) -> Envelope[AdminFaqView]:
    item = await service.create_faq(access, product_id, payload, idempotency_key)
    latest = await service.get_product(access, product_id)
    return _resource(response, item, latest.version)


@router.post(
    "/{product_id}/faqs/{faq_id}/versions",
    status_code=201,
    response_model=Envelope[AdminContentVersionView],
    operation_id="AdminProductFaqVersion_Create",
)
async def create_faq_version(
    product_id: str,
    faq_id: str,
    payload: AdminFaqVersionCreateRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:update")],
) -> Envelope[AdminContentVersionView]:
    item = await service.create_faq_version(access, product_id, faq_id, payload, idempotency_key)
    latest = await service.get_product(access, product_id)
    return _resource(response, item, latest.version)


@router.post(
    "/{product_id}/faqs/{faq_id}/publications",
    response_model=Envelope[AdminFaqView],
    operation_id="AdminProductFaq_Publish",
)
async def publish_faq(
    product_id: str,
    faq_id: str,
    payload: AdminFaqPublicationRequest,
    response: Response,
    service: ProductAdminServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("products:publish")],
) -> Envelope[AdminFaqView]:
    item = await service.publish_faq(access, product_id, faq_id, payload, idempotency_key)
    latest = await service.get_product(access, product_id)
    return _resource(response, item, latest.version)


def _resource[T](response: Response, data: T, version: int) -> Envelope[T]:
    response.headers["ETag"] = _etag(version)
    _no_store(response)
    return Envelope(data=data)
