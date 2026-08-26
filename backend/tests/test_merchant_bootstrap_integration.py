import os
import secrets

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.bootstrap.merchant import STORE_OPERATOR_PERMISSIONS, provision_store_operator
from app.core.config import get_settings
from app.core.security import SecurityService
from app.database.mysql import mysql_session
from app.modules.stores.models import Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_store_operator_login_permissions_and_store_isolation(client: AsyncClient) -> None:
    suffix = secrets.token_hex(5)
    username = f"merchant_{suffix}"
    password = f"Merchant-{suffix}-Correct-Horse!"
    security = SecurityService(get_settings())

    async for session in mysql_session():
        provisioning = await provision_store_operator(
            session,
            security,
            username=username,
            password=password,
            store_name=f"商家测试店铺 {suffix}",
        )
        own_store = await session.scalar(
            select(Store).where(Store.store_no == provisioning.store_no)
        )
        assert own_store is not None
        foreign_store = Store(
            store_no=f"sto_foreign_{suffix}",
            owner_user_id=own_store.owner_user_id,
            store_name=f"隔离测试店铺 {suffix}",
            store_name_normalized=f"isolated-store-{suffix}",
            store_status="active",
        )
        session.add(foreign_store)
        await session.commit()
        own_store_id = own_store.id
        foreign_store_no = foreign_store.store_no

    login = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": username,
            "password": password,
            "client": {"client_type": "web", "device_name": "Merchant bootstrap test"},
        },
    )
    assert login.status_code == 200, login.text
    mfa = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"merchant-mfa-{suffix}"},
        json={
            "challenge_id": login.json()["data"]["challenge_id"],
            "method": "totp",
            "code": pyotp.TOTP(provisioning.totp_secret).now(),
        },
    )
    assert mfa.status_code == 200, mfa.text
    bootstrap = mfa.json()["data"]
    permissions = set(bootstrap["permission_codes"])
    assert permissions == set(STORE_OPERATOR_PERMISSIONS)
    assert "users:read" not in permissions
    assert bootstrap["scopes"] == [{"scope_type": "store", "scope_id": own_store_id}]
    headers = {"Authorization": f"Bearer {bootstrap['session']['access_token']}"}

    stores = await client.get("/api/v1/admin/stores", headers=headers)
    assert stores.status_code == 200, stores.text
    assert [item["store_id"] for item in stores.json()["data"]["items"]] == [
        provisioning.store_no
    ]

    forbidden = await client.get(f"/api/v1/admin/stores/{foreign_store_no}", headers=headers)
    assert forbidden.status_code == 404
