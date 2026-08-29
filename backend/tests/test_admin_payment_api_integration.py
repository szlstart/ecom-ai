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
from app.modules.identity.models import User
from app.modules.orders.models import Order, TradeOrder
from app.modules.payments.models import Payment, PaymentEvent
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


async def test_admin_payment_read_and_provider_reconciliation(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    password = f"Admin-Payment-{suffix}-Correct-Horse!"

    async for session in mysql_session():
        provisioning = await provision_platform_super_admin(
            session,
            security,
            username=f"payment_admin_{suffix}",
            password=password,
        )
        admin = await session.scalar(select(User).where(User.user_no == provisioning.user_no))
        assert admin is not None
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=admin.id,
            store_name=f"支付店铺 {suffix}",
            store_name_normalized=f"payment-store-{suffix}",
            store_status="active",
            rating_score=Decimal("5.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=now,
        )
        session.add(store)
        await session.flush()
        trade = TradeOrder(
            trade_no=new_prefixed_ulid("trd_"),
            checkout_session_id=None,
            checkout_no_snapshot=new_prefixed_ulid("chk_"),
            checkout_snapshot_hash=hashlib.sha256(f"payment-{suffix}".encode()).digest(),
            user_id=admin.id,
            order_source="buy_now",
            trade_status="pending_payment",
            goods_amount=1000,
            freight_amount=0,
            payable_amount=1000,
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
            payment_status="processing",
            fulfillment_status="unfulfilled",
            after_sale_status="none",
            goods_amount=1000,
            freight_amount=0,
            payable_amount=1000,
            adjustment_amount=0,
            paid_amount=0,
            refunded_amount=0,
            currency="CNY",
            policy_snapshot={"schema_version": 1},
            expires_at=trade.expires_at,
        )
        session.add(order)
        payment_no = new_prefixed_ulid("pay_")
        payment = Payment(
            payment_no=payment_no,
            trade_order_id=trade.id,
            user_id=admin.id,
            provider="fake",
            payment_method="fake_balance",
            provider_trade_no=f"fake_{payment_no}",
            provider_request_id=f"fake_request_{payment_no}",
            payment_status="pending",
            requested_amount=1000,
            paid_amount=0,
            refunded_amount=0,
            currency="CNY",
            expires_at=trade.expires_at,
        )
        session.add(payment)
        await session.commit()
        payment_internal_id = payment.id

    login = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": f"payment_admin_{suffix}",
            "password": password,
            "client": {"client_type": "web", "device_name": "Payment Admin Test"},
        },
    )
    challenge_id = login.json()["data"]["challenge_id"]
    mfa = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"payment-mfa-{suffix}-001"},
        json={
            "challenge_id": challenge_id,
            "method": "totp",
            "code": pyotp.TOTP(provisioning.totp_secret).now(),
        },
    )
    assert mfa.status_code == 200, mfa.text
    headers = {"Authorization": f"Bearer {mfa.json()['data']['session']['access_token']}"}

    listing = await client.get("/api/v1/admin/payments", headers=headers)
    assert listing.status_code == 200, listing.text
    assert payment_no in [item["payment"]["payment_id"] for item in listing.json()["data"]["items"]]
    detail = await client.get(f"/api/v1/admin/payments/{payment_no}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert f"fake_{payment_no}" not in detail.text
    assert detail.json()["data"]["provider_trade_no_masked"].startswith("fake")

    reconciliation_key = f"payment-reconcile-{suffix}-001"
    payload = {
        "reason_code": "PAYMENT_STATUS_RECONCILIATION",
        "reason": "支付结果长时间处于确认中",
    }
    reconciled = await client.post(
        f"/api/v1/admin/payments/{payment_no}/reconciliations",
        headers={
            **headers,
            "If-Match": detail.headers["etag"],
            "Idempotency-Key": reconciliation_key,
        },
        json=payload,
    )
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["data"]["provider_status"] == "pending"
    assert reconciled.json()["data"]["result"] == "no_change"
    replayed = await client.post(
        f"/api/v1/admin/payments/{payment_no}/reconciliations",
        headers={
            **headers,
            "If-Match": detail.headers["etag"],
            "Idempotency-Key": reconciliation_key,
        },
        json=payload,
    )
    assert replayed.status_code == 200
    assert replayed.json()["data"]["reconciled_at"] == reconciled.json()["data"]["reconciled_at"]

    async for session in mysql_session():
        stored = await session.get(Payment, payment_internal_id)
        assert stored and stored.payment_status == "pending" and stored.version == 1
        assert (
            await session.scalar(
                select(func.count(PaymentEvent.id)).where(
                    PaymentEvent.payment_id == payment_internal_id,
                    PaymentEvent.event_type == "reconciliation_observed",
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count(AdminOperationLog.id)).where(
                    AdminOperationLog.target_no == payment_no,
                    AdminOperationLog.action == "reconcile_payment",
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.aggregate_no == payment_no,
                    OutboxEvent.event_type == "payment.reconciled.v1",
                )
            )
            == 1
        )
