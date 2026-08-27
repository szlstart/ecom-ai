import os
import secrets

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.bootstrap.merchant import provision_store_operator
from app.core.config import get_settings
from app.core.security import SecurityService
from app.database.mysql import mysql_session
from app.modules.identity.models import User
from app.modules.stores.models import Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_merchant_zero_revenue_and_physical_account_deletion(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    username = f"merchant_delete_{suffix}"
    password = f"Merchant-{suffix}-Password!"
    security = SecurityService(get_settings())
    async for session in mysql_session():
        merchant = await provision_store_operator(
            session,
            security,
            username=username,
            password=password,
            store_name=f"可注销店铺 {suffix}",
        )

    login = await client.post(
        "/api/v1/merchant/auth/login",
        json={
            "identifier": username,
            "password": password,
            "client": {"client_type": "web", "device_name": "Deletion test"},
        },
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['data']['session']['access_token']}"}

    revenue = await client.get(
        f"/api/v1/merchant/stores/{merchant.store_no}/revenue", headers=headers
    )
    assert revenue.status_code == 200, revenue.text
    assert revenue.json()["data"] == {
        "store_id": merchant.store_no,
        "gross_sales": {"minor_units": "0", "currency": "CNY"},
        "refunded_amount": {"minor_units": "0", "currency": "CNY"},
        "net_revenue": {"minor_units": "0", "currency": "CNY"},
        "today_revenue": {"minor_units": "0", "currency": "CNY"},
        "yesterday_revenue": {"minor_units": "0", "currency": "CNY"},
        "last_30_days_revenue": {"minor_units": "0", "currency": "CNY"},
        "all_order_count": 0,
        "completed_order_count": 0,
        "pending_payment_count": 0,
        "pending_shipment_count": 0,
        "in_transit_count": 0,
        "after_sale_pending_count": 0,
        "cancelled_count": 0,
    }

    deletion = await client.request(
        "DELETE",
        "/api/v1/merchant/account",
        headers=headers,
        json={"confirmation": "DELETE_MY_STORE_AND_ACCOUNT"},
    )
    assert deletion.status_code == 204, deletion.text
    async for session in mysql_session():
        assert await session.scalar(select(User).where(User.username == username)) is None
        assert (
            await session.scalar(select(Store).where(Store.store_no == merchant.store_no)) is None
        )
