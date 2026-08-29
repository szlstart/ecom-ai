import os
import secrets
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
from app.modules.catalog.models import Category, Product
from app.modules.identity.models import User
from app.modules.rbac.models import AdminOperationLog
from app.modules.stores.models import ShippingTemplate, Store, StoreFeaturedProduct
from app.modules.system.models import OutboxEvent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_store_operations_are_scoped_versioned_and_publicly_visible(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    password = f"Admin-StoreOps-{suffix}-Correct-Horse!"

    async for session in mysql_session():
        provisioning = await provision_platform_super_admin(
            session,
            security,
            username=f"store_ops_{suffix}",
            password=password,
        )
        owner = await session.scalar(select(User).where(User.user_no == provisioning.user_no))
        assert owner is not None
        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            category_name=f"运营分类 {suffix}",
            category_code=f"ops-{suffix}",
            path="/ops",
            level=1,
            sort_order=1,
            category_status="active",
        )
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=owner.id,
            store_name=f"运营店铺 {suffix}",
            store_name_normalized=f"ops-store-{suffix}",
            store_status="active",
            rating_score=Decimal("5.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=now,
        )
        other_store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=owner.id,
            store_name=f"其他店铺 {suffix}",
            store_name_normalized=f"other-store-{suffix}",
            store_status="active",
            rating_score=Decimal("5.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=now,
        )
        session.add_all([category, store, other_store])
        await session.flush()
        product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=store.id,
            category_id=category.id,
            product_name=f"运营商品 {suffix}",
            product_status="on_sale",
            min_price_amount=9900,
            max_price_amount=9900,
            currency="CNY",
            sales_count=0,
            review_count=0,
            rating_score=Decimal("5.00"),
            published_at=now,
        )
        foreign_product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=other_store.id,
            category_id=category.id,
            product_name=f"跨店商品 {suffix}",
            product_status="on_sale",
            min_price_amount=8800,
            max_price_amount=8800,
            currency="CNY",
            sales_count=0,
            review_count=0,
            rating_score=Decimal("5.00"),
            published_at=now,
        )
        session.add_all([product, foreign_product])
        await session.commit()
        store_db_id = store.id
        store_no = store.store_no
        product_no = product.product_no
        foreign_product_no = foreign_product.product_no

    login = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": f"store_ops_{suffix}",
            "password": password,
            "client": {"client_type": "web", "device_name": "Store Operations Test"},
        },
    )
    assert login.status_code == 200, login.text
    mfa = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"store-ops-mfa-{suffix}"},
        json={
            "challenge_id": login.json()["data"]["challenge_id"],
            "method": "totp",
            "code": pyotp.TOTP(provisioning.totp_secret).now(),
        },
    )
    assert mfa.status_code == 200, mfa.text
    auth = {"Authorization": f"Bearer {mfa.json()['data']['session']['access_token']}"}

    group = await client.post(
        f"/api/v1/admin/stores/{store_no}/product-groups",
        headers={**auth, "Idempotency-Key": f"group-{suffix}"},
        json={"group_name": "本周精选", "sort_order": 1},
    )
    assert group.status_code == 201, group.text
    group_id = group.json()["data"]["group_id"]
    cross_store = await client.put(
        f"/api/v1/admin/stores/{store_no}/product-groups/{group_id}/products",
        headers={**auth, "If-Match": group.headers["etag"]},
        json={"product_ids": [foreign_product_no]},
    )
    assert cross_store.status_code == 422
    assert cross_store.json()["code"] == "STORE_PRODUCT_SCOPE_INVALID"
    grouped = await client.put(
        f"/api/v1/admin/stores/{store_no}/product-groups/{group_id}/products",
        headers={**auth, "If-Match": group.headers["etag"]},
        json={"product_ids": [product_no]},
    )
    assert grouped.status_code == 200, grouped.text
    assert grouped.json()["data"]["product_ids"] == [product_no]

    template = await client.post(
        f"/api/v1/admin/stores/{store_no}/shipping-templates",
        headers={**auth, "Idempotency-Key": f"shipping-{suffix}-v1"},
        json={
            "template_name": "全国基础运费",
            "delivery_type": "express",
            "charge_mode": "by_item",
            "currency": "CNY",
            "dispatch_min_hours": 12,
            "dispatch_max_hours": 48,
            "rules": [
                {
                    "region_scope": {"include": ["CN"]},
                    "first_unit": 1,
                    "additional_unit": 1,
                    "first_fee_amount": 800,
                    "additional_fee_amount": 200,
                    "estimated_min_days": 1,
                    "estimated_max_days": 5,
                }
            ],
        },
    )
    assert template.status_code == 201, template.text
    template_id = template.json()["data"]["template_id"]
    family_id = template.json()["data"]["template_family_id"]
    updated_template = await client.patch(
        f"/api/v1/admin/stores/{store_no}/shipping-templates/{template_id}",
        headers={**auth, "If-Match": template.headers["etag"]},
        json={"template_name": "全国标准运费"},
    )
    assert updated_template.status_code == 200, updated_template.text
    published_template = await client.post(
        f"/api/v1/admin/stores/{store_no}/shipping-templates/{template_id}/publications",
        headers={
            **auth,
            "If-Match": updated_template.headers["etag"],
            "Idempotency-Key": f"shipping-publish-{suffix}-v1",
        },
        json={"reason": "运费规则审核通过。"},
    )
    assert published_template.status_code == 200, published_template.text
    assert published_template.json()["data"]["status"] == "effective"
    immutable = await client.patch(
        f"/api/v1/admin/stores/{store_no}/shipping-templates/{template_id}",
        headers={**auth, "If-Match": published_template.headers["etag"]},
        json={"template_name": "禁止修改"},
    )
    assert immutable.status_code == 409
    assert immutable.json()["code"] == "SHIPPING_TEMPLATE_IMMUTABLE"

    revision = await client.post(
        f"/api/v1/admin/stores/{store_no}/shipping-templates",
        headers={**auth, "Idempotency-Key": f"shipping-{suffix}-v2"},
        json={
            "template_family_id": family_id,
            "template_name": "全国标准运费 v2",
            "delivery_type": "express",
            "charge_mode": "fixed",
            "currency": "CNY",
            "dispatch_min_hours": 6,
            "dispatch_max_hours": 24,
            "rules": [
                {
                    "region_scope": {"include": ["CN"]},
                    "first_unit": 1,
                    "additional_unit": 1,
                    "first_fee_amount": 500,
                    "additional_fee_amount": 0,
                    "estimated_min_days": 1,
                    "estimated_max_days": 3,
                }
            ],
        },
    )
    assert revision.status_code == 201, revision.text
    assert revision.json()["data"]["policy_version"] == 2
    published_revision = await client.post(
        f"/api/v1/admin/stores/{store_no}/shipping-templates/"
        f"{revision.json()['data']['template_id']}/publications",
        headers={
            **auth,
            "If-Match": revision.headers["etag"],
            "Idempotency-Key": f"shipping-publish-{suffix}-v2",
        },
        json={"reason": "启用新版运费。"},
    )
    assert published_revision.status_code == 200, published_revision.text

    announcement = await client.post(
        f"/api/v1/admin/stores/{store_no}/announcements",
        headers={**auth, "Idempotency-Key": f"announcement-{suffix}"},
        json={
            "title": "新品到店",
            "content": "本周新品已经上架。",
            "status": "draft",
            "sort_order": 1,
        },
    )
    assert announcement.status_code == 201, announcement.text
    published_announcement = await client.patch(
        f"/api/v1/admin/stores/{store_no}/announcements/"
        f"{announcement.json()['data']['announcement_id']}",
        headers={**auth, "If-Match": announcement.headers["etag"]},
        json={"status": "published"},
    )
    assert published_announcement.status_code == 200, published_announcement.text

    store_detail = await client.get(f"/api/v1/admin/stores/{store_no}", headers=auth)
    featured = await client.put(
        f"/api/v1/admin/stores/{store_no}/featured-products",
        headers={**auth, "If-Match": store_detail.headers["etag"]},
        json={"slot_type": "recommended", "items": [{"product_id": product_no}]},
    )
    assert featured.status_code == 200, featured.text
    assert featured.json()["data"][0]["product_id"] == product_no

    public_groups = await client.get(f"/api/v1/stores/{store_no}/product-groups")
    assert public_groups.status_code == 200, public_groups.text
    assert public_groups.json()["data"]["items"][0]["visible_product_count"] == 1
    home = await client.get(f"/api/v1/stores/{store_no}/home-content")
    assert home.status_code == 200, home.text
    assert home.json()["data"]["announcements"][0]["title"] == "新品到店"
    assert home.json()["data"]["recommended_products"][0]["product_id"] == product_no

    async for session in mysql_session():
        effective_count = await session.scalar(
            select(func.count(ShippingTemplate.id)).where(
                ShippingTemplate.store_id == store_db_id,
                ShippingTemplate.template_family_no == family_id,
                ShippingTemplate.template_status == "effective",
            )
        )
        featured_count = await session.scalar(
            select(func.count(StoreFeaturedProduct.id)).where(
                StoreFeaturedProduct.store_id == store_db_id,
                StoreFeaturedProduct.slot_type == "recommended",
            )
        )
        audit_count = await session.scalar(
            select(func.count(AdminOperationLog.id)).where(
                AdminOperationLog.scope_type == "store",
                AdminOperationLog.scope_id == store_db_id,
            )
        )
        outbox_count = await session.scalar(
            select(func.count(OutboxEvent.id)).where(OutboxEvent.aggregate_no == store_no)
        )
        assert effective_count == 1
        assert featured_count == 1
        assert audit_count is not None and audit_count >= 9
        assert outbox_count is not None and outbox_count >= 1
