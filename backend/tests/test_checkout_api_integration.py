import hashlib
import os
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.modules.catalog.models import Category, Product, ProductFulfillmentProfile, ProductSku
from app.modules.checkout.models import CheckoutSession
from app.modules.identity.models import AuthSession, User, UserAddress
from app.modules.inventory.models import Inventory
from app.modules.stores.models import ShippingTemplate, ShippingTemplateRule, Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_checkout_snapshot_idempotency_etag_and_repricing(client: AsyncClient) -> None:
    suffix = secrets.token_hex(5)
    now, security = utc_now(), SecurityService(get_settings())
    async for session in mysql_session():
        user = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"checkout_{suffix}",
            username_normalized=f"checkout_{suffix}",
            nickname="Checkout",
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            permission_version=1,
            registered_at=now,
        )
        other_user = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"checkout_other_{suffix}",
            username_normalized=f"checkout_other_{suffix}",
            nickname="Other Checkout",
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            permission_version=1,
            registered_at=now,
        )
        session.add_all([user, other_user])
        await session.flush()
        auth_session = AuthSession(
            session_no=new_prefixed_ulid("ses_"),
            user_id=user.id,
            refresh_token_hash=security.keyed_hash("refresh-token", secrets.token_urlsafe()),
            token_family_no=new_prefixed_ulid("tfa_"),
            device_no=new_prefixed_ulid("dev_"),
            device_name="Checkout integration",
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
        address = UserAddress(
            address_no=new_prefixed_ulid("addr_"),
            user_id=user.id,
            recipient_name_ciphertext=security.encrypt("address-recipient", "测试用户"),
            phone_ciphertext=security.encrypt("address-phone", "+8613812345678"),
            phone_last4="5678",
            country_code="CN",
            province_code="CN-44",
            city_code="CN-4401",
            district_code="CN-440106",
            address_ciphertext=security.encrypt("address-detail", "测试路 1 号"),
            is_default=True,
            key_version=1,
        )
        other_address = UserAddress(
            address_no=new_prefixed_ulid("addr_"),
            user_id=other_user.id,
            recipient_name_ciphertext=security.encrypt("address-recipient", "其他用户"),
            phone_ciphertext=security.encrypt("address-phone", "+8613900000000"),
            phone_last4="0000",
            country_code="CN",
            province_code="CN-44",
            city_code="CN-4401",
            district_code="CN-440106",
            address_ciphertext=security.encrypt("address-detail", "不可访问地址"),
            is_default=True,
            key_version=1,
        )
        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            category_name="结算分类",
            category_code=f"checkout-{suffix}",
            path="/checkout",
            level=1,
            category_status="active",
        )
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=user.id,
            store_name="结算店铺",
            store_name_normalized=f"checkout-store-{suffix}",
            store_status="active",
            rating_score=Decimal("0.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=now,
        )
        session.add_all([auth_session, address, other_address, category, store])
        await session.flush()
        template = ShippingTemplate(
            template_no=new_prefixed_ulid("ship_"),
            template_family_no=new_prefixed_ulid("shf_"),
            store_id=store.id,
            template_name="标准快递",
            delivery_type="express",
            charge_mode="item",
            currency="CNY",
            template_status="effective",
            dispatch_min_hours=12,
            dispatch_max_hours=24,
            policy_version=1,
        )
        product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=store.id,
            category_id=category.id,
            product_name="结算商品",
            product_status="on_sale",
            min_price_amount=2500,
            max_price_amount=2500,
            currency="CNY",
            rating_score=Decimal("0.00"),
            published_at=now,
        )
        session.add_all([template, product])
        await session.flush()
        session.add(
            ShippingTemplateRule(
                shipping_template_id=template.id,
                region_scope={"include": ["CN"]},
                first_unit=1,
                additional_unit=1,
                first_fee_amount=800,
                additional_fee_amount=200,
                estimated_min_days=1,
                estimated_max_days=3,
                rule_status="active",
            )
        )
        session.add(
            ProductFulfillmentProfile(
                product_id=product.id,
                shipping_template_id=template.id,
                origin_region_code="CN-44",
                dispatch_min_hours=12,
                dispatch_max_hours=24,
                profile_version=1,
            )
        )
        sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=product.id,
            store_id=store.id,
            merchant_sku_code=f"CHECKOUT-{suffix}",
            sku_name="标准款",
            spec_values=[{"name": "规格", "value": "标准"}],
            spec_signature=hashlib.sha256(suffix.encode()).digest(),
            sale_price_amount=2500,
            market_price_amount=3000,
            currency="CNY",
            weight_grams=500,
            sku_status="active",
        )
        session.add(sku)
        await session.flush()
        session.add(Inventory(sku_id=sku.id, on_hand_quantity=10, inventory_status="active"))
        await session.commit()
        user_no, session_no, sku_no, address_no, other_address_no, store_no = (
            user.user_no,
            auth_session.session_no,
            sku.sku_no,
            address.address_no,
            other_address.address_no,
            store.store_no,
        )
    token, _ = security.create_access_token(
        user_no=user_no, session_no=session_no, audience="user", permission_version=1
    )
    auth = {"Authorization": f"Bearer {token}"}
    body = {"source": {"source_type": "buy_now", "sku_id": sku_no, "quantity": 2}}
    created = await client.post(
        "/api/v1/checkout-sessions",
        headers={**auth, "Idempotency-Key": f"checkout-{suffix}"},
        json=body,
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["address_id"] == address_no
    assert data["amounts"] == {
        "goods_amount": {"minor_units": "5000", "currency": "CNY"},
        "freight_amount": {"minor_units": "1000", "currency": "CNY"},
        "payable_amount": {"minor_units": "6000", "currency": "CNY"},
    }
    assert data["blocking_issues"] == []
    replay = await client.post(
        "/api/v1/checkout-sessions",
        headers={**auth, "Idempotency-Key": f"checkout-{suffix}"},
        json=body,
    )
    assert replay.status_code == 201 and replay.json()["data"] == data
    checkout_id = data["checkout_id"]
    foreign_address = await client.patch(
        f"/api/v1/checkout-sessions/{checkout_id}",
        headers={**auth, "If-Match": created.headers["etag"]},
        json={"address_id": other_address_no},
    )
    assert foreign_address.status_code == 404
    patched = await client.patch(
        f"/api/v1/checkout-sessions/{checkout_id}",
        headers={**auth, "If-Match": created.headers["etag"]},
        json={"buyer_remarks": [{"store_id": store_no, "content": "  请轻放\r\n谢谢  "}]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["store_groups"][0]["buyer_remark"] == "请轻放\n谢谢"
    stale = await client.patch(
        f"/api/v1/checkout-sessions/{checkout_id}",
        headers={**auth, "If-Match": created.headers["etag"]},
        json={"address_id": address_no},
    )
    assert stale.status_code == 412
    repriced = await client.post(
        f"/api/v1/checkout-sessions/{checkout_id}/repricings",
        headers={**auth, "Idempotency-Key": f"reprice-{suffix}"},
    )
    assert repriced.status_code == 200, repriced.text
    assert repriced.json()["data"]["pricing_version"] == 3

    expiring = await client.post(
        "/api/v1/checkout-sessions",
        headers={**auth, "Idempotency-Key": f"checkout-expire-{suffix}"},
        json=body,
    )
    assert expiring.status_code == 201
    expiring_id = expiring.json()["data"]["checkout_id"]
    async for session in mysql_session():
        row = await session.scalar(
            select(CheckoutSession).where(CheckoutSession.checkout_no == expiring_id)
        )
        assert row is not None
        row.expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()
    expired = await client.get(f"/api/v1/checkout-sessions/{expiring_id}", headers=auth)
    assert expired.status_code == 410
