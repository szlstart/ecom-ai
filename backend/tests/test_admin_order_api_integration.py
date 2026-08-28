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
from app.modules.catalog.models import Category, Product, ProductSku
from app.modules.identity.models import User
from app.modules.inventory.models import Inventory, InventoryReservation
from app.modules.orders.models import Order, OrderItem, TradeOrder
from app.modules.rbac.models import AdminOperationLog
from app.modules.stores.models import Store
from app.modules.system.models import OutboxEvent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_admin_order_adjustment_and_cancellation_invariants(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    password = f"Admin-Order-{suffix}-Correct-Horse!"

    async for session in mysql_session():
        provisioning = await provision_platform_super_admin(
            session,
            security,
            username=f"order_admin_{suffix}",
            password=password,
        )
        admin = await session.scalar(select(User).where(User.user_no == provisioning.user_no))
        assert admin is not None
        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            category_name=f"订单分类 {suffix}",
            category_code=f"order-{suffix}",
            path="/order-test",
            level=1,
            sort_order=1,
            category_status="active",
        )
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=admin.id,
            store_name=f"订单店铺 {suffix}",
            store_name_normalized=f"order-store-{suffix}",
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
            product_name=f"订单商品 {suffix}",
            product_status="on_sale",
            min_price_amount=1000,
            max_price_amount=1000,
            currency="CNY",
            sales_count=0,
            review_count=0,
            rating_score=Decimal("0.00"),
            published_at=now,
        )
        session.add(product)
        await session.flush()
        sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=product.id,
            store_id=store.id,
            merchant_sku_code=f"order-{suffix}",
            sku_name="订单测试 SKU",
            spec_values=[{"name": "规格", "value": "标准"}],
            spec_signature=hashlib.sha256(f"order-spec-{suffix}".encode()).digest(),
            sale_price_amount=1000,
            market_price_amount=1000,
            currency="CNY",
            sku_status="active",
        )
        session.add(sku)
        await session.flush()
        trade = TradeOrder(
            trade_no=new_prefixed_ulid("trd_"),
            checkout_session_id=None,
            checkout_no_snapshot=new_prefixed_ulid("chk_"),
            checkout_snapshot_hash=hashlib.sha256(f"checkout-{suffix}".encode()).digest(),
            user_id=admin.id,
            order_source="buy_now",
            trade_status="pending_payment",
            goods_amount=2000,
            freight_amount=100,
            payable_amount=2100,
            adjustment_amount=0,
            paid_amount=0,
            refunded_amount=0,
            currency="CNY",
            order_count=1,
            expires_at=now + timedelta(hours=1),
        )
        session.add(trade)
        await session.flush()
        order = Order(
            order_no=new_prefixed_ulid("ord_"),
            trade_order_id=trade.id,
            user_id=admin.id,
            store_id=store.id,
            order_status="pending_payment",
            payment_status="unpaid",
            fulfillment_status="unfulfilled",
            after_sale_status="none",
            goods_amount=2000,
            freight_amount=100,
            payable_amount=2100,
            adjustment_amount=0,
            paid_amount=0,
            refunded_amount=0,
            currency="CNY",
            policy_snapshot={"schema_version": 1},
            expires_at=trade.expires_at,
        )
        session.add(order)
        await session.flush()
        order_item = OrderItem(
            order_item_no=new_prefixed_ulid("oit_"),
            order_id=order.id,
            product_id=product.id,
            sku_id=sku.id,
            product_no=product.product_no,
            sku_no=sku.sku_no,
            product_name=product.product_name,
            sku_name=sku.sku_name,
            spec_snapshot=sku.spec_values,
            quantity=2,
            unit_price_amount=1000,
            market_price_amount=1000,
            gross_amount=2000,
            payable_amount=2000,
            adjustment_amount=0,
            refunded_quantity=0,
            refunded_amount=0,
            currency="CNY",
            # A list projection must not attempt to resolve a review route unless
            # the policy actually returns the review action.
            review_status="closed",
            after_sale_status="none",
        )
        inventory = Inventory(
            sku_id=sku.id,
            on_hand_quantity=10,
            reserved_quantity=2,
            safety_stock_quantity=0,
            sold_quantity=0,
            inventory_status="active",
        )
        session.add_all([order_item, inventory])
        await session.flush()
        reservation = InventoryReservation(
            reservation_no=new_prefixed_ulid("irs_"),
            inventory_id=inventory.id,
            sku_id=sku.id,
            order_id=order.id,
            order_item_id=order_item.id,
            quantity=2,
            reservation_status="active",
            idempotency_key=f"order-test-{suffix}",
            expires_at=trade.expires_at,
        )
        session.add(reservation)
        await session.commit()
        order_no = order.order_no
        store_no = store.store_no
        trade_id = trade.id
        order_id = order.id
        item_id = order_item.id
        order_item_no = order_item.order_item_no
        inventory_id = inventory.id

    login = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": f"order_admin_{suffix}",
            "password": password,
            "client": {"client_type": "web", "device_name": "Order Admin Test"},
        },
    )
    challenge_id = login.json()["data"]["challenge_id"]
    mfa = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"order-mfa-{suffix}-001"},
        json={
            "challenge_id": challenge_id,
            "method": "totp",
            "code": pyotp.TOTP(provisioning.totp_secret).now(),
        },
    )
    assert mfa.status_code == 200, mfa.text
    headers = {"Authorization": f"Bearer {mfa.json()['data']['session']['access_token']}"}

    order_list = await client.get(
        "/api/v1/admin/orders",
        headers=headers,
        params={"store_id": store_no, "view": "pending_payment"},
    )
    assert order_list.status_code == 200, order_list.text
    assert "next_cursor" in order_list.json()["data"]
    listed_order = next(
        item for item in order_list.json()["data"]["items"] if item["order"]["order_id"] == order_no
    )
    assert listed_order["shippable_quantities"] == {order_item_no: 2}

    detail = await client.get(f"/api/v1/admin/orders/{order_no}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert "address" not in detail.text
    assert detail.json()["data"]["shippable_quantities"] == {order_item_no: 2}
    adjusted = await client.post(
        f"/api/v1/admin/orders/{order_no}/amount-adjustments",
        headers={
            **headers,
            "If-Match": detail.headers["etag"],
            "Idempotency-Key": f"order-adjust-{suffix}-001",
        },
        json={
            "adjustment_amount": {"minor_units": "-200", "currency": "CNY"},
            "reason_code": "MANUAL_PRICE_ADJUSTMENT",
            "reason": "补偿活动差价",
        },
    )
    assert adjusted.status_code == 200, adjusted.text
    assert adjusted.json()["data"]["order"]["amounts"]["payable_amount"]["minor_units"] == "1900"

    cancelled = await client.post(
        f"/api/v1/admin/orders/{order_no}/cancellations",
        headers={
            **headers,
            "If-Match": adjusted.headers["etag"],
            "Idempotency-Key": f"order-cancel-{suffix}-001",
        },
        json={
            "reason_code": "ADMIN_ORDER_CANCELLATION",
            "reason": "买卖双方确认取消",
        },
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["order"]["order_status"] == "cancelled"

    cancelled_list = await client.get(
        "/api/v1/admin/orders",
        headers=headers,
        params={"store_id": store_no, "view": "cancelled"},
    )
    assert cancelled_list.status_code == 200, cancelled_list.text
    assert [item["order"]["order_id"] for item in cancelled_list.json()["data"]["items"]] == [
        order_no
    ]

    another_store = await client.get(
        "/api/v1/admin/orders",
        headers=headers,
        params={"store_id": f"sto_missing_{suffix}", "view": "all"},
    )
    assert another_store.status_code == 200, another_store.text
    assert another_store.json()["data"]["items"] == []

    async for session in mysql_session():
        stored_trade = await session.get(TradeOrder, trade_id)
        stored_order = await session.get(Order, order_id)
        stored_item = await session.get(OrderItem, item_id)
        stored_inventory = await session.get(Inventory, inventory_id)
        assert stored_trade and stored_trade.payable_amount == 1900
        assert stored_order and stored_order.payable_amount == 1900
        assert stored_item and stored_item.payable_amount == 1800
        assert stored_inventory and stored_inventory.reserved_quantity == 0
        assert (
            await session.scalar(
                select(func.count(AdminOperationLog.id)).where(
                    AdminOperationLog.target_no.in_((order_no, stored_trade.trade_no))
                )
            )
            == 2
        )
        assert (
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.aggregate_no.in_((order_no, stored_trade.trade_no))
                )
            )
            == 2
        )
        stored_order.order_status = "closed"
        await session.commit()

    closed_list = await client.get(
        "/api/v1/admin/orders",
        headers=headers,
        params={"store_id": store_no, "view": "cancelled"},
    )
    assert closed_list.status_code == 200, closed_list.text
    assert [item["order"]["order_id"] for item in closed_list.json()["data"]["items"]] == [order_no]
