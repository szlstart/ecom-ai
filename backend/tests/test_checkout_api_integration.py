import asyncio
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthContext
from app.bootstrap.admin import provision_platform_super_admin
from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, TokenClaims, utc_now
from app.database.mysql import mysql_session
from app.modules.after_sale.models import (
    RefundAppeal,
    RefundAppealEvent,
    RefundApplication,
    RefundPaymentRecord,
)
from app.modules.after_sale.schemas import (
    AdminRefundAppealDecisionRequest,
    AdminRefundDecisionRequest,
)
from app.modules.after_sale.service import AfterSaleService
from app.modules.cart.models import CartItem
from app.modules.catalog.models import Category, Product, ProductFulfillmentProfile, ProductSku
from app.modules.checkout.models import CheckoutSession
from app.modules.files.models import FileObject
from app.modules.identity.models import AuthSession, User, UserAddress
from app.modules.inventory.models import Inventory, InventoryLog, InventoryReservation
from app.modules.logistics.models import LogisticsSyncLog, Shipment
from app.modules.logistics.schemas import (
    AdminShipmentCreateItem,
    AdminShipmentCreateRequest,
    AdminShipmentVoidRequest,
    AdminTrackingCorrectionRequest,
)
from app.modules.logistics.service import LogisticsService
from app.modules.orders.models import Order, OrderAddress, OrderItem, TradeOrder
from app.modules.orders.service import OrderService
from app.modules.payments.models import Payment, PaymentCallback
from app.modules.payments.service import PaymentService
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.models import AdminApprovalRequest, Permission
from app.modules.rbac.schemas import ApprovalDecisionRequest, ApprovalRequiredView
from app.modules.rbac.service import RbacService
from app.modules.reviews.models import (
    Review,
    ReviewAppendImage,
    ReviewAppendRecord,
    ReviewGovernanceRecord,
    ReviewImage,
    ReviewRevisionRecord,
)
from app.modules.reviews.schemas import AdminReviewModerationRequest, AdminReviewReplyRequest
from app.modules.reviews.service import ReviewService
from app.modules.stores.models import ShippingTemplate, ShippingTemplateRule, Store
from app.modules.system.models import OutboxEvent
from app.workers.admin_approval_worker import AdminApprovalWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def _admin_access(
    session: AsyncSession,
    user_no: str,
    session_no: str,
    *,
    permission_code: str,
) -> AdminAccess:
    user = await session.scalar(select(User).where(User.user_no == user_no))
    auth_session = await session.scalar(
        select(AuthSession).where(AuthSession.session_no == session_no)
    )
    permission = await session.scalar(
        select(Permission).where(Permission.permission_code == permission_code)
    )
    assert user is not None and auth_session is not None and permission is not None
    return AdminAccess(
        context=AuthContext(
            user=user,
            session=auth_session,
            claims=TokenClaims(
                subject=user.user_no,
                session_id=auth_session.session_no,
                audience="admin",
                permission_version=user.permission_version,
                expires_at=auth_session.expires_at,
            ),
        ),
        permission=permission,
        scopes=(("platform", 0),),
    )


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
        other_auth_session = AuthSession(
            session_no=new_prefixed_ulid("ses_"),
            user_id=other_user.id,
            refresh_token_hash=security.keyed_hash("refresh-token", secrets.token_urlsafe()),
            token_family_no=new_prefixed_ulid("tfa_"),
            device_no=new_prefixed_ulid("dev_"),
            device_name="Other checkout integration",
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
        session.add(other_auth_session)
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
        second_store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=user.id,
            store_name="第二结算店铺",
            store_name_normalized=f"checkout-store-second-{suffix}",
            store_status="active",
            rating_score=Decimal("0.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=now,
        )
        session.add_all([auth_session, address, other_address, category, store])
        session.add(second_store)
        await session.flush()
        template = ShippingTemplate(
            template_no=new_prefixed_ulid("sht_"),
            template_family_no=new_prefixed_ulid("sht_"),
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
        second_template = ShippingTemplate(
            template_no=new_prefixed_ulid("sht_"),
            template_family_no=new_prefixed_ulid("sht_"),
            store_id=second_store.id,
            template_name="第二店铺快递",
            delivery_type="express",
            charge_mode="item",
            currency="CNY",
            template_status="effective",
            dispatch_min_hours=12,
            dispatch_max_hours=24,
            policy_version=1,
        )
        second_product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=second_store.id,
            category_id=category.id,
            product_name="第二结算商品",
            product_status="on_sale",
            min_price_amount=4000,
            max_price_amount=4000,
            currency="CNY",
            rating_score=Decimal("0.00"),
            published_at=now,
        )
        session.add_all([template, product, second_template, second_product])
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
            ShippingTemplateRule(
                shipping_template_id=second_template.id,
                region_scope={"include": ["CN"]},
                first_unit=1,
                additional_unit=1,
                first_fee_amount=500,
                additional_fee_amount=100,
                estimated_min_days=1,
                estimated_max_days=2,
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
        session.add(
            ProductFulfillmentProfile(
                product_id=second_product.id,
                shipping_template_id=second_template.id,
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
        second_sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=second_product.id,
            store_id=second_store.id,
            merchant_sku_code=f"CHECKOUT-SECOND-{suffix}",
            sku_name="第二标准款",
            spec_values=[{"name": "规格", "value": "第二标准"}],
            spec_signature=hashlib.sha256(f"second-{suffix}".encode()).digest(),
            sale_price_amount=4000,
            market_price_amount=4500,
            currency="CNY",
            weight_grams=500,
            sku_status="active",
        )
        session.add(second_sku)
        await session.flush()
        session.add_all(
            [
                Inventory(sku_id=sku.id, on_hand_quantity=10, inventory_status="active"),
                Inventory(sku_id=second_sku.id, on_hand_quantity=10, inventory_status="active"),
            ]
        )
        await session.commit()
        (
            user_no,
            other_user_no,
            session_no,
            other_session_no,
            sku_no,
            second_sku_no,
            address_no,
            other_address_no,
            store_no,
        ) = (
            user.user_no,
            other_user.user_no,
            auth_session.session_no,
            other_auth_session.session_no,
            sku.sku_no,
            second_sku.sku_no,
            address.address_no,
            other_address.address_no,
            store.store_no,
        )
    token, _ = security.create_access_token(
        user_no=user_no, session_no=session_no, audience="user", permission_version=1
    )
    auth = {"Authorization": f"Bearer {token}"}
    other_token, _ = security.create_access_token(
        user_no=other_user_no,
        session_no=other_session_no,
        audience="user",
        permission_version=1,
    )
    other_auth = {"Authorization": f"Bearer {other_token}"}
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
    assert repriced.json()["data"]["pricing_version"] == "pricing_v1"
    assert repriced.json()["data"]["version"] == 2

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
    expired_order_payload = {
        "checkout_id": expiring_id,
        "checkout_version": expiring.json()["data"]["version"],
    }
    for _ in range(2):
        expired_order = await client.post(
            "/api/v1/orders",
            headers={**auth, "Idempotency-Key": f"order-expired-{suffix}"},
            json=expired_order_payload,
        )
        assert expired_order.status_code == 410
        assert expired_order.json()["code"] == "CHECKOUT_EXPIRED"

    changing = await client.post(
        "/api/v1/checkout-sessions",
        headers={**auth, "Idempotency-Key": f"checkout-price-change-{suffix}"},
        json={"source": {"source_type": "buy_now", "sku_id": sku_no, "quantity": 1}},
    )
    assert changing.status_code == 201
    changing_data = changing.json()["data"]
    async for session in mysql_session():
        changed_sku = await session.scalar(select(ProductSku).where(ProductSku.sku_no == sku_no))
        assert changed_sku is not None
        changed_sku.sale_price_amount = 2600
        changed_sku.version += 1
        await session.commit()
    price_changed_order = await client.post(
        "/api/v1/orders",
        headers={**auth, "Idempotency-Key": f"order-price-change-{suffix}"},
        json={
            "checkout_id": changing_data["checkout_id"],
            "checkout_version": changing_data["version"],
        },
    )
    assert price_changed_order.status_code == 412
    assert price_changed_order.json()["code"] == "CHECKOUT_VERSION_MISMATCH"
    async for session in mysql_session():
        assert (
            await session.scalar(
                select(TradeOrder).where(
                    TradeOrder.checkout_no_snapshot == changing_data["checkout_id"]
                )
            )
            is None
        )
        changed_sku = await session.scalar(select(ProductSku).where(ProductSku.sku_no == sku_no))
        assert changed_sku is not None
        changed_sku.sale_price_amount = 2500
        changed_sku.version += 1
        await session.commit()

    first_cart = await client.post(
        "/api/v1/users/me/cart/items",
        headers={**auth, "Idempotency-Key": f"cart-first-{suffix}"},
        json={"sku_id": sku_no, "quantity": 1},
    )
    assert first_cart.status_code == 200, first_cart.text
    second_cart = await client.post(
        "/api/v1/users/me/cart/items",
        headers={**auth, "Idempotency-Key": f"cart-second-{suffix}"},
        json={"sku_id": second_sku_no, "quantity": 1},
    )
    assert second_cart.status_code == 200, second_cart.text
    cart_item_ids = [
        item["cart_item_id"]
        for group in second_cart.json()["data"]["groups"]
        for item in group["items"]
    ]
    cart_checkout = await client.post(
        "/api/v1/checkout-sessions",
        headers={**auth, "Idempotency-Key": f"cart-checkout-{suffix}"},
        json={"source": {"source_type": "cart", "cart_item_ids": cart_item_ids}},
    )
    assert cart_checkout.status_code == 201, cart_checkout.text
    cart_checkout_data = cart_checkout.json()["data"]
    order_payload = {
        "checkout_id": cart_checkout_data["checkout_id"],
        "checkout_version": cart_checkout_data["version"],
    }
    attempts = await asyncio.gather(
        client.post(
            "/api/v1/orders",
            headers={**auth, "Idempotency-Key": f"order-winner-a-{suffix}"},
            json=order_payload,
        ),
        client.post(
            "/api/v1/orders",
            headers={**auth, "Idempotency-Key": f"order-winner-b-{suffix}"},
            json=order_payload,
        ),
    )
    assert sorted(response.status_code for response in attempts) == [201, 409]
    created_order = next(response for response in attempts if response.status_code == 201)
    losing_order = next(response for response in attempts if response.status_code == 409)
    assert losing_order.json()["code"] == "CHECKOUT_ALREADY_CONSUMED"
    order_data = created_order.json()["data"]
    assert len(order_data["order_ids"]) == 2
    winner_key = (
        f"order-winner-a-{suffix}" if attempts[0].status_code == 201 else f"order-winner-b-{suffix}"
    )
    replayed_order = await client.post(
        "/api/v1/orders",
        headers={**auth, "Idempotency-Key": winner_key},
        json=order_payload,
    )
    assert replayed_order.status_code == 201
    assert replayed_order.json()["data"] == order_data

    async for session in mysql_session():
        trade = await session.scalar(
            select(TradeOrder).where(TradeOrder.trade_no == order_data["trade_order_id"])
        )
        assert trade is not None
        assert trade.order_count == 2
        assert (trade.goods_amount, trade.freight_amount, trade.payable_amount) == (
            6500,
            1300,
            7800,
        )
        orders = list(
            (await session.scalars(select(Order).where(Order.trade_order_id == trade.id))).all()
        )
        assert len(orders) == 2
        assert (
            len(
                list(
                    (
                        await session.scalars(
                            select(OrderItem).where(
                                OrderItem.order_id.in_([item.id for item in orders])
                            )
                        )
                    ).all()
                )
            )
            == 2
        )
        assert (
            len(
                list(
                    (
                        await session.scalars(
                            select(OrderAddress).where(
                                OrderAddress.order_id.in_([item.id for item in orders])
                            )
                        )
                    ).all()
                )
            )
            == 2
        )
        reservations = list(
            (
                await session.scalars(
                    select(InventoryReservation).where(
                        InventoryReservation.order_id.in_([item.id for item in orders])
                    )
                )
            ).all()
        )
        assert len(reservations) == 2
        assert all(item.reservation_status == "active" for item in reservations)
        inventory_rows = list(
            (
                await session.scalars(
                    select(Inventory).where(
                        Inventory.sku_id.in_([item.sku_id for item in reservations])
                    )
                )
            ).all()
        )
        assert sorted(item.reserved_quantity for item in inventory_rows) == [1, 1]
        logs = list(
            (
                await session.scalars(
                    select(InventoryLog).where(
                        InventoryLog.reference_no.in_(order_data["order_ids"])
                    )
                )
            ).all()
        )
        assert len(logs) == 2 and all(item.operation_type == "reserve" for item in logs)
        consumed_checkout = await session.scalar(
            select(CheckoutSession).where(
                CheckoutSession.checkout_no == cart_checkout_data["checkout_id"]
            )
        )
        assert consumed_checkout is not None
        assert consumed_checkout.checkout_status == "submitted"
        purchased_cart_items = list(
            (
                await session.scalars(
                    select(CartItem).where(CartItem.cart_item_no.in_(cart_item_ids))
                )
            ).all()
        )
        assert len(purchased_cart_items) == 2
        assert all(not item.is_selected for item in purchased_cart_items)
        outbox = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_type == "trade_order",
                OutboxEvent.aggregate_no == trade.trade_no,
            )
        )
        assert outbox is not None and outbox.event_type == "order.created.v1"

    first_page = await client.get(
        "/api/v1/users/me/orders",
        headers=auth,
        params={"view": "pending_payment", "limit": 1},
    )
    assert first_page.status_code == 200, first_page.text
    first_body = first_page.json()
    assert len(first_body["data"]["items"]) == 1
    assert first_body["meta"]["pagination"]["has_next"] is True
    assert first_body["data"]["items"][0]["matched_views"] == [
        "all",
        "pending_payment",
    ]
    assert [action["code"] for action in first_body["data"]["items"][0]["available_actions"]] == [
        "pay",
        "cancel_order",
    ]
    dashboard = await client.get("/api/v1/users/me/dashboard", headers=auth)
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["data"]["order_counts"]["pending_payment"] >= 2
    assert "orders" not in dashboard.json()["data"]["unavailable_sections"]
    second_page = await client.get(
        "/api/v1/users/me/orders",
        headers=auth,
        params={
            "view": "pending_payment",
            "limit": 1,
            "cursor": first_body["meta"]["pagination"]["next_cursor"],
        },
    )
    assert second_page.status_code == 200, second_page.text
    assert second_page.json()["meta"]["pagination"]["has_previous"] is True

    search = await client.get(
        "/api/v1/users/me/orders",
        headers=auth,
        params={"q": "第二结算商品"},
    )
    assert search.status_code == 200, search.text
    assert len(search.json()["data"]["items"]) == 1
    selected_order_id = search.json()["data"]["items"][0]["order_id"]
    detail = await client.get(f"/api/v1/orders/{selected_order_id}", headers=auth)
    assert detail.status_code == 200, detail.text
    detail_data = detail.json()["data"]
    assert detail_data["address"]["recipient_name"] == "测试用户"
    assert detail_data["address"]["phone_masked"] == "*** **** 5678"
    assert detail_data["address"]["address"] == "测试路 1 号"
    assert len(detail_data["events"]) == 4
    assert detail.headers["etag"] == '"v0"'

    events = await client.get(f"/api/v1/orders/{selected_order_id}/events", headers=auth)
    assert events.status_code == 200
    assert {item["state_dimension"] for item in events.json()["data"]["items"]} == {
        "order",
        "payment",
        "fulfillment",
        "after_sale",
    }
    trade_view = await client.get(
        f"/api/v1/trade-orders/{order_data['trade_order_id']}", headers=auth
    )
    assert trade_view.status_code == 200, trade_view.text
    assert trade_view.json()["data"]["order_count"] == 2
    assert len(trade_view.json()["data"]["orders"]) == 2
    missing = await client.get("/api/v1/orders/ord_01DOESNOTEXIST", headers=auth)
    assert missing.status_code == 404
    assert missing.json()["code"] == "RESOURCE_NOT_FOUND"

    cancelled = await client.post(
        f"/api/v1/orders/{selected_order_id}/cancellations",
        headers={
            **auth,
            "If-Match": detail.headers["etag"],
            "Idempotency-Key": f"order-cancel-{suffix}",
        },
        json={"reason_code": "no_longer_needed", "description": "测试取消"},
    )
    assert cancelled.status_code == 200, cancelled.text
    cancelled_data = cancelled.json()["data"]
    assert cancelled_data["order"]["order_status"] == "cancelled"
    assert cancelled_data["events"][0]["event_code"] == "order.user_cancelled"
    assert [action["code"] for action in cancelled_data["order"]["available_actions"]] == [
        "delete_order",
        "repurchase",
    ]
    cancellation_replay = await client.post(
        f"/api/v1/orders/{selected_order_id}/cancellations",
        headers={
            **auth,
            "If-Match": detail.headers["etag"],
            "Idempotency-Key": f"order-cancel-{suffix}",
        },
        json={"reason_code": "no_longer_needed", "description": "测试取消"},
    )
    assert cancellation_replay.status_code == 200
    assert cancellation_replay.json()["data"] == cancelled_data
    async for session in mysql_session():
        cancelled_orders = list(
            (
                await session.scalars(
                    select(Order).where(Order.order_no.in_(order_data["order_ids"]))
                )
            ).all()
        )
        assert len(cancelled_orders) == 2
        assert all(item.order_status == "cancelled" for item in cancelled_orders)
        released = list(
            (
                await session.scalars(
                    select(InventoryReservation).where(
                        InventoryReservation.order_id.in_([item.id for item in cancelled_orders])
                    )
                )
            ).all()
        )
        assert all(item.reservation_status == "released" for item in released)

    hidden = await client.delete(
        f"/api/v1/users/me/orders/{selected_order_id}",
        headers={**auth, "If-Match": cancelled.headers["etag"]},
    )
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["data"]["restore_url"].endswith(
        f"/users/me/orders/{selected_order_id}/restorations"
    )
    assert (
        await client.get(f"/api/v1/orders/{selected_order_id}", headers=auth)
    ).status_code == 404
    restored = await client.post(
        f"/api/v1/users/me/orders/{selected_order_id}/restorations",
        headers={
            **auth,
            "If-Match": hidden.headers["etag"],
            "Idempotency-Key": f"order-restore-{suffix}",
        },
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["order_id"] == selected_order_id

    current_cart = await client.get("/api/v1/users/me/cart", headers=auth)
    repurchased = await client.post(
        f"/api/v1/orders/{selected_order_id}/repurchases",
        headers={
            **auth,
            "If-Match": current_cart.headers["etag"],
            "Idempotency-Key": f"order-repurchase-{suffix}",
        },
    )
    assert repurchased.status_code == 200, repurchased.text
    assert len(repurchased.json()["data"]["added_items"]) == 1
    assert repurchased.json()["data"]["unavailable_items"] == []

    receipt_checkout = await client.post(
        "/api/v1/checkout-sessions",
        headers={**auth, "Idempotency-Key": f"receipt-checkout-{suffix}"},
        json={"source": {"source_type": "buy_now", "sku_id": sku_no, "quantity": 2}},
    )
    assert receipt_checkout.status_code == 201, receipt_checkout.text
    receipt_checkout_data = receipt_checkout.json()["data"]
    receipt_created = await client.post(
        "/api/v1/orders",
        headers={**auth, "Idempotency-Key": f"receipt-order-{suffix}"},
        json={
            "checkout_id": receipt_checkout_data["checkout_id"],
            "checkout_version": receipt_checkout_data["version"],
        },
    )
    assert receipt_created.status_code == 201, receipt_created.text
    receipt_order_id = receipt_created.json()["data"]["order_ids"][0]
    receipt_trade_id = receipt_created.json()["data"]["trade_order_id"]
    payment_payload = {
        "trade_order_id": receipt_trade_id,
        "provider": "fake",
        "payment_method": "fake_balance",
        "return_url_key": "payment_result",
    }
    payment_created = await client.post(
        "/api/v1/payments",
        headers={**auth, "Idempotency-Key": f"payment-create-{suffix}"},
        json=payment_payload,
    )
    assert payment_created.status_code == 201, payment_created.text
    payment_data = payment_created.json()["data"]
    assert payment_data["payment_status"] == "pending"
    assert payment_data["display_status"] == "confirming"
    receipt_trade = await client.get(f"/api/v1/trade-orders/{receipt_trade_id}", headers=auth)
    assert receipt_trade.status_code == 200
    assert (
        payment_data["requested_amount"]
        == receipt_trade.json()["data"]["amounts"]["payable_amount"]
    )
    assert [event["to_status"] for event in payment_data["events"]] == [
        "created",
        "pending",
    ]
    payment_replay = await client.post(
        "/api/v1/payments",
        headers={**auth, "Idempotency-Key": f"payment-create-{suffix}"},
        json=payment_payload,
    )
    assert payment_replay.status_code == 201
    assert payment_replay.json()["data"]["payment_id"] == payment_data["payment_id"]
    active_conflict = await client.post(
        "/api/v1/payments",
        headers={**auth, "Idempotency-Key": f"payment-create-new-{suffix}"},
        json=payment_payload,
    )
    assert active_conflict.status_code == 409
    assert active_conflict.json()["code"] == "PAYMENT_ATTEMPT_IN_PROGRESS"
    assert active_conflict.headers["location"].endswith(payment_data["payment_id"])
    payment_get = await client.get(f"/api/v1/payments/{payment_data['payment_id']}", headers=auth)
    assert payment_get.status_code == 200
    assert payment_get.json()["data"]["action"] is None
    payment_list = await client.get(
        f"/api/v1/trade-orders/{receipt_trade_id}/payments", headers=auth
    )
    assert payment_list.status_code == 200
    assert [item["payment_id"] for item in payment_list.json()["data"]["items"]] == [
        payment_data["payment_id"]
    ]

    def webhook_request(
        payment: dict[str, object],
        event_id: str,
        *,
        amount_delta: int = 0,
        callback_status: str = "succeeded",
    ) -> tuple[bytes, dict[str, str]]:
        amount = payment["requested_amount"]
        assert isinstance(amount, dict)
        body = json.dumps(
            {
                "provider_event_id": event_id,
                "payment_id": payment["payment_id"],
                "provider_trade_no": f"fake_{payment['payment_id']}",
                "status": callback_status,
                "amount_minor_units": str(int(str(amount["minor_units"])) + amount_delta),
                "currency": amount["currency"],
                "occurred_at": datetime.now(UTC).isoformat(),
                "failure_code": "FAKE_DECLINED" if callback_status == "failed" else None,
            },
            separators=(",", ":"),
        ).encode()
        timestamp = str(int(datetime.now(UTC).timestamp()))
        signature = security.keyed_hash(
            "fake-payment-webhook", timestamp.encode() + b"." + body
        ).hex()
        return body, {
            "Content-Type": "application/json",
            "X-Payment-Timestamp": timestamp,
            "X-Payment-Signature": signature,
        }

    invalid_body, valid_headers = webhook_request(payment_data, f"fake_invalid_{suffix}")
    invalid_signature = await client.post(
        "/api/v1/webhooks/payments/fake",
        headers={**valid_headers, "X-Payment-Signature": "0" * 64},
        content=invalid_body,
    )
    assert invalid_signature.status_code == 401
    mismatch_body, mismatch_headers = webhook_request(
        payment_data, f"fake_mismatch_{suffix}", amount_delta=1
    )
    mismatch_callback = await client.post(
        "/api/v1/webhooks/payments/fake",
        headers=mismatch_headers,
        content=mismatch_body,
    )
    assert mismatch_callback.status_code == 200
    assert (
        await client.get(f"/api/v1/payments/{payment_data['payment_id']}", headers=auth)
    ).json()["data"]["payment_status"] == "pending"
    success_body, success_headers = webhook_request(payment_data, f"fake_success_{suffix}")
    succeeded_callback = await client.post(
        "/api/v1/webhooks/payments/fake",
        headers=success_headers,
        content=success_body,
    )
    assert succeeded_callback.status_code == 200, succeeded_callback.text
    assert succeeded_callback.json()["data"]["duplicate"] is False
    duplicate_callback = await client.post(
        "/api/v1/webhooks/payments/fake",
        headers=success_headers,
        content=success_body,
    )
    assert duplicate_callback.status_code == 200
    assert duplicate_callback.json()["data"]["duplicate"] is True
    settled_payment = await client.get(
        f"/api/v1/payments/{payment_data['payment_id']}", headers=auth
    )
    assert settled_payment.status_code == 200
    assert settled_payment.json()["data"]["payment_status"] == "succeeded"
    assert settled_payment.json()["data"]["paid_amount"] == payment_data["requested_amount"]
    async for session in mysql_session():
        receipt_order = await session.scalar(
            select(Order).where(Order.order_no == receipt_order_id)
        )
        assert receipt_order is not None
        assert receipt_order.order_status == "pending_shipment"
        assert receipt_order.payment_status == "paid"
        reservation = await session.scalar(
            select(InventoryReservation).where(InventoryReservation.order_id == receipt_order.id)
        )
        assert reservation is not None and reservation.reservation_status == "confirmed"
        callbacks = list(
            (
                await session.scalars(
                    select(PaymentCallback).where(
                        PaymentCallback.provider == "fake",
                        PaymentCallback.payload_hash.in_(
                            [
                                hashlib.sha256(mismatch_body).digest(),
                                hashlib.sha256(success_body).digest(),
                            ]
                        ),
                    )
                )
            ).all()
        )
        assert {item.process_status for item in callbacks} == {"rejected", "processed"}
        receipt_item = await session.scalar(
            select(OrderItem).where(OrderItem.order_id == receipt_order.id)
        )
        assert receipt_item is not None
        receipt_order_version = receipt_order.version
        receipt_item_no = receipt_item.order_item_no
        receipt_item_quantity = receipt_item.quantity
        receipt_store_id = receipt_order.store_id
        review_source = FileObject(
            file_no=new_prefixed_ulid("file_"),
            bucket="review-assets-private",
            object_key=f"tests/reviews/{suffix}/source.webp",
            purpose="review_image",
            owner_type="user",
            owner_no=user_no,
            variant="original",
            declared_mime_type="image/webp",
            detected_mime_type="image/webp",
            size_bytes=1024,
            sha256=hashlib.sha256(f"review-source-{suffix}".encode()).digest(),
            width=960,
            height=960,
            visibility="private",
            sensitivity_level="L1",
            scan_status="safe",
            file_status="active",
            reference_count=0,
            activated_at=utc_now(),
        )
        session.add(review_source)
        await session.flush()
        review_file = FileObject(
            file_no=new_prefixed_ulid("file_"),
            bucket="public-assets",
            object_key=f"tests/reviews/{suffix}/w960.webp",
            purpose="review_image",
            owner_type="user",
            owner_no=user_no,
            parent_file_id=review_source.id,
            variant="w960",
            processor_version="image-v1",
            declared_mime_type="image/webp",
            detected_mime_type="image/webp",
            size_bytes=768,
            sha256=hashlib.sha256(f"review-w960-{suffix}".encode()).digest(),
            width=960,
            height=960,
            visibility="private",
            sensitivity_level="L1",
            scan_status="safe",
            file_status="active",
            reference_count=0,
            activated_at=utc_now(),
        )
        session.add(review_file)
        await session.commit()
        review_file_no = review_file.file_no
    first_tracking_no = f"SF{suffix.upper()}1111"
    tracking_no = f"SF{suffix.upper()}1234"
    async for session in mysql_session():
        admin_user = await session.scalar(select(User).where(User.user_no == user_no))
        admin_session = await session.scalar(
            select(AuthSession).where(AuthSession.session_no == session_no)
        )
        assert admin_user is not None and admin_session is not None
        claims = TokenClaims(
            subject=admin_user.user_no,
            session_id=admin_session.session_no,
            audience="admin",
            permission_version=admin_user.permission_version,
            expires_at=admin_session.expires_at,
        )
        permission = Permission(
            permission_code="shipments:create",
            resource="shipments",
            action="create",
            risk_level="high",
            allowed_scope_types=["store"],
            delegation_policy="role_policy",
            requires_mfa=True,
            requires_recent_auth=True,
            approval_policy="none",
            owner="logistics",
            description="shipment create",
            permission_status="active",
        )
        access = AdminAccess(
            context=AuthContext(
                user=admin_user,
                session=admin_session,
                claims=claims,
            ),
            permission=permission,
            scopes=(("store", receipt_store_id),),
        )
        logistics = LogisticsService(
            session,
            security,
            get_settings().security_hmac_secret.get_secret_value(),
        )
        shipment_request = AdminShipmentCreateRequest(
            carrier_code="fake_express",
            carrier_name="测试快递",
            tracking_no=first_tracking_no,
            items=[
                AdminShipmentCreateItem(
                    order_item_id=receipt_item_no,
                    quantity=1,
                )
            ],
        )
        shipment_result = await logistics.create_shipment(
            access,
            receipt_order_id,
            shipment_request,
            receipt_order_version,
            f"shipment-create-{suffix}",
        )
        assert "tracking_no" not in shipment_result.model_dump()
        replayed_shipment = await logistics.create_shipment(
            access,
            receipt_order_id,
            shipment_request,
            receipt_order_version,
            f"shipment-create-{suffix}",
        )
        assert replayed_shipment.shipment_id == shipment_result.shipment_id
        partial_order = await session.scalar(
            select(Order).where(Order.order_no == receipt_order_id)
        )
        assert partial_order is not None
        assert partial_order.order_status == "pending_shipment"
        assert partial_order.fulfillment_status == "partial"
        second_request = AdminShipmentCreateRequest(
            carrier_code="fake_express",
            carrier_name="测试快递",
            tracking_no=tracking_no,
            items=[
                AdminShipmentCreateItem(
                    order_item_id=receipt_item_no,
                    quantity=receipt_item_quantity - 1,
                )
            ],
        )
        second_result = await logistics.create_shipment(
            access,
            receipt_order_id,
            second_request,
            partial_order.version,
            f"shipment-create-second-{suffix}",
        )
        shipment_no = second_result.shipment_id
        shipment_masked = second_result.tracking_no_masked
        shipped_order = await session.scalar(
            select(Order).where(Order.order_no == receipt_order_id)
        )
        assert shipped_order is not None
        assert shipped_order.order_status == "shipped"
        assert shipped_order.fulfillment_status == "shipped"
        correction_permission = Permission(
            permission_code="shipments:correct",
            resource="shipments",
            action="correct",
            risk_level="high",
            allowed_scope_types=["store"],
            delegation_policy="role_policy",
            requires_mfa=True,
            requires_recent_auth=True,
            approval_policy="none",
            owner="logistics",
            description="shipment correct",
            permission_status="active",
        )
        correction_access = AdminAccess(
            context=access.context,
            permission=correction_permission,
            scopes=access.scopes,
        )
        corrected = await logistics.correct_tracking(
            correction_access,
            shipment_result.shipment_id,
            AdminTrackingCorrectionRequest(
                tracking_no=f"SF{suffix.upper()}2222",
                reason_code="ENTRY_ERROR",
                reason="录入运单号时发生错误",
            ),
            shipment_result.version,
            f"shipment-correct-{suffix}",
        )
        assert corrected.version == 1
        assert "tracking_no" not in corrected.model_dump()
        void_permission = Permission(
            permission_code="shipments:void",
            resource="shipments",
            action="void",
            risk_level="high",
            allowed_scope_types=["store"],
            delegation_policy="role_policy",
            requires_mfa=True,
            requires_recent_auth=True,
            approval_policy="none",
            owner="logistics",
            description="shipment void",
            permission_status="active",
        )
        void_access = AdminAccess(
            context=access.context,
            permission=void_permission,
            scopes=access.scopes,
        )
        voided = await logistics.void_shipment(
            void_access,
            shipment_result.shipment_id,
            AdminShipmentVoidRequest(
                reason_code="PACKAGE_ERROR",
                reason="包裹分配错误，撤销后重新发货",
            ),
            corrected.version,
            f"shipment-void-{suffix}",
        )
        assert voided.shipment_status == "voided"
        reopened_order = await session.scalar(
            select(Order).where(Order.order_no == receipt_order_id)
        )
        assert reopened_order is not None
        assert reopened_order.order_status == "pending_shipment"
        assert reopened_order.fulfillment_status == "partial"
        replacement = await logistics.create_shipment(
            access,
            receipt_order_id,
            AdminShipmentCreateRequest(
                carrier_code="fake_express",
                carrier_name="测试快递",
                tracking_no=f"SF{suffix.upper()}9999",
                items=[
                    AdminShipmentCreateItem(
                        order_item_id=receipt_item_no,
                        quantity=1,
                    )
                ],
            ),
            reopened_order.version,
            f"shipment-replacement-{suffix}",
        )
        assert replacement.shipment_status == "created"
        restored_order = await session.scalar(
            select(Order).where(Order.order_no == receipt_order_id)
        )
        assert restored_order is not None
        assert restored_order.order_status == "shipped"
        assert restored_order.fulfillment_status == "shipped"
        wrong_scope_access = AdminAccess(
            context=access.context,
            permission=permission,
            scopes=(("store", receipt_store_id + 99_999),),
        )
        with pytest.raises(ApplicationError) as scope_error:
            await logistics.admin_detail(wrong_scope_access, shipment_no)
        assert scope_error.value.status == 404

    def logistics_webhook_request(
        event_id: str,
        *,
        event_status: str,
        description: str,
        occurred_at: datetime,
        include_estimate: bool = False,
    ) -> tuple[bytes, dict[str, str]]:
        payload = {
            "provider_event_id": event_id,
            "shipment_id": shipment_no,
            "carrier_code": "fake_express",
            "tracking_no": tracking_no,
            "status": event_status,
            "provider_status": event_status.upper(),
            "description": description,
            "location_text": "广州市",
            "occurred_at": occurred_at.isoformat(),
        }
        if include_estimate:
            payload["estimated_delivery_min_at"] = (
                datetime.now(UTC) + timedelta(days=1)
            ).isoformat()
            payload["estimated_delivery_max_at"] = (
                datetime.now(UTC) + timedelta(days=2)
            ).isoformat()
        body = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = str(int(datetime.now(UTC).timestamp()))
        signature = security.keyed_hash(
            "fake-logistics-webhook", timestamp.encode() + b"." + body
        ).hex()
        return body, {
            "Content-Type": "application/json",
            "X-Logistics-Timestamp": timestamp,
            "X-Logistics-Signature": signature,
        }

    in_transit_body, in_transit_headers = logistics_webhook_request(
        f"track-{suffix}",
        event_status="in_transit",
        description="包裹正在运输途中",
        occurred_at=datetime.now(UTC) - timedelta(minutes=10),
        include_estimate=True,
    )
    invalid_logistics_webhook = await client.post(
        "/api/v1/webhooks/logistics/fake_express",
        headers={**in_transit_headers, "X-Logistics-Signature": "invalid"},
        content=in_transit_body,
    )
    assert invalid_logistics_webhook.status_code == 401
    in_transit_webhook = await client.post(
        "/api/v1/webhooks/logistics/fake_express",
        headers=in_transit_headers,
        content=in_transit_body,
    )
    assert in_transit_webhook.status_code == 200, in_transit_webhook.text
    assert in_transit_webhook.json()["data"]["duplicate"] is False
    duplicate_logistics_webhook = await client.post(
        "/api/v1/webhooks/logistics/fake_express",
        headers=in_transit_headers,
        content=in_transit_body,
    )
    assert duplicate_logistics_webhook.status_code == 200
    assert duplicate_logistics_webhook.json()["data"]["duplicate"] is True
    older_body, older_headers = logistics_webhook_request(
        f"track-older-{suffix}",
        event_status="picked_up",
        description="承运商补发较早的揽收轨迹",
        occurred_at=datetime.now(UTC) - timedelta(hours=1),
    )
    older_webhook = await client.post(
        "/api/v1/webhooks/logistics/fake_express",
        headers=older_headers,
        content=older_body,
    )
    assert older_webhook.status_code == 200, older_webhook.text
    conflict_body, conflict_headers = logistics_webhook_request(
        f"track-{suffix}",
        event_status="in_transit",
        description="同一事件编号但内容被篡改",
        occurred_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    conflict_webhook = await client.post(
        "/api/v1/webhooks/logistics/fake_express",
        headers=conflict_headers,
        content=conflict_body,
    )
    assert conflict_webhook.status_code == 409
    assert conflict_webhook.json()["code"] == "LOGISTICS_PROVIDER_EVENT_CONFLICT"
    async for session in mysql_session():
        shipment_other_user = await session.scalar(
            select(User).where(User.user_no == other_user_no)
        )
        assert shipment_other_user is not None
        logistics = LogisticsService(
            session,
            security,
            get_settings().security_hmac_secret.get_secret_value(),
        )
        with pytest.raises(ApplicationError) as ownership_error:
            await logistics.detail(shipment_other_user, shipment_no)
        assert ownership_error.value.status == 404
    shipment_list = await client.get(f"/api/v1/orders/{receipt_order_id}/shipments", headers=auth)
    assert shipment_list.status_code == 200, shipment_list.text
    assert len(shipment_list.json()["data"]["items"]) == 2
    summary = next(
        item for item in shipment_list.json()["data"]["items"] if item["shipment_id"] == shipment_no
    )
    assert summary["shipment_id"] == shipment_no
    assert summary["tracking_no_masked"] == shipment_masked
    assert "tracking_no" not in summary
    assert summary["delivery_estimate"]["status"] == "available"
    assert summary["delivery_estimate"]["source"] == "carrier"
    shipment_detail = await client.get(f"/api/v1/shipments/{shipment_no}", headers=auth)
    assert shipment_detail.status_code == 200, shipment_detail.text
    assert shipment_detail.json()["data"]["tracking_no"] == tracking_no
    assert shipment_detail.headers["etag"] == '"v2"'
    assert shipment_detail.json()["data"]["shipment_status"] == "in_transit"
    tracks = await client.get(f"/api/v1/shipments/{shipment_no}/tracks?limit=10", headers=auth)
    assert tracks.status_code == 200
    assert [item["track_status"] for item in tracks.json()["data"]["items"]] == [
        "picked_up",
        "in_transit",
    ]
    refresh_headers = {
        **auth,
        "Idempotency-Key": f"shipment-refresh-{suffix}",
    }
    refresh = await client.post(
        f"/api/v1/shipments/{shipment_no}/refreshes", headers=refresh_headers
    )
    assert refresh.status_code == 202, refresh.text
    refresh_replay = await client.post(
        f"/api/v1/shipments/{shipment_no}/refreshes", headers=refresh_headers
    )
    assert refresh_replay.status_code == 202
    refresh_limited = await client.post(
        f"/api/v1/shipments/{shipment_no}/refreshes",
        headers={**auth, "Idempotency-Key": f"shipment-refresh-new-{suffix}"},
    )
    assert refresh_limited.status_code == 429
    assert refresh_limited.json()["code"] == "SHIPMENT_REFRESH_RATE_LIMITED"
    async for session in mysql_session():
        logistics = LogisticsService(
            session,
            security,
            get_settings().security_hmac_secret.get_secret_value(),
        )
        assert await logistics.sync_shipment(shipment_no)
        shipment = await session.scalar(select(Shipment).where(Shipment.shipment_no == shipment_no))
        assert shipment is not None
        latest_poll = await session.scalar(
            select(LogisticsSyncLog)
            .where(
                LogisticsSyncLog.shipment_id == shipment.id,
                LogisticsSyncLog.sync_type == "poll",
            )
            .order_by(LogisticsSyncLog.id.desc())
            .limit(1)
        )
        assert latest_poll is not None
        assert latest_poll.sync_status == "no_change"
        assert latest_poll.track_count == 0
    receipt_detail = await client.get(f"/api/v1/orders/{receipt_order_id}", headers=auth)
    assert receipt_detail.status_code == 200
    confirmed = await client.post(
        f"/api/v1/orders/{receipt_order_id}/receipt-confirmations",
        headers={
            **auth,
            "If-Match": receipt_detail.headers["etag"],
            "Idempotency-Key": f"receipt-confirm-{suffix}",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["order"]["order_status"] == "completed"
    assert confirmed.json()["data"]["order"]["fulfillment_status"] == "received"
    dashboard = await client.get("/api/v1/users/me/dashboard", headers=auth)
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["data"]["order_counts"]["pending_review"] >= 1
    pending_reviews = await client.get(
        "/api/v1/users/me/reviews?view=pending&limit=20",
        headers=auth,
    )
    assert pending_reviews.status_code == 200, pending_reviews.text
    assert any(
        item["order_item_id"] == receipt_item_no
        and item["item_type"] == "pending"
        and item["review"] is None
        for item in pending_reviews.json()["data"]["items"]
    )
    review_eligibility = await client.get(
        f"/api/v1/review-eligibilities/{receipt_item_no}", headers=auth
    )
    assert review_eligibility.status_code == 200, review_eligibility.text
    assert review_eligibility.json()["data"]["eligible"] is True
    assert review_eligibility.json()["data"]["available_actions"] == ["create"]
    review_payload = {
        "order_item_id": receipt_item_no,
        "rating": 5,
        "content": "  包装很好\r\n商品符合预期  ",
        "is_anonymous": False,
        "image_file_ids": [review_file_no],
    }
    review_headers = {**auth, "Idempotency-Key": f"review-create-{suffix}"}
    created_review = await client.post(
        "/api/v1/reviews",
        headers=review_headers,
        json=review_payload,
    )
    assert created_review.status_code == 201, created_review.text
    assert created_review.json()["data"]["content"] == "包装很好\n商品符合预期"
    assert created_review.json()["data"]["review_status"] == "pending"
    assert created_review.json()["data"]["moderation_status"] == "pending"
    assert created_review.json()["data"]["images"][0]["file_id"] == review_file_no
    assert created_review.headers["etag"] == '"v0"'
    replayed_review = await client.post(
        "/api/v1/reviews",
        headers=review_headers,
        json=review_payload,
    )
    assert replayed_review.status_code == 201
    assert replayed_review.json()["data"] == created_review.json()["data"]
    duplicate_review = await client.post(
        "/api/v1/reviews",
        headers={**auth, "Idempotency-Key": f"review-create-duplicate-{suffix}"},
        json=review_payload,
    )
    assert duplicate_review.status_code == 409
    assert duplicate_review.json()["code"] == "REVIEW_ALREADY_EXISTS"
    existing_eligibility = await client.get(
        f"/api/v1/review-eligibilities/{receipt_item_no}", headers=auth
    )
    assert existing_eligibility.status_code == 200
    assert existing_eligibility.json()["data"]["eligible"] is False
    assert (
        existing_eligibility.json()["data"]["existing_review_id"]
        == created_review.json()["data"]["review_id"]
    )
    assert existing_eligibility.json()["data"]["available_actions"] == ["view", "edit"]
    review_id = created_review.json()["data"]["review_id"]
    private_detail = await client.get(f"/api/v1/reviews/{review_id}", headers=auth)
    assert private_detail.status_code == 200, private_detail.text
    assert private_detail.headers["etag"] == '"v0"'
    hidden_from_public = await client.get(f"/api/v1/reviews/{review_id}")
    assert hidden_from_public.status_code == 404
    hidden_from_other_user = await client.get(f"/api/v1/reviews/{review_id}", headers=other_auth)
    assert hidden_from_other_user.status_code == 404
    cross_user_update = await client.patch(
        f"/api/v1/reviews/{review_id}",
        headers={**other_auth, "If-Match": created_review.headers["etag"]},
        json={
            "rating": 1,
            "content": "不应允许修改他人评价",
            "is_anonymous": False,
            "image_file_ids": [],
        },
    )
    assert cross_user_update.status_code == 404
    submitted_reviews = await client.get(
        f"/api/v1/users/me/reviews?view=published&order_id={receipt_order_id}",
        headers=auth,
    )
    assert submitted_reviews.status_code == 200, submitted_reviews.text
    assert submitted_reviews.json()["data"]["items"][0]["review"]["review_id"] == review_id
    missing_precondition = await client.patch(
        f"/api/v1/reviews/{review_id}",
        headers=auth,
        json={
            "rating": 4,
            "content": "修改后的评价",
            "is_anonymous": True,
            "image_file_ids": [review_file_no],
        },
    )
    assert missing_precondition.status_code == 428
    updated_review = await client.patch(
        f"/api/v1/reviews/{review_id}",
        headers={**auth, "If-Match": created_review.headers["etag"]},
        json={
            "rating": 4,
            "content": "  修改后的评价  ",
            "is_anonymous": True,
            "image_file_ids": [review_file_no],
        },
    )
    assert updated_review.status_code == 200, updated_review.text
    assert updated_review.headers["etag"] == '"v1"'
    assert updated_review.json()["data"]["content"] == "修改后的评价"
    assert updated_review.json()["data"]["is_anonymous"] is True
    stale_update = await client.patch(
        f"/api/v1/reviews/{review_id}",
        headers={**auth, "If-Match": created_review.headers["etag"]},
        json={
            "rating": 3,
            "content": "过期版本不应覆盖",
            "is_anonymous": False,
            "image_file_ids": [],
        },
    )
    assert stale_update.status_code == 409
    assert stale_update.json()["code"] == "VERSION_CONFLICT"
    async for session in mysql_session():
        reviewed_item = await session.scalar(
            select(OrderItem).where(OrderItem.order_item_no == receipt_item_no)
        )
        review = await session.scalar(
            select(Review).where(Review.review_no == created_review.json()["data"]["review_id"])
        )
        assert reviewed_item is not None and reviewed_item.review_status == "reviewed"
        assert review is not None and review.order_item_id == reviewed_item.id
        bound_image = await session.scalar(
            select(ReviewImage).where(ReviewImage.review_id == review.id)
        )
        bound_file = await session.scalar(
            select(FileObject).where(FileObject.file_no == review_file_no)
        )
        assert bound_file is not None and bound_file.reference_count == 1
        assert bound_image is not None and bound_image.object_key == bound_file.object_key
        revision = await session.scalar(
            select(ReviewRevisionRecord).where(ReviewRevisionRecord.review_id == review.id)
        )
        assert revision is not None
        assert revision.before_snapshot["rating"] == 5
        assert revision.after_snapshot["rating"] == 4
        review.review_status = "published"
        review.moderation_status = "passed"
        review.published_at = utc_now()
        review.version += 1
        bound_file.visibility = "public_derivative"
        append_source = FileObject(
            file_no=new_prefixed_ulid("file_"),
            bucket="review-assets-private",
            object_key=f"tests/reviews/{suffix}/append-source.webp",
            purpose="review_image",
            owner_type="user",
            owner_no=user_no,
            variant="original",
            declared_mime_type="image/webp",
            detected_mime_type="image/webp",
            size_bytes=1024,
            sha256=hashlib.sha256(f"append-source-{suffix}".encode()).digest(),
            width=800,
            height=800,
            visibility="private",
            sensitivity_level="L1",
            scan_status="safe",
            file_status="active",
            reference_count=0,
            activated_at=utc_now(),
        )
        session.add(append_source)
        await session.flush()
        append_file = FileObject(
            file_no=new_prefixed_ulid("file_"),
            bucket="public-assets",
            object_key=f"tests/reviews/{suffix}/append-w960.webp",
            purpose="review_image",
            owner_type="user",
            owner_no=user_no,
            parent_file_id=append_source.id,
            variant="w960",
            processor_version="image-v1",
            declared_mime_type="image/webp",
            detected_mime_type="image/webp",
            size_bytes=700,
            sha256=hashlib.sha256(f"append-w960-{suffix}".encode()).digest(),
            width=800,
            height=800,
            visibility="private",
            sensitivity_level="L1",
            scan_status="safe",
            file_status="active",
            reference_count=0,
            activated_at=utc_now(),
        )
        session.add(append_file)
        await session.commit()
        append_file_no = append_file.file_no

    public_detail = await client.get(f"/api/v1/reviews/{review_id}")
    assert public_detail.status_code == 200, public_detail.text
    assert public_detail.json()["data"]["user_display_name"] == "匿名用户"
    assert "order_id" not in public_detail.json()["data"]
    append_payload = {
        "content": "  使用一段时间后的追评  ",
        "image_file_ids": [append_file_no],
    }
    append_headers = {**auth, "Idempotency-Key": f"review-append-{suffix}"}
    appended = await client.post(
        f"/api/v1/reviews/{review_id}/append-records",
        headers=append_headers,
        json=append_payload,
    )
    assert appended.status_code == 201, appended.text
    assert appended.json()["data"]["append"]["content"] == "使用一段时间后的追评"
    assert appended.json()["data"]["append"]["images"][0]["file_id"] == append_file_no
    assert "append" not in appended.json()["data"]["available_actions"]
    append_replay = await client.post(
        f"/api/v1/reviews/{review_id}/append-records",
        headers=append_headers,
        json=append_payload,
    )
    assert append_replay.status_code == 201
    assert append_replay.json()["data"] == appended.json()["data"]
    duplicate_append = await client.post(
        f"/api/v1/reviews/{review_id}/append-records",
        headers={**auth, "Idempotency-Key": f"review-append-duplicate-{suffix}"},
        json=append_payload,
    )
    assert duplicate_append.status_code == 409
    assert duplicate_append.json()["code"] == "REVIEW_APPEND_ALREADY_EXISTS"
    async for session in mysql_session():
        review = await session.scalar(select(Review).where(Review.review_no == review_id))
        assert review is not None
        append_record = await session.scalar(
            select(ReviewAppendRecord).where(ReviewAppendRecord.review_id == review.id)
        )
        assert append_record is not None and append_record.append_status == "pending"
        append_image = await session.scalar(
            select(ReviewAppendImage).where(ReviewAppendImage.append_record_id == append_record.id)
        )
        append_bound_file = await session.scalar(
            select(FileObject).where(FileObject.file_no == append_file_no)
        )
        assert append_image is not None
        assert append_bound_file is not None and append_bound_file.reference_count == 1

    async for session in mysql_session():
        review = await session.scalar(select(Review).where(Review.review_no == review_id))
        assert review is not None
        review_service = ReviewService(session, get_settings())
        reply_access = AdminAccess(
            context=access.context,
            permission=Permission(
                permission_code="reviews:reply",
                resource="reviews",
                action="reply",
                risk_level="medium",
                allowed_scope_types=["store"],
                delegation_policy="role_policy",
                requires_mfa=False,
                requires_recent_auth=False,
                approval_policy="none",
                owner="reviews",
                description="review reply",
                permission_status="active",
            ),
            scopes=(("store", receipt_store_id),),
        )
        replied_review = await review_service.admin_reply(
            reply_access,
            review_id,
            AdminReviewReplyRequest(content="感谢您的真实评价。"),
            review.version,
            f"review-reply-{suffix}",
        )
        assert replied_review.merchant_reply is not None
        moderation_access = AdminAccess(
            context=access.context,
            permission=Permission(
                permission_code="reviews:moderate",
                resource="reviews",
                action="moderate",
                risk_level="high",
                allowed_scope_types=["store"],
                delegation_policy="role_policy",
                requires_mfa=False,
                requires_recent_auth=True,
                approval_policy="none",
                owner="reviews",
                description="review moderation",
                permission_status="active",
            ),
            scopes=(("store", receipt_store_id),),
        )
        hidden_review = await review_service.admin_moderate(
            moderation_access,
            review_id,
            AdminReviewModerationRequest(
                action="hide",
                rule_code="CONTENT_POLICY",
                reason="测试屏蔽评价并保留治理历史",
            ),
            replied_review.version,
            f"review-hide-{suffix}",
        )
        assert hidden_review.review_status == "hidden"
    assert (await client.get(f"/api/v1/reviews/{review_id}")).status_code == 404
    async for session in mysql_session():
        review_service = ReviewService(session, get_settings())
        restored_review = await review_service.admin_moderate(
            moderation_access,
            review_id,
            AdminReviewModerationRequest(
                action="restore",
                rule_code="APPEAL_PASSED",
                reason="复核通过，恢复公开展示",
            ),
            hidden_review.version,
            f"review-restore-{suffix}",
        )
        assert restored_review.review_status == "published"
        assert [record.action for record in restored_review.governance_history] == [
            "hide",
            "restore",
        ]
        records = list(
            (
                await session.scalars(
                    select(ReviewGovernanceRecord).where(
                        ReviewGovernanceRecord.review_id
                        == select(Review.id).where(Review.review_no == review_id).scalar_subquery()
                    )
                )
            ).all()
        )
        assert len(records) == 2
    assert (await client.get(f"/api/v1/reviews/{review_id}")).status_code == 200

    duplicate_eligibility_item = await client.post(
        "/api/v1/refund-eligibility-checks",
        headers=auth,
        json={
            "order_id": receipt_order_id,
            "items": [
                {"order_item_id": receipt_item_no, "quantity": 1},
                {"order_item_id": receipt_item_no, "quantity": 1},
            ],
            "requested_type": "refund_only",
            "reason_code": "NO_LONGER_NEEDED",
        },
    )
    assert duplicate_eligibility_item.status_code == 422
    eligibility_payload = {
        "order_id": receipt_order_id,
        "items": [{"order_item_id": receipt_item_no, "quantity": 1}],
        "requested_type": "refund_only",
        "reason_code": "NO_LONGER_NEEDED",
    }
    first_eligibility = await client.post(
        "/api/v1/refund-eligibility-checks", headers=auth, json=eligibility_payload
    )
    assert first_eligibility.status_code == 200, first_eligibility.text
    first_eligibility_data = first_eligibility.json()["data"]
    assert first_eligibility_data["eligible"] is True
    first_refund_amount = first_eligibility_data["suggested_refund_amount"]
    first_refund = await client.post(
        "/api/v1/refund-applications",
        headers={**auth, "Idempotency-Key": f"refund-first-{suffix}"},
        json={
            "eligibility_token": first_eligibility_data["eligibility_token"],
            "items": eligibility_payload["items"],
            "refund_type": "refund_only",
            "reason_code": "NO_LONGER_NEEDED",
            "reason_detail": "测试部分数量售后占用",
            "requested_amount": first_refund_amount,
            "policy_accepted": True,
        },
    )
    assert first_refund.status_code == 201, first_refund.text
    first_refund_id = first_refund.json()["data"]["refund_id"]
    assert (
        await client.get(f"/api/v1/refund-applications/{first_refund_id}", headers=other_auth)
    ).status_code == 404
    remaining_eligibility = await client.post(
        "/api/v1/refund-eligibility-checks", headers=auth, json=eligibility_payload
    )
    assert remaining_eligibility.status_code == 200, remaining_eligibility.text
    remaining_data = remaining_eligibility.json()["data"]
    assert remaining_data["eligible"] is True
    assert remaining_data["items"][0]["available_quantity"] == 1
    assert remaining_data["items"][0]["available_actions"] == [
        "view_active_after_sale",
        "apply_after_sale",
    ]
    async for session in mysql_session():
        receipt_item = await session.scalar(
            select(OrderItem).where(OrderItem.order_item_no == receipt_item_no)
        )
        assert receipt_item is not None
        assert (
            int(first_refund_amount["minor_units"])
            + int(remaining_data["suggested_refund_amount"]["minor_units"])
            == receipt_item.payable_amount
        )
    cancelled_refund = await client.post(
        f"/api/v1/refund-applications/{first_refund_id}/cancellations",
        headers={**auth, "Idempotency-Key": f"refund-cancel-{suffix}"},
        json={"reason": "改为整单申请"},
    )
    assert cancelled_refund.status_code == 200, cancelled_refund.text
    assert cancelled_refund.json()["data"]["refund_status"] == "cancelled"

    # A dedicated rejected refund isolates user cancellation from the later
    # administrator dual-control appeal scenario.
    async for session in mysql_session():
        cancelled_refund_row = await session.scalar(
            select(RefundApplication).where(RefundApplication.refund_no == first_refund_id)
        )
        assert cancelled_refund_row is not None
        cancellable_refund = RefundApplication(
            refund_no=new_prefixed_ulid("rfd_"),
            order_id=cancelled_refund_row.order_id,
            user_id=cancelled_refund_row.user_id,
            store_id=cancelled_refund_row.store_id,
            refund_type="refund_only",
            refund_status="rejected",
            reason_code="NO_LONGER_NEEDED",
            reason_detail="验证用户撤销申诉",
            requested_amount=cancelled_refund_row.requested_amount,
            approved_amount=0,
            currency=cancelled_refund_row.currency,
            policy_snapshot={"fixture": "appeal_cancel"},
            submitted_at=utc_now(),
            decided_at=utc_now(),
        )
        session.add(cancellable_refund)
        await session.flush()
        cancellable_appeal = RefundAppeal(
            appeal_no=new_prefixed_ulid("rap_"),
            refund_id=cancellable_refund.id,
            user_id=cancellable_refund.user_id,
            store_id=cancellable_refund.store_id,
            appeal_status="submitted",
            reason="验证用户撤销申诉",
        )
        session.add(cancellable_appeal)
        await session.flush()
        session.add(
            RefundAppealEvent(
                event_no=new_prefixed_ulid("rae_"),
                appeal_id=cancellable_appeal.id,
                event_type="appeal.created",
                from_status=None,
                to_status="submitted",
                actor_type="user",
                actor_id=cancellable_refund.user_id,
                reason_code="USER_APPEAL",
                remark="验证用户撤销申诉",
                appeal_version=cancellable_appeal.version,
            )
        )
        await session.commit()
        cancellable_appeal_id = cancellable_appeal.appeal_no
        cancellable_appeal_version = cancellable_appeal.version

    assert (
        await client.get(
            f"/api/v1/refund-appeals/{cancellable_appeal_id}/events", headers=other_auth
        )
    ).status_code == 404
    appeal_events = await client.get(
        f"/api/v1/refund-appeals/{cancellable_appeal_id}/events", headers=auth
    )
    assert appeal_events.status_code == 200, appeal_events.text
    assert [item["event_type"] for item in appeal_events.json()["data"]["items"]] == [
        "appeal.created"
    ]
    appeal_cancel_key = f"appeal-cancel-{suffix}"
    cancelled_appeal = await client.post(
        f"/api/v1/refund-appeals/{cancellable_appeal_id}/cancellations",
        headers={
            **auth,
            "Idempotency-Key": appeal_cancel_key,
            "If-Match": f'"v{cancellable_appeal_version}"',
        },
    )
    assert cancelled_appeal.status_code == 200, cancelled_appeal.text
    assert cancelled_appeal.json()["data"]["appeal_status"] == "cancelled"
    replayed_appeal_cancel = await client.post(
        f"/api/v1/refund-appeals/{cancellable_appeal_id}/cancellations",
        headers={
            **auth,
            "Idempotency-Key": appeal_cancel_key,
            "If-Match": f'"v{cancellable_appeal_version}"',
        },
    )
    assert replayed_appeal_cancel.status_code == 200
    assert replayed_appeal_cancel.json()["data"] == cancelled_appeal.json()["data"]
    updated_appeal_events = await client.get(
        f"/api/v1/refund-appeals/{cancellable_appeal_id}/events", headers=auth
    )
    assert [
        item["event_type"] for item in updated_appeal_events.json()["data"]["items"]
    ] == ["appeal.created", "appeal.cancelled"]

    rejected_eligibility = await client.post(
        "/api/v1/refund-eligibility-checks", headers=auth, json=eligibility_payload
    )
    rejected_eligibility_data = rejected_eligibility.json()["data"]
    rejected_refund = await client.post(
        "/api/v1/refund-applications",
        headers={**auth, "Idempotency-Key": f"refund-rejected-{suffix}"},
        json={
            "eligibility_token": rejected_eligibility_data["eligibility_token"],
            "items": eligibility_payload["items"],
            "refund_type": "refund_only",
            "reason_code": "NO_LONGER_NEEDED",
            "reason_detail": "用于验证拒绝后重新申请和申诉",
            "requested_amount": rejected_eligibility_data["suggested_refund_amount"],
            "policy_accepted": True,
        },
    )
    assert rejected_refund.status_code == 201, rejected_refund.text
    rejected_refund_id = rejected_refund.json()["data"]["refund_id"]
    async for session in mysql_session():
        permission = Permission(
            permission_code="refunds:review",
            resource="refunds",
            action="review",
            risk_level="critical",
            allowed_scope_types=["store"],
            delegation_policy="role_policy",
            requires_mfa=True,
            requires_recent_auth=True,
            approval_policy="amount_based",
            owner="after_sale",
            description="refund review",
            permission_status="active",
        )
        refund_access = AdminAccess(
            context=access.context,
            permission=permission,
            scopes=(("store", receipt_store_id),),
        )
        after_sale = AfterSaleService(session, get_settings(), security)
        rejected_row = await session.scalar(
            select(RefundApplication).where(RefundApplication.refund_no == rejected_refund_id)
        )
        assert rejected_row is not None
        with pytest.raises(ApplicationError) as unclaimed_decision:
            await after_sale.decide(
                refund_access,
                rejected_refund_id,
                AdminRefundDecisionRequest(
                    decision="reject",
                    reason_code="POLICY_NOT_MET",
                    reason="测试未领取时禁止审核",
                ),
                rejected_row.version,
                f"refund-unclaimed-{suffix}",
            )
        assert unclaimed_decision.value.code == "REFUND_CLAIM_REQUIRED"
        await session.rollback()
        rejected_row = await session.scalar(
            select(RefundApplication).where(RefundApplication.refund_no == rejected_refund_id)
        )
        assert rejected_row is not None
        rejected_claim_key = f"refund-claim-rejected-{suffix}"
        rejected_claim_version = rejected_row.version
        claimed_refund = await after_sale.claim_refund(
            refund_access,
            rejected_refund_id,
            rejected_claim_version,
            rejected_claim_key,
        )
        replayed_claim = await after_sale.claim_refund(
            refund_access,
            rejected_refund_id,
            rejected_claim_version,
            rejected_claim_key,
        )
        assert replayed_claim.version == claimed_refund.version
        rejected_decision_key = f"refund-reject-{suffix}"
        rejected_result = await after_sale.decide(
            refund_access,
            rejected_refund_id,
            AdminRefundDecisionRequest(
                decision="reject",
                reason_code="POLICY_NOT_MET",
                reason="测试拒绝后释放订单项占用",
            ),
            claimed_refund.version,
            rejected_decision_key,
        )
        assert rejected_result.refund_status == "rejected"
        replayed_rejection = await after_sale.decide(
            refund_access,
            rejected_refund_id,
            AdminRefundDecisionRequest(
                decision="reject",
                reason_code="POLICY_NOT_MET",
                reason="测试拒绝后释放订单项占用",
            ),
            claimed_refund.version,
            rejected_decision_key,
        )
        assert replayed_rejection.version == rejected_result.version
    appeal = await client.post(
        f"/api/v1/refund-applications/{rejected_refund_id}/appeals",
        headers={**auth, "Idempotency-Key": f"refund-appeal-{suffix}"},
        json={"reason": "请求平台复核本次退款拒绝决定"},
    )
    assert appeal.status_code == 201, appeal.text
    assert appeal.json()["data"]["appeal_status"] == "submitted"
    appeal_id = appeal.json()["data"]["appeal_id"]

    full_eligibility_payload = {
        **eligibility_payload,
        "items": [{"order_item_id": receipt_item_no, "quantity": 2}],
    }
    full_eligibility = await client.post(
        "/api/v1/refund-eligibility-checks", headers=auth, json=full_eligibility_payload
    )
    full_eligibility_data = full_eligibility.json()["data"]
    assert full_eligibility_data["eligible"] is True
    successful_refund = await client.post(
        "/api/v1/refund-applications",
        headers={**auth, "Idempotency-Key": f"refund-success-{suffix}"},
        json={
            "eligibility_token": full_eligibility_data["eligibility_token"],
            "items": full_eligibility_payload["items"],
            "refund_type": "refund_only",
            "reason_code": "NO_LONGER_NEEDED",
            "reason_detail": "验证退款资金回调闭环",
            "requested_amount": full_eligibility_data["suggested_refund_amount"],
            "policy_accepted": True,
        },
    )
    assert successful_refund.status_code == 201, successful_refund.text
    successful_refund_id = successful_refund.json()["data"]["refund_id"]
    approval_settings = get_settings().model_copy(
        update={"refund_dual_approval_threshold_minor": 1}
    )
    admin_identities: list[tuple[str, str]] = []
    async for session in mysql_session():
        for label in ("refund_initiator", "refund_approver_a", "refund_approver_b"):
            username = f"{label}_{suffix}"
            provisioned = await provision_platform_super_admin(
                session,
                security,
                username=username,
                password=f"Admin-{label}-{suffix}-Correct-Horse!",
            )
            admin_user = await session.scalar(
                select(User).where(User.user_no == provisioned.user_no)
            )
            assert admin_user is not None
            admin_session = AuthSession(
                session_no=new_prefixed_ulid("ses_"),
                user_id=admin_user.id,
                refresh_token_hash=security.keyed_hash(
                    "refresh-token", f"approval-{label}-{suffix}"
                ),
                token_family_no=new_prefixed_ulid("tfa_"),
                device_no=new_prefixed_ulid("dev_"),
                device_name="Approval integration",
                client_type="web",
                audience="admin",
                csrf_token_hash=security.keyed_hash("csrf-token", f"approval-{label}-{suffix}"),
                authenticated_at=utc_now(),
                authentication_methods=["password", "totp"],
                assurance_level="aal2",
                issued_at=utc_now(),
                expires_at=utc_now() + timedelta(hours=1),
                last_seen_at=utc_now(),
            )
            session.add(admin_session)
            await session.commit()
            admin_identities.append((admin_user.user_no, admin_session.session_no))

        initiator_access = await _admin_access(
            session,
            *admin_identities[0],
            permission_code="refunds:review",
        )
        after_sale = AfterSaleService(session, approval_settings, security)
        successful_row = await session.scalar(
            select(RefundApplication).where(RefundApplication.refund_no == successful_refund_id)
        )
        assert successful_row is not None
        successful_claim_key = f"refund-claim-success-{suffix}"
        successful_claim_version = successful_row.version
        claimed_refund = await after_sale.claim_refund(
            initiator_access,
            successful_refund_id,
            successful_claim_version,
            successful_claim_key,
        )
        replayed_claim = await after_sale.claim_refund(
            initiator_access,
            successful_refund_id,
            successful_claim_version,
            successful_claim_key,
        )
        assert replayed_claim.version == claimed_refund.version
        approve_key = f"refund-approve-{suffix}"
        decision = AdminRefundDecisionRequest(
            decision="approve",
            reason_code="POLICY_PASSED",
            reason="符合首版原路退款规则",
            approved_amount=full_eligibility_data["suggested_refund_amount"],
        )
        approval_required = await after_sale.request_refund_decision(
            initiator_access,
            successful_refund_id,
            decision,
            claimed_refund.version,
            approve_key,
        )
        assert isinstance(approval_required, ApprovalRequiredView)
        replayed_approval = await after_sale.request_refund_decision(
            initiator_access,
            successful_refund_id,
            decision,
            claimed_refund.version,
            approve_key,
        )
        assert isinstance(replayed_approval, ApprovalRequiredView)
        assert replayed_approval.approval_request_id == approval_required.approval_request_id
        approval_no = approval_required.approval_request_id

    for index, identity in enumerate(admin_identities[1:], start=1):
        async for session in mysql_session():
            approver_access = await _admin_access(
                session,
                *identity,
                permission_code="admin_approvals:decide",
            )
            approval_row = await session.scalar(
                select(AdminApprovalRequest).where(
                    AdminApprovalRequest.approval_request_no == approval_no
                )
            )
            assert approval_row is not None
            approval_view = await RbacService(session, security).decide_approval(
                approver_access,
                approval_no,
                ApprovalDecisionRequest(
                    decision="approve",
                    reason_code="VERIFIED",
                    reason=f"第 {index} 位复核员确认参数和影响",
                ),
                approval_row.version,
                f"refund-approval-{index}-{suffix}",
            )
            assert approval_view.approved_count == index

    approval_succeeded = False
    async for session in mysql_session():
        approval_event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_no == approval_no)
        )
        assert approval_event is not None
        approval_event.available_at = utc_now() - timedelta(seconds=1)
        await session.commit()
    for _ in range(20):
        async for session in mysql_session():
            await AdminApprovalWorker(session, approval_settings, security).process_one()
            approval_row = await session.scalar(
                select(AdminApprovalRequest).where(
                    AdminApprovalRequest.approval_request_no == approval_no
                )
            )
            assert approval_row is not None
            if approval_row.request_status == "succeeded":
                approval_succeeded = True
                break
        if approval_succeeded:
            break
    assert approval_succeeded

    async for session in mysql_session():
        appeal_access = await _admin_access(
            session,
            *admin_identities[0],
            permission_code="refund_appeals:review",
        )
        appeal_row = await session.scalar(
            select(RefundAppeal).where(RefundAppeal.appeal_no == appeal_id)
        )
        assert appeal_row is not None
        appeal_service = AfterSaleService(session, approval_settings, security)
        claimed_appeal = await appeal_service.claim_appeal(
            appeal_access,
            appeal_id,
            appeal_row.version,
            f"appeal-claim-{suffix}",
        )
        appeal_approval = await appeal_service.request_appeal_decision(
            appeal_access,
            appeal_id,
            AdminRefundAppealDecisionRequest(
                decision="approve",
                reason="平台复核确认原退款拒绝结论应当撤销",
            ),
            claimed_appeal.version,
            f"appeal-decision-{suffix}",
        )
        assert isinstance(appeal_approval, ApprovalRequiredView)
        appeal_approval_no = appeal_approval.approval_request_id

    for index, identity in enumerate(admin_identities[1:], start=1):
        async for session in mysql_session():
            approver_access = await _admin_access(
                session,
                *identity,
                permission_code="admin_approvals:decide",
            )
            approval_row = await session.scalar(
                select(AdminApprovalRequest).where(
                    AdminApprovalRequest.approval_request_no == appeal_approval_no
                )
            )
            assert approval_row is not None
            await RbacService(session, security).decide_approval(
                approver_access,
                appeal_approval_no,
                ApprovalDecisionRequest(
                    decision="approve",
                    reason_code="VERIFIED",
                    reason=f"第 {index} 位复核员确认申诉结论",
                ),
                approval_row.version,
                f"appeal-approval-{index}-{suffix}",
            )

    appeal_approval_succeeded = False
    async for session in mysql_session():
        appeal_approval_event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_no == appeal_approval_no)
        )
        assert appeal_approval_event is not None
        appeal_approval_event.available_at = utc_now() - timedelta(seconds=1)
        await session.commit()
    for _ in range(20):
        async for session in mysql_session():
            await AdminApprovalWorker(session, approval_settings, security).process_one()
            appeal_approval_row = await session.scalar(
                select(AdminApprovalRequest).where(
                    AdminApprovalRequest.approval_request_no == appeal_approval_no
                )
            )
            assert appeal_approval_row is not None
            if appeal_approval_row.request_status == "succeeded":
                appeal_approval_succeeded = True
                break
        if appeal_approval_succeeded:
            break
    assert appeal_approval_succeeded
    async for session in mysql_session():
        decided_appeal = await session.scalar(
            select(RefundAppeal).where(RefundAppeal.appeal_no == appeal_id)
        )
        assert decided_appeal is not None and decided_appeal.appeal_status == "upheld"

    async for session in mysql_session():
        approved_refund = await session.scalar(
            select(RefundApplication).where(RefundApplication.refund_no == successful_refund_id)
        )
        assert approved_refund is not None and approved_refund.refund_status == "refunding"
        refund_payment = await session.scalar(
            select(RefundPaymentRecord).where(RefundPaymentRecord.refund_id == approved_refund.id)
        )
        assert refund_payment is not None
        refund_payment_no = refund_payment.refund_payment_no
    refund_webhook_payload = {
        "provider_event_id": f"refund-event-{suffix}",
        "refund_payment_no": refund_payment_no,
        "status": "succeeded",
        "amount_minor_units": full_eligibility_data["suggested_refund_amount"]["minor_units"],
        "currency": "CNY",
    }
    refund_webhook_body = json.dumps(refund_webhook_payload, separators=(",", ":")).encode()
    refund_webhook_timestamp = str(int(datetime.now(UTC).timestamp()))
    refund_webhook_signature = security.keyed_hash(
        "fake-refund-webhook",
        refund_webhook_timestamp.encode() + b"." + refund_webhook_body,
    ).hex()
    refund_webhook_headers = {
        "Content-Type": "application/json",
        "X-Refund-Timestamp": refund_webhook_timestamp,
        "X-Refund-Signature": refund_webhook_signature,
    }
    refund_webhook = await client.post(
        "/api/v1/webhooks/refunds/fake",
        headers=refund_webhook_headers,
        content=refund_webhook_body,
    )
    assert refund_webhook.status_code == 200, refund_webhook.text
    assert refund_webhook.json()["data"]["status"] == "succeeded"
    refund_webhook_replay = await client.post(
        "/api/v1/webhooks/refunds/fake",
        headers=refund_webhook_headers,
        content=refund_webhook_body,
    )
    assert refund_webhook_replay.status_code == 200
    assert refund_webhook_replay.json()["data"]["duplicate"] is True
    completed_refund = await client.get(
        f"/api/v1/refund-applications/{successful_refund_id}", headers=auth
    )
    assert completed_refund.status_code == 200
    assert completed_refund.json()["data"]["refund_status"] == "succeeded"

    timeout_checkout = await client.post(
        "/api/v1/checkout-sessions",
        headers={**auth, "Idempotency-Key": f"timeout-checkout-{suffix}"},
        json={"source": {"source_type": "buy_now", "sku_id": second_sku_no, "quantity": 1}},
    )
    assert timeout_checkout.status_code == 201, timeout_checkout.text
    timeout_checkout_data = timeout_checkout.json()["data"]
    timeout_created = await client.post(
        "/api/v1/orders",
        headers={**auth, "Idempotency-Key": f"timeout-order-{suffix}"},
        json={
            "checkout_id": timeout_checkout_data["checkout_id"],
            "checkout_version": timeout_checkout_data["version"],
        },
    )
    assert timeout_created.status_code == 201, timeout_created.text
    timeout_trade_id = timeout_created.json()["data"]["trade_order_id"]
    timeout_order_id = timeout_created.json()["data"]["order_ids"][0]
    timeout_payment_payload = {
        "trade_order_id": timeout_trade_id,
        "provider": "fake",
        "payment_method": "fake_balance",
        "return_url_key": "payment_result",
    }
    timeout_payment = await client.post(
        "/api/v1/payments",
        headers={**auth, "Idempotency-Key": f"timeout-payment-{suffix}"},
        json=timeout_payment_payload,
    )
    assert timeout_payment.status_code == 201
    timeout_payment_data = timeout_payment.json()["data"]
    processing_order = await client.get(f"/api/v1/orders/{timeout_order_id}", headers=auth)
    assert [action["code"] for action in processing_order.json()["data"]["available_actions"]] == []
    stale_close = await client.post(
        f"/api/v1/payments/{timeout_payment_data['payment_id']}/closures",
        headers={
            **auth,
            "If-Match": '"v0"',
            "Idempotency-Key": f"timeout-payment-close-stale-{suffix}",
        },
    )
    assert stale_close.status_code == 412
    closed_payment = await client.post(
        f"/api/v1/payments/{timeout_payment_data['payment_id']}/closures",
        headers={
            **auth,
            "If-Match": timeout_payment.headers["etag"],
            "Idempotency-Key": f"timeout-payment-close-{suffix}",
        },
    )
    assert closed_payment.status_code == 200, closed_payment.text
    assert closed_payment.json()["data"]["payment_status"] == "closed"
    payable_again = await client.get(f"/api/v1/orders/{timeout_order_id}", headers=auth)
    assert [action["code"] for action in payable_again.json()["data"]["available_actions"]] == [
        "pay",
        "cancel_order",
    ]
    close_replay = await client.post(
        f"/api/v1/payments/{timeout_payment_data['payment_id']}/closures",
        headers={
            **auth,
            "If-Match": timeout_payment.headers["etag"],
            "Idempotency-Key": f"timeout-payment-close-{suffix}",
        },
    )
    assert close_replay.status_code == 200
    assert close_replay.json()["data"] == closed_payment.json()["data"]
    retry_payment = await client.post(
        "/api/v1/payments",
        headers={**auth, "Idempotency-Key": f"timeout-payment-retry-{suffix}"},
        json=timeout_payment_payload,
    )
    assert retry_payment.status_code == 201
    retry_payment_data = retry_payment.json()["data"]
    failure_body, failure_headers = webhook_request(
        retry_payment_data,
        f"fake_failure_{suffix}",
        callback_status="failed",
    )
    failed_callback = await client.post(
        "/api/v1/webhooks/payments/fake",
        headers=failure_headers,
        content=failure_body,
    )
    assert failed_callback.status_code == 200
    failed_payment = await client.get(
        f"/api/v1/payments/{retry_payment_data['payment_id']}", headers=auth
    )
    assert failed_payment.json()["data"]["payment_status"] == "failed"
    reconcile_payment = await client.post(
        "/api/v1/payments",
        headers={**auth, "Idempotency-Key": f"timeout-payment-reconcile-{suffix}"},
        json=timeout_payment_payload,
    )
    assert reconcile_payment.status_code == 201
    reconcile_payment_id = reconcile_payment.json()["data"]["payment_id"]
    async for session in mysql_session():
        timeout_trade = await session.scalar(
            select(TradeOrder).where(TradeOrder.trade_no == timeout_trade_id)
        )
        assert timeout_trade is not None
        timeout_trade.expires_at = utc_now() - timedelta(seconds=1)
        timeout_orders = list(
            (
                await session.scalars(select(Order).where(Order.trade_order_id == timeout_trade.id))
            ).all()
        )
        for timeout_order in timeout_orders:
            timeout_order.expires_at = timeout_trade.expires_at
        expiring_payment = await session.scalar(
            select(Payment).where(Payment.payment_no == reconcile_payment_id)
        )
        assert expiring_payment is not None
        expiring_payment.expires_at = timeout_trade.expires_at
        await session.commit()
    async for session in mysql_session():
        reconciled = await PaymentService(session, security).reconcile_expired(limit=1000)
        assert reconciled >= 1
    reconciled_payment = await client.get(f"/api/v1/payments/{reconcile_payment_id}", headers=auth)
    assert reconciled_payment.json()["data"]["payment_status"] == "closed"
    async for session in mysql_session():
        processed = await OrderService(session, get_settings(), security).expire_due(limit=1000)
        assert processed >= 1
    async for session in mysql_session():
        timed_out_trade = await session.scalar(
            select(TradeOrder).where(TradeOrder.trade_no == timeout_trade_id)
        )
        assert timed_out_trade is not None and timed_out_trade.trade_status == "closed"
        timed_out_orders = list(
            (
                await session.scalars(
                    select(Order).where(Order.trade_order_id == timed_out_trade.id)
                )
            ).all()
        )
        assert all(item.order_status == "cancelled" for item in timed_out_orders)
        timeout_events = list(
            (
                await session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.aggregate_no == timeout_trade_id,
                        OutboxEvent.event_type == "trade_order.cancelled.v1",
                    )
                )
            ).all()
        )
        assert len(timeout_events) == 1
