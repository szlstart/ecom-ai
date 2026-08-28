import hashlib
import os
import secrets
from datetime import timedelta
from decimal import Decimal

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.bootstrap.admin import provision_platform_super_admin
from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.modules.catalog.models import Brand, Category, Product, ProductSku
from app.modules.identity.models import AuthSession, User
from app.modules.inventory.models import Inventory, InventoryLog
from app.modules.orders.models import Order, OrderItem, TradeOrder
from app.modules.rbac.models import AdminOperationLog, Role, UserRole
from app.modules.reviews.models import Review, ReviewAppendRecord, ReviewReply
from app.modules.stores.models import (
    Store,
    StoreAnnouncement,
    StoreFeaturedProduct,
    StoreProductGroup,
    StoreProductGroupItem,
    StoreServicePolicy,
)
from app.modules.system.models import OutboxEvent

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
        consumer_role = await session.scalar(select(Role).where(Role.role_code == "user"))
        assert consumer_role is not None
        session.add(
            UserRole(
                user_id=user.id,
                role_id=consumer_role.id,
                grant_no=new_prefixed_ulid("grt_"),
                scope_type="platform",
                scope_id=0,
                grant_status="active",
                active_grant_key=security.keyed_hash(
                    "active-role-grant", f"{user.id}:{consumer_role.id}:platform:0"
                ),
                granted_by=user.id,
                granted_at=now,
                grant_reason="catalog_integration_consumer",
            )
        )

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
                review_count=2 if index == 1 else index,
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
        review_trade = TradeOrder(
            trade_no=new_prefixed_ulid("trd_"),
            checkout_session_id=None,
            checkout_no_snapshot=new_prefixed_ulid("chk_"),
            checkout_snapshot_hash=hashlib.sha256(f"review-checkout-{suffix}".encode()).digest(),
            user_id=user.id,
            order_source="buy_now",
            trade_status="paid",
            goods_amount=3000,
            freight_amount=0,
            payable_amount=3000,
            adjustment_amount=0,
            paid_amount=3000,
            refunded_amount=0,
            currency="CNY",
            order_count=1,
            expires_at=now + timedelta(days=1),
            paid_at=now - timedelta(days=2),
        )
        session.add(review_trade)
        await session.flush()
        review_order = Order(
            order_no=new_prefixed_ulid("ord_"),
            trade_order_id=review_trade.id,
            user_id=user.id,
            store_id=store.id,
            order_status="completed",
            payment_status="paid",
            fulfillment_status="received",
            after_sale_status="none",
            goods_amount=3000,
            freight_amount=0,
            payable_amount=3000,
            adjustment_amount=0,
            paid_amount=3000,
            refunded_amount=0,
            currency="CNY",
            policy_snapshot={},
            expires_at=now + timedelta(days=1),
            paid_at=now - timedelta(days=2),
            shipped_at=now - timedelta(days=1),
            completed_at=now - timedelta(hours=1),
        )
        session.add(review_order)
        await session.flush()
        review_items = [
            OrderItem(
                order_item_no=new_prefixed_ulid("oit_"),
                order_id=review_order.id,
                product_id=products[0].id,
                sku_id=sku.id,
                product_no=products[0].product_no,
                sku_no=sku.sku_no,
                product_name=products[0].product_name,
                sku_name=sku.sku_name,
                spec_snapshot=sku.spec_values,
                quantity=1,
                unit_price_amount=1000,
                market_price_amount=1200,
                gross_amount=1000,
                payable_amount=1000,
                adjustment_amount=0,
                refunded_quantity=0,
                refunded_amount=0,
                currency="CNY",
                review_status="reviewed",
                after_sale_status="none",
            )
            for _ in range(3)
        ]
        session.add_all(review_items)
        await session.flush()
        newest_review = Review(
            review_no=new_prefixed_ulid("rev_"),
            order_id=review_order.id,
            order_item_id=review_items[0].id,
            user_id=user.id,
            store_id=store.id,
            product_id=products[0].id,
            sku_id=sku.id,
            rating=5,
            content="公开评价内容",
            is_anonymous=False,
            review_status="published",
            moderation_status="passed",
            published_at=now - timedelta(seconds=1),
            helpful_count=2,
        )
        older_review = Review(
            review_no=new_prefixed_ulid("rev_"),
            order_id=review_order.id,
            order_item_id=review_items[1].id,
            user_id=user.id,
            store_id=store.id,
            product_id=products[0].id,
            sku_id=sku.id,
            rating=4,
            content="较早的匿名评价",
            is_anonymous=True,
            review_status="published",
            moderation_status="passed",
            published_at=now - timedelta(days=1),
            helpful_count=0,
        )
        hidden_review = Review(
            review_no=new_prefixed_ulid("rev_"),
            order_id=review_order.id,
            order_item_id=review_items[2].id,
            user_id=user.id,
            store_id=store.id,
            product_id=products[0].id,
            sku_id=sku.id,
            rating=1,
            content="不得出现在公开接口中的评价",
            is_anonymous=False,
            review_status="hidden",
            moderation_status="blocked",
            published_at=now - timedelta(days=2),
            hidden_at=now - timedelta(days=1),
            helpful_count=0,
        )
        session.add_all([newest_review, older_review, hidden_review])
        await session.flush()
        session.add_all(
            [
                ReviewAppendRecord(
                    append_no=new_prefixed_ulid("rpa_"),
                    review_id=newest_review.id,
                    user_id=user.id,
                    content="使用一周后的追评",
                    append_status="published",
                    moderation_status="passed",
                    published_at=now,
                ),
                ReviewReply(
                    review_id=newest_review.id,
                    store_id=store.id,
                    replier_user_id=user.id,
                    content="感谢您的认可。",
                    reply_status="published",
                    published_at=now,
                ),
            ]
        )
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

    homepage = await client.get("/api/v1/homepage", headers=auth)
    assert homepage.status_code == 200, homepage.text
    homepage_data = homepage.json()["data"]
    assert "categories" not in homepage_data
    assert [section["section"] for section in homepage_data["sections"]] == ["recommended"]
    assert homepage_data["sections"][0]["title"] == "为你推荐"
    homepage_product = homepage_data["sections"][0]["items"][0]
    assert "subtitle" not in homepage_product

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
    detail_data = detail.json()["data"]
    assert detail_data["store"]["store_id"] == store_no
    assert "subtitle" not in detail_data
    assert "description" not in detail_data
    assert "source_content" not in detail.text

    skus = await client.get(f"/api/v1/products/{product_no}/skus")
    assert skus.status_code == 200, skus.text
    public_sku = skus.json()["data"]["items"][0]
    assert public_sku["max_purchase_quantity"] == 15
    assert "market_price" not in public_sku
    assert "spec_values" not in public_sku

    reviews = await client.get(f"/api/v1/products/{product_no}/reviews", params={"limit": 1})
    assert reviews.status_code == 200, reviews.text
    review_payload = reviews.json()
    assert review_payload["data"]["summary"]["review_count"] == 2
    assert review_payload["data"]["summary"]["rating_distribution"]["5"] == 1
    assert review_payload["data"]["items"][0]["user_display_name"] != f"Catalog {suffix}"
    assert review_payload["data"]["items"][0]["append"]["content"] == "使用一周后的追评"
    assert review_payload["data"]["items"][0]["merchant_reply"]["content"] == "感谢您的认可。"
    review_cursor = review_payload["meta"]["pagination"]["next_cursor"]
    assert review_cursor
    second_review_page = await client.get(
        f"/api/v1/products/{product_no}/reviews",
        params={"limit": 1, "cursor": review_cursor},
    )
    assert second_review_page.status_code == 200
    assert second_review_page.json()["data"]["items"][0]["user_display_name"] == "匿名用户"
    mismatched_review_cursor = await client.get(
        f"/api/v1/products/{product_no}/reviews",
        params={"limit": 1, "rating": 5, "cursor": review_cursor},
    )
    assert mismatched_review_cursor.status_code == 400
    image_reviews = await client.get(
        f"/api/v1/products/{product_no}/reviews",
        params={"has_image": "true"},
    )
    assert image_reviews.status_code == 200
    assert image_reviews.json()["data"]["items"] == []
    assert "不得出现在公开接口中的评价" not in reviews.text

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


