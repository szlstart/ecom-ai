import hashlib
import os
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.modules.catalog.models import Brand, Category, Product, ProductSku
from app.modules.identity.models import AuthSession, User
from app.modules.inventory.models import Inventory
from app.modules.stores.models import (
    Store,
    StoreAnnouncement,
    StoreFeaturedProduct,
    StoreProductGroup,
    StoreProductGroupItem,
    StoreServicePolicy,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_public_catalog_store_cursor_and_favorite_lifecycle(client: AsyncClient) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())

    async for session in mysql_session():
        user = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"catalog_{suffix}",
            username_normalized=f"catalog_{suffix}",
            nickname=f"Catalog {suffix}",
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            permission_version=1,
            registered_at=now,
        )
        session.add(user)
        await session.flush()

        auth_session = AuthSession(
            session_no=new_prefixed_ulid("ses_"),
            user_id=user.id,
            refresh_token_hash=security.keyed_hash("refresh-token", secrets.token_urlsafe()),
            token_family_no=new_prefixed_ulid("tfa_"),
            device_no=new_prefixed_ulid("dev_"),
            device_name="Catalog integration",
            client_type="web",
            audience="user",
            csrf_token_hash=security.keyed_hash("csrf-token", secrets.token_urlsafe()),
            authenticated_at=now,
            authentication_methods=["password"],
            assurance_level="aal1",
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
        )
        session.add(auth_session)

        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            category_name=f"测试分类 {suffix}",
            category_code=f"test-{suffix}",
            path="/test",
            level=1,
            sort_order=1,
            category_status="active",
        )
        brand = Brand(
            brand_no=new_prefixed_ulid("brd_"),
            brand_name=f"测试品牌 {suffix}",
            brand_name_normalized=f"brand-{suffix}",
            brand_status="active",
        )
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=user.id,
            store_name=f"测试店铺 {suffix}",
            store_name_normalized=f"store-{suffix}",
            description="公开店铺集成测试",
            store_status="active",
            rating_score=Decimal("4.80"),
            rating_count=12,
            follower_count=0,
            sales_count=300,
            opened_at=now,
        )
        session.add_all([category, brand, store])
        await session.flush()

        products: list[Product] = []
        for index, sales in enumerate((30, 20, 10), start=1):
            product = Product(
                product_no=new_prefixed_ulid("prd_"),
                store_id=store.id,
                category_id=category.id,
                brand_id=brand.id,
                product_name=f"集成测试商品 {suffix}-{index}",
                subtitle=f"第 {index} 个商品",
                product_status="on_sale",
                min_price_amount=index * 1000,
                max_price_amount=index * 1000,
                currency="CNY",
                sales_count=sales,
                review_count=index,
                rating_score=Decimal("4.50"),
                published_at=now - timedelta(minutes=index),
            )
            session.add(product)
            products.append(product)
        await session.flush()

        sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=products[0].id,
            store_id=store.id,
            merchant_sku_code=f"merchant-{suffix}",
            sku_name="标准款",
            spec_values=[{"name": "规格", "value": "标准"}],
            spec_signature=hashlib.sha256(f"spec-{suffix}".encode()).digest(),
            sale_price_amount=1000,
            market_price_amount=1200,
            currency="CNY",
            sku_status="active",
        )
        session.add(sku)
        await session.flush()
        session.add(
            Inventory(
                sku_id=sku.id,
                on_hand_quantity=20,
                reserved_quantity=3,
                safety_stock_quantity=2,
                sold_quantity=30,
                inventory_status="active",
            )
        )

        group = StoreProductGroup(
            group_no=new_prefixed_ulid("grp_"),
            store_id=store.id,
            group_name="精选",
            group_name_normalized=f"featured-{suffix}",
            group_status="active",
            sort_order=1,
        )
        session.add(group)
        await session.flush()
        session.add(
            StoreProductGroupItem(
                store_product_group_id=group.id,
                product_id=products[0].id,
                store_id=store.id,
                sort_order=1,
            )
        )
        session.add_all(
            [
                StoreAnnouncement(
                    announcement_no=new_prefixed_ulid("ann_"),
                    store_id=store.id,
                    title="测试公告",
                    content="欢迎来到测试店铺",
                    announcement_status="published",
                    starts_at=now - timedelta(minutes=1),
                    sort_order=1,
                ),
                StoreServicePolicy(
                    policy_no=new_prefixed_ulid("pol_"),
                    store_id=store.id,
                    policy_type="shipping",
                    title="发货政策",
                    content="预计 48 小时内发货。",
                    content_hash=hashlib.sha256(b"shipping-policy").digest(),
                    policy_version=1,
                    policy_status="published",
                    effective_at=now - timedelta(minutes=1),
                    published_at=now,
                    created_by=user.id,
                    published_by=user.id,
                ),
                StoreFeaturedProduct(
                    store_id=store.id,
                    product_id=products[0].id,
                    slot_type="recommended",
                    sort_order=1,
                    starts_at=now - timedelta(minutes=1),
                ),
            ]
        )
        await session.commit()
        user_no = user.user_no
        session_no = auth_session.session_no
        product_no = products[0].product_no
        store_no = store.store_no
        group_no = group.group_no

    token, _ = security.create_access_token(
        user_no=user_no,
        session_no=session_no,
        audience="user",
        permission_version=1,
    )
    auth = {"Authorization": f"Bearer {token}"}

    first_page = await client.get("/api/v1/products", params={"sort": "sales", "limit": 2})
    assert first_page.status_code == 200, first_page.text
    first_payload = first_page.json()
    assert len(first_payload["data"]["items"]) >= 2
    assert first_payload["meta"]["pagination"]["next_cursor"]

    second_page = await client.get(
        "/api/v1/products",
        params={
            "sort": "sales",
            "limit": 2,
            "cursor": first_payload["meta"]["pagination"]["next_cursor"],
        },
    )
    assert second_page.status_code == 200, second_page.text
    assert second_page.json()["meta"]["pagination"]["has_previous"] is True

    mismatch = await client.get(
        "/api/v1/products",
        params={
            "sort": "newest",
            "limit": 2,
            "cursor": first_payload["meta"]["pagination"]["next_cursor"],
        },
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["code"] == "PAGINATION_CURSOR_INVALID"

    detail = await client.get(f"/api/v1/products/{product_no}", headers=auth)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["store"]["store_id"] == store_no
    assert "source_content" not in detail.text

    skus = await client.get(f"/api/v1/products/{product_no}/skus")
    assert skus.status_code == 200, skus.text
    assert skus.json()["data"]["items"][0]["max_purchase_quantity"] == 15

    grouped = await client.get(f"/api/v1/stores/{store_no}/products", params={"group_id": group_no})
    assert grouped.status_code == 200, grouped.text
    assert [item["product_id"] for item in grouped.json()["data"]["items"]] == [product_no]

    store_page = await client.get(f"/api/v1/stores/{store_no}", headers=auth)
    assert store_page.status_code == 200, store_page.text
    assert store_page.json()["data"]["customer_service_enabled"] is True

    favorite = await client.put(f"/api/v1/users/me/favorite-products/{product_no}", headers=auth)
    assert favorite.status_code == 204, favorite.text
    repeated_favorite = await client.put(
        f"/api/v1/users/me/favorite-products/{product_no}", headers=auth
    )
    assert repeated_favorite.status_code == 204
    favorites = await client.get("/api/v1/users/me/favorite-products", headers=auth)
    assert favorites.status_code == 200
    assert product_no in [item["product_id"] for item in favorites.json()["data"]["items"]]

    follow = await client.put(f"/api/v1/users/me/followed-stores/{store_no}", headers=auth)
    assert follow.status_code == 204, follow.text
    repeated_follow = await client.put(f"/api/v1/users/me/followed-stores/{store_no}", headers=auth)
    assert repeated_follow.status_code == 204
    followed = await client.get("/api/v1/users/me/followed-stores", headers=auth)
    assert followed.status_code == 200
    assert store_no in [item["store_id"] for item in followed.json()["data"]["items"]]
