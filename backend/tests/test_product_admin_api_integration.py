import hashlib
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
from app.modules.catalog.models import Category, Product, ProductStatusLog
from app.modules.files.models import FileObject
from app.modules.identity.models import User
from app.modules.stores.models import ShippingTemplate, Store
from app.modules.system.models import OutboxEvent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_product_draft_review_publish_and_off_shelf_lifecycle(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    password = f"Admin-Product-{suffix}-Correct-Horse!"

    async for session in mysql_session():
        provisioning = await provision_platform_super_admin(
            session,
            security,
            username=f"product_admin_{suffix}",
            password=password,
        )
        owner = await session.scalar(select(User).where(User.user_no == provisioning.user_no))
        assert owner is not None
        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            category_name=f"商品分类 {suffix}",
            category_code=f"product-{suffix}",
            path="/product",
            level=1,
            sort_order=1,
            category_status="active",
        )
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=owner.id,
            store_name=f"商品店铺 {suffix}",
            store_name_normalized=f"product-store-{suffix}",
            store_status="active",
            rating_score=Decimal("0.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=now,
        )
        session.add_all([category, store])
        await session.flush()
        template = ShippingTemplate(
            template_no=new_prefixed_ulid("sht_"),
            template_family_no=new_prefixed_ulid("sht_"),
            store_id=store.id,
            template_name="免邮模板",
            delivery_type="standard",
            charge_mode="free",
            currency="CNY",
            template_status="effective",
            dispatch_min_hours=12,
            dispatch_max_hours=48,
            policy_version=1,
        )
        image = FileObject(
            file_no=new_prefixed_ulid("file_"),
            bucket="public-assets",
            object_key=f"products/{store.store_no}/main.webp",
            purpose="product",
            owner_type="store",
            owner_no=store.store_no,
            declared_mime_type="image/webp",
            detected_mime_type="image/webp",
            size_bytes=4096,
            sha256=hashlib.sha256(f"image-{suffix}".encode()).digest(),
            width=1200,
            height=1200,
            visibility="public_derivative",
            sensitivity_level="S1",
            scan_status="safe",
            file_status="active",
            activated_at=now,
        )
        session.add_all([template, image])
        await session.commit()
        store_no = store.store_no
        category_no = category.category_no
        template_no = template.template_no
        image_no = image.file_no

    login = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": f"product_admin_{suffix}",
            "password": password,
            "client": {"client_type": "web", "device_name": "Product Admin Test"},
        },
    )
    assert login.status_code == 200, login.text
    mfa = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"product-mfa-{suffix}-001"},
        json={
            "challenge_id": login.json()["data"]["challenge_id"],
            "method": "totp",
            "code": pyotp.TOTP(provisioning.totp_secret).now(),
        },
    )
    assert mfa.status_code == 200, mfa.text
    auth = {"Authorization": f"Bearer {mfa.json()['data']['session']['access_token']}"}

    created = await client.post(
        "/api/v1/admin/products",
        headers={**auth, "Idempotency-Key": f"product-create-{suffix}-01"},
        json={
            "store_id": store_no,
            "category_id": category_no,
            "brand_id": None,
            "product_name": f"企业级测试商品 {suffix}",
            "subtitle": "完整商品生命周期",
            "description": "安全纯文本摘要",
        },
    )
    assert created.status_code == 201, created.text
    product_id = created.json()["data"]["product_id"]

    incomplete = await client.post(
        f"/api/v1/admin/products/{product_id}/review-submissions",
        headers={
            **auth,
            "If-Match": created.headers["etag"],
            "Idempotency-Key": f"product-submit-incomplete-{suffix}",
        },
        json={"reason_code": "READY_FOR_REVIEW", "reason": "尝试提交不完整商品。"},
    )
    assert incomplete.status_code == 409
    assert incomplete.json()["code"] == "PRODUCT_INCOMPLETE"

    sku = await client.post(
        f"/api/v1/admin/products/{product_id}/skus",
        headers={**auth, "Idempotency-Key": f"product-sku-{suffix}-0001"},
        json={
            "merchant_sku_code": f"SKU-{suffix}",
            "sku_name": "标准款",
            "spec_values": [{"name": "规格", "value": "标准"}],
            "sale_price_amount": 12900,
            "market_price_amount": 15900,
            "currency": "CNY",
            "weight_grams": 500,
            "barcode": None,
        },
    )
    assert sku.status_code == 201, sku.text

    product = await client.get(f"/api/v1/admin/products/{product_id}", headers=auth)
    assert product.status_code == 200, product.text
    images = await client.put(
        f"/api/v1/admin/products/{product_id}/images",
        headers={**auth, "If-Match": product.headers["etag"]},
        json={
            "items": [
                {
                    "file_id": image_no,
                    "sku_id": None,
                    "image_type": "main",
                    "alt_text": "商品主图",
                    "sort_order": 0,
                }
            ]
        },
    )
    assert images.status_code == 200, images.text

    fulfillment = await client.put(
        f"/api/v1/admin/products/{product_id}/fulfillment-profile",
        headers={**auth, "If-Match": images.headers["etag"]},
        json={
            "shipping_template_id": template_no,
            "origin_region_code": "CN_310000",
            "dispatch_min_hours": 12,
            "dispatch_max_hours": 48,
            "purchase_notice": "支持七天无理由退货。",
        },
    )
    assert fulfillment.status_code == 200, fulfillment.text

    fulfillment_read = await client.get(
        f"/api/v1/admin/products/{product_id}/fulfillment-profile",
        headers=auth,
    )
    assert fulfillment_read.status_code == 200, fulfillment_read.text
    assert fulfillment_read.json()["data"]["shipping_template_id"] == template_no
    assert fulfillment_read.json()["data"]["dispatch_max_hours"] == 48

    unsafe = await client.post(
        f"/api/v1/admin/products/{product_id}/detail-content-versions",
        headers={**auth, "Idempotency-Key": f"product-content-unsafe-{suffix}"},
        json={"source_format": "html", "source_content": "<script>alert(1)</script>"},
    )
    assert unsafe.status_code == 422
    assert unsafe.json()["code"] == "PRODUCT_CONTENT_UNSAFE"

    content = await client.post(
        f"/api/v1/admin/products/{product_id}/detail-content-versions",
        headers={**auth, "Idempotency-Key": f"product-content-{suffix}-01"},
        json={
            "source_format": "html",
            "source_content": "<h2>商品详情</h2><p><strong>安全</strong>且耐用。</p>",
        },
    )
    assert content.status_code == 201, content.text
    assert "source_content" in content.json()["data"]

    attributes = await client.put(
        f"/api/v1/admin/products/{product_id}/attributes",
        headers={**auth, "If-Match": content.headers["etag"]},
        json={
            "items": [
                {
                    "attribute_code": "material",
                    "attribute_name": "材质",
                    "value_text": "铝合金",
                    "value_normalized": "aluminum",
                    "unit": None,
                    "is_searchable": True,
                    "sort_order": 1,
                }
            ]
        },
    )
    assert attributes.status_code == 200, attributes.text

    faq = await client.post(
        f"/api/v1/admin/products/{product_id}/faqs",
        headers={**auth, "Idempotency-Key": f"product-faq-{suffix}-0001"},
        json={
            "question": "多久发货?",
            "sort_order": 1,
            "source_format": "plain_text",
            "source_content": "付款后四十八小时内发货。",
        },
    )
    assert faq.status_code == 201, faq.text
    faq_data = faq.json()["data"]
    published_faq = await client.post(
        f"/api/v1/admin/products/{product_id}/faqs/{faq_data['faq_id']}/publications",
        headers={**auth, "Idempotency-Key": f"product-faq-publish-{suffix}"},
        json={"version_id": faq_data["current_version_id"], "reason": "FAQ 审核通过。"},
    )
    assert published_faq.status_code == 200, published_faq.text

    ready = await client.get(f"/api/v1/admin/products/{product_id}", headers=auth)
    assert ready.status_code == 200, ready.text
    assert ready.json()["data"]["completeness"]["missing_requirements"] == []
    submitted = await client.post(
        f"/api/v1/admin/products/{product_id}/review-submissions",
        headers={
            **auth,
            "If-Match": ready.headers["etag"],
            "Idempotency-Key": f"product-submit-{suffix}-0001",
        },
        json={"reason_code": "READY_FOR_REVIEW", "reason": "商品资料已经完整。"},
    )
    assert submitted.status_code == 200, submitted.text
    approved = await client.post(
        f"/api/v1/admin/products/{product_id}/moderation-decisions",
        headers={
            **auth,
            "If-Match": submitted.headers["etag"],
            "Idempotency-Key": f"product-approve-{suffix}-001",
        },
        json={
            "decision": "approve",
            "reason_code": "CONTENT_APPROVED",
            "reason": "商品内容和履约资料审核通过。",
        },
    )
    assert approved.status_code == 200, approved.text
    published = await client.post(
        f"/api/v1/admin/products/{product_id}/publications",
        headers={
            **auth,
            "If-Match": approved.headers["etag"],
            "Idempotency-Key": f"product-publish-{suffix}-001",
        },
        json={"reason_code": "PUBLISH_APPROVED", "reason": "批准商品正式上架。"},
    )
    assert published.status_code == 200, published.text
    assert published.json()["data"]["status"] == "on_sale"

    public_detail = await client.get(f"/api/v1/products/{product_id}")
    assert public_detail.status_code == 200, public_detail.text
    assert "source_content" not in public_detail.text
    assert public_detail.json()["data"]["detail_content"]["safe_html"]
    public_faqs = await client.get(f"/api/v1/products/{product_id}/faqs")
    assert public_faqs.status_code == 200, public_faqs.text
    assert public_faqs.json()["data"]["items"][0]["question"] == "多久发货?"

    edit_on_sale = await client.patch(
        f"/api/v1/admin/products/{product_id}",
        headers={**auth, "If-Match": published.headers["etag"]},
        json={"product_name": f"在售商品就地编辑 {suffix}"},
    )
    assert edit_on_sale.status_code == 200, edit_on_sale.text
    assert edit_on_sale.json()["data"]["status"] == "on_sale"
    edited_public_detail = await client.get(f"/api/v1/products/{product_id}")
    assert edited_public_detail.status_code == 200, edited_public_detail.text
    assert edited_public_detail.json()["data"]["product_name"] == f"在售商品就地编辑 {suffix}"

    off_shelf = await client.post(
        f"/api/v1/admin/products/{product_id}/off-shelf-commands",
        headers={
            **auth,
            "If-Match": edit_on_sale.headers["etag"],
            "Idempotency-Key": f"product-off-shelf-{suffix}",
        },
        json={"reason_code": "MERCHANT_REQUEST", "reason": "商家主动下架商品。"},
    )
    assert off_shelf.status_code == 200, off_shelf.text
    assert off_shelf.json()["data"]["status"] == "off_shelf"

    deleted = await client.delete(
        f"/api/v1/admin/products/{product_id}",
        headers={
            **auth,
            "If-Match": off_shelf.headers["etag"],
            "Idempotency-Key": f"product-delete-{suffix}",
        },
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["previous_status"] == "off_shelf"
    assert deleted.json()["data"]["deleted_at"]
    assert (await client.get(f"/api/v1/products/{product_id}")).status_code == 404
    assert (
        await client.get(f"/api/v1/admin/products/{product_id}", headers=auth)
    ).status_code == 404

    async for session in mysql_session():
        status_count = await session.scalar(
            select(func.count(ProductStatusLog.id))
            .join(Product, Product.id == ProductStatusLog.product_id)
            .where(Product.product_no == product_id)
        )
        product_event_count = await session.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.aggregate_type == "product",
                OutboxEvent.aggregate_no == product_id,
            )
        )
        assert status_count is not None and status_count >= 5
        assert product_event_count is not None and product_event_count >= 10
        deleted_product = await session.scalar(
            select(Product).where(Product.product_no == product_id)
        )
        assert deleted_product is not None and deleted_product.deleted_at is not None
