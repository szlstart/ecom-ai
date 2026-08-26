from datetime import timedelta

import pytest

from app.core.exceptions import ApplicationError
from app.core.pagination import CursorCodec
from app.main import create_app


def test_catalog_and_store_openapi_operations_are_stable() -> None:
    paths = create_app().openapi()["paths"]
    expected = {
        ("/api/v1/homepage", "get"): "Homepage_Get",
        ("/api/v1/products", "get"): "Product_Search",
        ("/api/v1/products/{product_id}", "get"): "Product_Get",
        ("/api/v1/products/{product_id}/skus", "get"): "ProductSku_List",
        ("/api/v1/products/{product_id}/faqs", "get"): "ProductFaq_List",
        ("/api/v1/products/{product_id}/reviews", "get"): "ProductReview_List",
        ("/api/v1/search/suggestions", "get"): "SearchSuggestion_List",
        ("/api/v1/stores/{store_id}", "get"): "Store_Get",
        ("/api/v1/stores/{store_id}/products", "get"): "StoreProduct_List",
        ("/api/v1/stores/{store_id}/product-groups", "get"): "StoreProductGroup_List",
        ("/api/v1/stores/{store_id}/home-content", "get"): "StoreHomeContent_Get",
        ("/api/v1/stores/{store_id}/service-policies", "get"): "StorePolicy_ListPublic",
        ("/api/v1/users/me/favorite-products", "get"): "FavoriteProduct_ListMine",
        ("/api/v1/users/me/favorite-products/{product_id}", "put"): "FavoriteProduct_Put",
        ("/api/v1/users/me/favorite-products/{product_id}", "delete"): "FavoriteProduct_Delete",
        ("/api/v1/users/me/followed-stores", "get"): "FollowedStore_ListMine",
        ("/api/v1/users/me/followed-stores/{store_id}", "put"): "FollowedStore_Put",
        ("/api/v1/users/me/followed-stores/{store_id}", "delete"): "FollowedStore_Delete",
        ("/api/v1/users/me/cart", "get"): "Cart_GetMine",
        ("/api/v1/users/me/cart/items", "post"): "CartItem_Create",
        ("/api/v1/users/me/cart/items/{cart_item_id}", "patch"): "CartItem_Patch",
        ("/api/v1/users/me/cart/items/{cart_item_id}", "delete"): "CartItem_Delete",
        ("/api/v1/users/me/cart/selection", "put"): "CartSelection_Replace",
        ("/api/v1/users/me/cart/invalid-items", "delete"): "CartInvalidItem_Clear",
        ("/api/v1/admin/categories", "get"): "AdminCategory_List",
        ("/api/v1/admin/categories", "post"): "AdminCategory_Upsert",
        ("/api/v1/admin/brands", "get"): "AdminBrand_List",
        ("/api/v1/admin/brands", "post"): "AdminBrand_Upsert",
        ("/api/v1/admin/inventories", "get"): "AdminInventory_List",
        ("/api/v1/admin/inventory-adjustments", "post"): "AdminInventory_Adjust",
        ("/api/v1/admin/stores", "get"): "AdminStore_List",
        ("/api/v1/admin/stores/{store_id}", "get"): "AdminStore_Get",
        ("/api/v1/admin/stores/{store_id}", "patch"): "AdminStore_Update",
        (
            "/api/v1/admin/stores/{store_id}/status-changes",
            "post",
        ): "AdminStore_ChangeStatus",
        (
            "/api/v1/admin/store-certifications",
            "get",
        ): "AdminStoreCertification_List",
        (
            "/api/v1/admin/store-certifications/{certification_id}",
            "get",
        ): "AdminStoreCertification_Get",
        (
            "/api/v1/admin/store-certifications/{certification_id}/decisions",
            "post",
        ): "AdminStoreCertification_Decide",
        (
            "/api/v1/admin/store-certifications/{certification_id}/material-versions",
            "post",
        ): "AdminStoreCertification_AddMaterialVersion",
        (
            "/api/v1/admin/stores/{store_id}/service-policies",
            "get",
        ): "AdminStorePolicy_List",
        (
            "/api/v1/admin/stores/{store_id}/service-policies",
            "post",
        ): "AdminStorePolicy_Create",
        (
            "/api/v1/admin/stores/{store_id}/service-policies/{policy_id}",
            "patch",
        ): "AdminStorePolicy_Update",
        (
            "/api/v1/admin/stores/{store_id}/service-policies/{policy_id}/publications",
            "post",
        ): "AdminStorePolicy_Publish",
        (
            "/api/v1/admin/stores/{store_id}/service-policies/{policy_id}/withdrawals",
            "post",
        ): "AdminStorePolicy_Withdraw",
        (
            "/api/v1/admin/stores/{store_id}/product-groups",
            "get",
        ): "AdminStoreProductGroup_List",
        (
            "/api/v1/admin/stores/{store_id}/product-groups",
            "post",
        ): "AdminStoreProductGroup_Create",
        (
            "/api/v1/admin/stores/{store_id}/product-groups/{group_id}",
            "patch",
        ): "AdminStoreProductGroup_Update",
        (
            "/api/v1/admin/stores/{store_id}/product-groups/{group_id}/products",
            "put",
        ): "AdminStoreProductGroup_ReplaceProducts",
        (
            "/api/v1/admin/stores/{store_id}/shipping-templates",
            "get",
        ): "AdminShippingTemplate_List",
        (
            "/api/v1/admin/stores/{store_id}/shipping-templates",
            "post",
        ): "AdminShippingTemplate_Create",
        (
            "/api/v1/admin/stores/{store_id}/shipping-templates/{template_id}",
            "patch",
        ): "AdminShippingTemplate_Update",
        (
            "/api/v1/admin/stores/{store_id}/shipping-templates/{template_id}/publications",
            "post",
        ): "AdminShippingTemplate_Publish",
        (
            "/api/v1/admin/stores/{store_id}/announcements",
            "get",
        ): "AdminStoreAnnouncement_List",
        (
            "/api/v1/admin/stores/{store_id}/announcements",
            "post",
        ): "AdminStoreAnnouncement_Create",
        (
            "/api/v1/admin/stores/{store_id}/announcements/{announcement_id}",
            "patch",
        ): "AdminStoreAnnouncement_Update",
        (
            "/api/v1/admin/stores/{store_id}/featured-products",
            "get",
        ): "AdminStoreFeaturedProduct_List",
        (
            "/api/v1/admin/stores/{store_id}/featured-products",
            "put",
        ): "AdminStoreFeaturedProduct_Replace",
        ("/api/v1/admin/products", "get"): "AdminProduct_List",
        ("/api/v1/admin/products", "post"): "AdminProduct_Create",
        ("/api/v1/admin/product-import-template", "get"): "AdminProductImportTemplate_Get",
        (
            "/api/v1/admin/product-import-template.csv",
            "get",
        ): "AdminProductImportTemplate_Download",
        ("/api/v1/admin/batch-jobs", "post"): "AdminBatchJob_Create",
        ("/api/v1/admin/batch-jobs", "get"): "AdminBatchJob_List",
        ("/api/v1/admin/batch-jobs/{job_id}", "get"): "AdminBatchJob_Get",
        (
            "/api/v1/admin/batch-jobs/{job_id}/items",
            "get",
        ): "AdminBatchJobItem_List",
        (
            "/api/v1/admin/batch-jobs/{job_id}/confirmations",
            "post",
        ): "AdminBatchJob_Confirm",
        (
            "/api/v1/admin/batch-jobs/{job_id}/cancellations",
            "post",
        ): "AdminBatchJob_Cancel",
        ("/api/v1/admin/products/{product_id}", "get"): "AdminProduct_Get",
        ("/api/v1/admin/products/{product_id}", "patch"): "AdminProduct_Update",
        (
            "/api/v1/admin/products/{product_id}/review-submissions",
            "post",
        ): "AdminProduct_Submit",
        (
            "/api/v1/admin/products/{product_id}/moderation-decisions",
            "post",
        ): "AdminProduct_Moderate",
        (
            "/api/v1/admin/products/{product_id}/publications",
            "post",
        ): "AdminProduct_Publish",
        (
            "/api/v1/admin/products/{product_id}/off-shelf-commands",
            "post",
        ): "AdminProduct_OffShelf",
        ("/api/v1/admin/products/{product_id}/skus", "post"): "AdminProductSku_Create",
        (
            "/api/v1/admin/products/{product_id}/images",
            "put",
        ): "AdminProductImage_Replace",
        (
            "/api/v1/admin/products/{product_id}/attributes",
            "put",
        ): "AdminProductAttribute_Replace",
        (
            "/api/v1/admin/products/{product_id}/fulfillment-profile",
            "get",
        ): "AdminProductFulfillment_Get",
        (
            "/api/v1/admin/products/{product_id}/fulfillment-profile",
            "put",
        ): "AdminProductFulfillment_Upsert",
        (
            "/api/v1/admin/products/{product_id}/detail-content-versions",
            "post",
        ): "AdminProductContentVersion_Create",
        (
            "/api/v1/admin/products/{product_id}/faqs",
            "post",
        ): "AdminProductFaq_Create",
        (
            "/api/v1/admin/products/{product_id}/faqs/{faq_id}/publications",
            "post",
        ): "AdminProductFaq_Publish",
        ("/api/v1/file-upload-policies/{purpose}", "get"): "FileUploadPolicy_Get",
        ("/api/v1/file-upload-sessions", "post"): "FileUploadSession_Create",
        ("/api/v1/file-upload-sessions/{upload_id}", "get"): "FileUploadSession_Get",
        (
            "/api/v1/file-upload-sessions/{upload_id}/complete",
            "post",
        ): "FileUploadSession_Complete",
        (
            "/api/v1/file-upload-sessions/{upload_id}",
            "delete",
        ): "FileUploadSession_Abort",
        ("/api/v1/files/{file_id}/metadata", "get"): "File_GetMetadata",
        ("/api/v1/files/{file_id}", "get"): "File_Get",
    }
    for (path, method), operation_id in expected.items():
        assert paths[path][method]["operationId"] == operation_id


def test_signed_cursor_is_filter_bound_and_tamper_evident() -> None:
    codec = CursorCodec("test-only-secret")
    token = codec.encode(filter_key='{"sort":"sales"}', values=("42", "7"))

    assert codec.decode(token, filter_key='{"sort":"sales"}') is not None
    with pytest.raises(ApplicationError) as mismatch:
        codec.decode(token, filter_key='{"sort":"newest"}')
    assert mismatch.value.code == "PAGINATION_CURSOR_INVALID"

    altered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(ApplicationError) as tampered:
        codec.decode(altered, filter_key='{"sort":"sales"}')
    assert tampered.value.code == "PAGINATION_CURSOR_INVALID"


def test_expired_cursor_requires_restart() -> None:
    codec = CursorCodec("test-only-secret", ttl=timedelta(seconds=-1))
    token = codec.encode(filter_key="catalog", values=("1", "1"))

    with pytest.raises(ApplicationError) as expired:
        codec.decode(token, filter_key="catalog")
    assert expired.value.status == 410
    assert expired.value.code == "PAGINATION_CURSOR_EXPIRED"