async def test_admin_taxonomy_and_inventory_adjustment_invariants(client: AsyncClient) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    password = f"Admin-Catalog-{suffix}-Correct-Horse!"

    async for session in mysql_session():
        provisioning = await provision_platform_super_admin(
            session,
            security,
            username=f"catalog_admin_{suffix}",
            password=password,
        )
        owner = await session.scalar(select(User).where(User.user_no == provisioning.user_no))
        assert owner is not None
        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            category_name=f"库存分类 {suffix}",
            category_code=f"inventory-{suffix}",
            path="/inventory",
            level=1,
            sort_order=1,
            category_status="active",
        )
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=owner.id,
            store_name=f"库存店铺 {suffix}",
            store_name_normalized=f"inventory-store-{suffix}",
            store_status="active",
            rating_score=Decimal("5.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=now,
        )
        session.add_all([category, store])
        await session.flush()
        product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=store.id,
            category_id=category.id,
            product_name=f"库存商品 {suffix}",
            product_status="draft",
            min_price_amount=1000,
            max_price_amount=1000,
            currency="CNY",
            sales_count=0,
            review_count=0,
            rating_score=Decimal("0.00"),
        )
        session.add(product)
        await session.flush()
        sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=product.id,
            store_id=store.id,
            merchant_sku_code=f"admin-{suffix}",
            sku_name="库存测试 SKU",
            spec_values=[{"name": "规格", "value": "测试"}],
            spec_signature=hashlib.sha256(f"admin-spec-{suffix}".encode()).digest(),
            sale_price_amount=1000,
            market_price_amount=1000,
            currency="CNY",
            sku_status="active",
        )
        session.add(sku)
        await session.flush()
        inventory = Inventory(
            sku_id=sku.id,
            on_hand_quantity=10,
            reserved_quantity=3,
            safety_stock_quantity=2,
            sold_quantity=0,
            inventory_status="active",
        )
        session.add(inventory)
        await session.commit()
        sku_no = sku.sku_no
        sku_internal_id = sku.id

    login = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": f"catalog_admin_{suffix}",
            "password": password,
            "client": {"client_type": "web", "device_name": "Catalog Admin Test"},
        },
    )
    assert login.status_code == 200, login.text
    challenge_id = login.json()["data"]["challenge_id"]
    mfa = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"catalog-mfa-{suffix}-001"},
        json={
            "challenge_id": challenge_id,
            "method": "totp",
            "code": pyotp.TOTP(provisioning.totp_secret).now(),
        },
    )
    assert mfa.status_code == 200, mfa.text
    token = mfa.json()["data"]["session"]["access_token"]
    admin_headers = {"Authorization": f"Bearer {token}"}

    root = await client.post(
        "/api/v1/admin/categories",
        headers={
            **admin_headers,
            "Idempotency-Key": f"category-root-{suffix}-001",
        },
        json={
            "parent_id": None,
            "category_name": f"根分类 {suffix}",
            "category_code": f"root-{suffix}",
            "sort_order": 1,
            "icon_file_id": None,
        },
    )
    assert root.status_code == 201, root.text
    root_id = root.json()["data"]["category_id"]

    child = await client.post(
        "/api/v1/admin/categories",
        headers={
            **admin_headers,
            "Idempotency-Key": f"category-child-{suffix}-01",
        },
        json={
            "parent_id": root_id,
            "category_name": f"子分类 {suffix}",
            "category_code": f"child-{suffix}",
            "sort_order": 1,
            "icon_file_id": None,
        },
    )
    assert child.status_code == 201, child.text
    child_id = child.json()["data"]["category_id"]

    cycle = await client.patch(
        f"/api/v1/admin/categories/{root_id}",
        headers={**admin_headers, "If-Match": root.headers["etag"]},
        json={"parent_id": child_id},
    )
    assert cycle.status_code == 409
    assert cycle.json()["code"] == "CATEGORY_CYCLE"

    brand = await client.post(
        "/api/v1/admin/brands",
        headers={
            **admin_headers,
            "Idempotency-Key": f"brand-create-{suffix}-0001",
        },
        json={
            "brand_name": f"管理品牌 {suffix}",
            "logo_file_id": None,
            "description": "管理端品牌测试",
        },
    )
    assert brand.status_code == 201, brand.text

    inventory_response = await client.get(
        f"/api/v1/admin/inventories/{sku_no}", headers=admin_headers
    )
    assert inventory_response.status_code == 200, inventory_response.text
    assert inventory_response.json()["data"]["available_quantity"] == 5

    adjustment_key = f"inventory-adjust-{suffix}-001"
    adjustment_payload = {
        "sku_id": sku_no,
        "on_hand_delta": 5,
        "reason_code": "STOCKTAKE_GAIN",
        "reason": "盘点发现库存增加",
        "reference_no": f"stocktake-{suffix}",
        "expected_version": inventory_response.json()["data"]["version"],
    }
    adjusted = await client.post(
        "/api/v1/admin/inventory-adjustments",
        headers={**admin_headers, "Idempotency-Key": adjustment_key},
        json=adjustment_payload,
    )
    assert adjusted.status_code == 200, adjusted.text
    adjusted_data = adjusted.json()["data"]
    assert adjusted_data["inventory"]["on_hand_quantity"] == 15
    assert adjusted_data["inventory"]["available_quantity"] == 10

    replayed = await client.post(
        "/api/v1/admin/inventory-adjustments",
        headers={**admin_headers, "Idempotency-Key": adjustment_key},
        json=adjustment_payload,
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["data"]["adjustment_id"] == adjusted_data["adjustment_id"]

    violating = await client.post(
        "/api/v1/admin/inventory-adjustments",
        headers={
            **admin_headers,
            "Idempotency-Key": f"inventory-negative-{suffix}-01",
        },
        json={
            **adjustment_payload,
            "on_hand_delta": -11,
            "expected_version": adjusted_data["inventory"]["version"],
        },
    )
    assert violating.status_code == 409
    assert violating.json()["code"] == "INVENTORY_ADJUSTMENT_WOULD_VIOLATE_RESERVATIONS"

    async for session in mysql_session():
        log_count = await session.scalar(
            select(func.count(InventoryLog.id)).where(InventoryLog.sku_id == sku_internal_id)
        )
        audit_count = await session.scalar(
            select(func.count(AdminOperationLog.id)).where(
                AdminOperationLog.target_type == "sku",
                AdminOperationLog.target_no == sku_no,
                AdminOperationLog.action == "adjust_inventory",
            )
        )
        event_count = await session.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.aggregate_type == "inventory",
                OutboxEvent.aggregate_no == sku_no,
                OutboxEvent.event_type == "inventory.adjusted.v1",
            )
        )
        assert log_count == 1
        assert audit_count == 1
        assert event_count == 1


