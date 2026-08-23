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

from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.modules.cart.models import CartItem
from app.modules.catalog.models import Category, Product, ProductFulfillmentProfile, ProductSku
from app.modules.checkout.models import CheckoutSession
from app.modules.identity.models import AuthSession, User, UserAddress
from app.modules.inventory.models import Inventory, InventoryLog, InventoryReservation
from app.modules.orders.models import Order, OrderAddress, OrderItem, TradeOrder
from app.modules.orders.service import OrderService
from app.modules.payments.models import PaymentCallback
from app.modules.stores.models import ShippingTemplate, ShippingTemplateRule, Store
from app.modules.system.models import OutboxEvent

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
        user_no, session_no, sku_no, second_sku_no, address_no, other_address_no, store_no = (
            user.user_no,
            auth_session.session_no,
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
        json={"source": {"source_type": "buy_now", "sku_id": sku_no, "quantity": 1}},
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
        receipt_order.order_status = "shipped"
        receipt_order.fulfillment_status = "shipped"
        receipt_order.shipped_at = utc_now()
        receipt_order.version += 1
        await session.commit()
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
    processing_order = await client.get(
        f"/api/v1/orders/{timeout_order_id}", headers=auth
    )
    assert [
        action["code"]
        for action in processing_order.json()["data"]["available_actions"]
    ] == []
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
    assert [
        action["code"] for action in payable_again.json()["data"]["available_actions"]
    ] == ["pay", "cancel_order"]
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
        await session.commit()
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
