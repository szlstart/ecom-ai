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
        ("/api/v1/admin/categories", "get"): "AdminCategory_List",
        ("/api/v1/admin/categories", "post"): "AdminCategory_Upsert",
        ("/api/v1/admin/brands", "get"): "AdminBrand_List",
        ("/api/v1/admin/brands", "post"): "AdminBrand_Upsert",
        ("/api/v1/admin/inventories", "get"): "AdminInventory_List",
        ("/api/v1/admin/inventory-adjustments", "post"): "AdminInventory_Adjust",
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