async def test_admin_store_status_and_policy_lifecycle(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    password = f"Admin-Store-{suffix}-Correct-Horse!"

    async for session in mysql_session():
        provisioning = await provision_platform_super_admin(
            session,
            security,
            username=f"store_admin_{suffix}",
            password=password,
        )
        owner = await session.scalar(select(User).where(User.user_no == provisioning.user_no))
        assert owner is not None
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=owner.id,
            store_name=f"运营店铺 {suffix}",
            store_name_normalized=f"operations-store-{suffix}",
            description="店铺经营状态与政策生命周期集成测试",
            store_status="active",
            rating_score=Decimal("0.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
        )
        secondary_store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=owner.id,
            store_name=f"运营店铺备用 {suffix}",
            store_name_normalized=f"operations-store-secondary-{suffix}",
            description="用于管理端游标分页测试",
            store_status="active",
            rating_score=Decimal("0.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
        )
        session.add_all([store, secondary_store])
        await session.flush()

        await session.commit()
        store_no = store.store_no

    login = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": f"store_admin_{suffix}",
            "password": password,
            "client": {"client_type": "web", "device_name": "Store Admin Test"},
        },
    )
    assert login.status_code == 200, login.text
    mfa = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"store-mfa-{suffix}-0001"},
        json={
            "challenge_id": login.json()["data"]["challenge_id"],
            "method": "totp",
            "code": pyotp.TOTP(provisioning.totp_secret).now(),
        },
    )
    assert mfa.status_code == 200, mfa.text
    token = mfa.json()["data"]["session"]["access_token"]
    admin_headers = {"Authorization": f"Bearer {token}"}

    first_store_page = await client.get(
        "/api/v1/admin/stores",
        headers=admin_headers,
        params={"q": suffix, "limit": 1},
    )
    assert first_store_page.status_code == 200, first_store_page.text
    store_cursor = first_store_page.json()["data"]["next_cursor"]
    assert store_cursor
    second_store_page = await client.get(
        "/api/v1/admin/stores",
        headers=admin_headers,
        params={"q": suffix, "limit": 1, "cursor": store_cursor},
    )
    assert second_store_page.status_code == 200, second_store_page.text
    mismatched_store_cursor = await client.get(
        "/api/v1/admin/stores",
        headers=admin_headers,
        params={"q": f"different-{suffix}", "limit": 1, "cursor": store_cursor},
    )
    assert mismatched_store_cursor.status_code == 400
    assert mismatched_store_cursor.json()["code"] == "PAGINATION_CURSOR_INVALID"

    store_detail = await client.get(f"/api/v1/admin/stores/{store_no}", headers=admin_headers)
    assert store_detail.status_code == 200, store_detail.text
    suspend = await client.post(
        f"/api/v1/admin/stores/{store_no}/status-changes",
        headers={
            **admin_headers,
            "If-Match": store_detail.headers["etag"],
            "Idempotency-Key": f"store-suspend-{suffix}-001",
        },
        json={
            "action": "suspend",
            "reason_code": "OPERATIONAL_REVIEW",
            "reason": "运营复核期间暂停店铺。",
        },
    )
    assert suspend.status_code == 200, suspend.text
    resume = await client.post(
        f"/api/v1/admin/stores/{store_no}/status-changes",
        headers={
            **admin_headers,
            "If-Match": suspend.headers["etag"],
            "Idempotency-Key": f"store-resume-{suffix}-0001",
        },
        json={
            "action": "resume",
            "reason_code": "REVIEW_COMPLETED",
            "reason": "运营复核完成，恢复店铺。",
        },
    )
    assert resume.status_code == 200, resume.text
    assert resume.json()["data"]["status"] == "active"

    policy = await client.post(
        f"/api/v1/admin/stores/{store_no}/service-policies",
        headers={
            **admin_headers,
            "Idempotency-Key": f"policy-create-{suffix}-0001",
        },
        json={
            "policy_type": "shipping",
            "title": "发货政策草稿",
            "content": "订单将在四十八小时内发出。",
            "effective_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(days=30)).isoformat(),
        },
    )
    assert policy.status_code == 201, policy.text
    policy_id = policy.json()["data"]["policy_id"]

    updated_policy = await client.patch(
        f"/api/v1/admin/stores/{store_no}/service-policies/{policy_id}",
        headers={**admin_headers, "If-Match": policy.headers["etag"]},
        json={"title": "正式发货政策"},
    )
    assert updated_policy.status_code == 200, updated_policy.text
    published_policy = await client.post(
        f"/api/v1/admin/stores/{store_no}/service-policies/{policy_id}/publications",
        headers={
            **admin_headers,
            "If-Match": updated_policy.headers["etag"],
            "Idempotency-Key": f"policy-publish-{suffix}-001",
        },
        json={"reason": "政策内容审核通过。"},
    )
    assert published_policy.status_code == 200, published_policy.text
    assert published_policy.json()["data"]["status"] == "published"

    public_policies = await client.get(f"/api/v1/stores/{store_no}/service-policies")
    assert public_policies.status_code == 200, public_policies.text
    assert [item["policy_id"] for item in public_policies.json()["data"]["items"]] == [policy_id]

    immutable = await client.patch(
        f"/api/v1/admin/stores/{store_no}/service-policies/{policy_id}",
        headers={**admin_headers, "If-Match": published_policy.headers["etag"]},
        json={"title": "禁止原地修改"},
    )
    assert immutable.status_code == 409
    assert immutable.json()["code"] == "PUBLISHED_POLICY_IMMUTABLE"

    overlap = await client.post(
        f"/api/v1/admin/stores/{store_no}/service-policies",
        headers={
            **admin_headers,
            "Idempotency-Key": f"policy-overlap-{suffix}-001",
        },
        json={
            "policy_type": "shipping",
            "title": "时间窗重叠政策",
            "content": "该版本不应成功发布。",
            "effective_at": now.isoformat(),
            "expires_at": (now + timedelta(days=10)).isoformat(),
        },
    )
    assert overlap.status_code == 201, overlap.text
    overlap_id = overlap.json()["data"]["policy_id"]
    overlap_publish = await client.post(
        f"/api/v1/admin/stores/{store_no}/service-policies/{overlap_id}/publications",
        headers={
            **admin_headers,
            "If-Match": overlap.headers["etag"],
            "Idempotency-Key": f"policy-overlap-publish-{suffix}",
        },
        json={"reason": "验证重叠时间窗拦截。"},
    )
    assert overlap_publish.status_code == 409
    assert overlap_publish.json()["code"] == "POLICY_EFFECTIVE_WINDOW_OVERLAP"

    withdrawn = await client.post(
        f"/api/v1/admin/stores/{store_no}/service-policies/{policy_id}/withdrawals",
        headers={
            **admin_headers,
            "If-Match": published_policy.headers["etag"],
            "Idempotency-Key": f"policy-withdraw-{suffix}-01",
        },
        json={"reason": "发布新版前撤回当前政策。"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["data"]["status"] == "withdrawn"

    async for session in mysql_session():
        store_outbox = await session.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.aggregate_type == "store",
                OutboxEvent.aggregate_no == store_no,
            )
        )
        policy_outbox = await session.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.aggregate_type == "store_service_policy",
                OutboxEvent.aggregate_no == policy_id,
            )
        )
        assert store_outbox == 2
        assert policy_outbox == 2
