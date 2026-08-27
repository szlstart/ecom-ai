import os
import secrets

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
            store_name_normalized=f"隔离测试店铺 {suffix}".casefold(),
            store_status="active",
        )
        session.add(foreign_store)
        await session.commit()
        own_store_id = own_store.id
        foreign_store_no = foreign_store.store_no

    login = await client.post(
        "/api/v1/merchant/auth/login",
        json={
            "identifier": username,
            "password": password,
            "client": {"client_type": "web", "device_name": "Merchant bootstrap test"},
        },
    )
    assert login.status_code == 200, login.text

    platform_login = await client.post(
        "/api/v1/admin/auth/password-login",
        json={
            "identifier": username,
            "password": password,
            "client": {"client_type": "web", "device_name": "Wrong portal test"},
        },
    )
    assert platform_login.status_code == 401
    assert platform_login.json()["code"] == "ADMIN_AUTH_INVALID_CREDENTIALS"
    user_login = await client.post(
        "/api/v1/auth/login",
        json={
            "auth_method": "password",
            "identifier": username,
            "password": password,
            "client": {"client_type": "web", "device_name": "Wrong user portal test"},
        },
    )
    assert user_login.status_code == 401
    assert user_login.json()["code"] == "AUTH_INVALID_CREDENTIALS"
    bootstrap = login.json()["data"]
    assert bootstrap["session"]["session"]["client_type"] == "merchant"
    assert "challenge_id" not in bootstrap
    permissions = set(bootstrap["permission_codes"])
    assert permissions == set(STORE_OPERATOR_PERMISSIONS)
    assert "users:read" not in permissions
    assert bootstrap["scopes"] == [{"scope_type": "store", "scope_id": own_store_id}]
    headers = {"Authorization": f"Bearer {bootstrap['session']['access_token']}"}

    me = await client.get("/api/v1/admin/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["data"]["assurance_level"] == "aal1"
    assert "ecom_merchant_refresh" in client.cookies
    assert "ecom_merchant_csrf" in client.cookies
    assert "ecom_admin_refresh" not in client.cookies

    platform_navigation = await client.get("/api/v1/admin/navigation", headers=headers)
    assert platform_navigation.status_code == 403
    assert platform_navigation.json()["code"] == "AUTH_PORTAL_MISMATCH"

    wrong_refresh = await client.post(
        "/api/v1/admin/auth/token-refresh",
        headers={"X-CSRF-Token": bootstrap["session"]["csrf_token"]},
    )
    assert wrong_refresh.status_code == 401
    refreshed_response = await client.post(
        "/api/v1/merchant/auth/token-refresh",
        headers={"X-CSRF-Token": bootstrap["session"]["csrf_token"]},
    )
    assert refreshed_response.status_code == 200, refreshed_response.text
    refreshed = refreshed_response.json()["data"]
    assert refreshed["session"]["client_type"] == "merchant"
    headers = {"Authorization": f"Bearer {refreshed['access_token']}"}

    reauthenticated = await client.post(
        "/api/v1/merchant/auth/reauthentications",
        headers=headers,
        json={"password": password},
    )
    assert reauthenticated.status_code == 200, reauthenticated.text
    assert reauthenticated.json()["data"]["assurance_level"] == "aal1"

    stores = await client.get("/api/v1/admin/stores", headers=headers)
    assert stores.status_code == 200, stores.text
    assert [item["store_id"] for item in stores.json()["data"]["items"]] == [
        provisioning.store_no
    ]

    store_detail = await client.get(
        f"/api/v1/admin/stores/{provisioning.store_no}", headers=headers
    )
    duplicate_name = await client.patch(
        f"/api/v1/admin/stores/{provisioning.store_no}",
        headers={**headers, "If-Match": store_detail.headers["etag"]},
        json={"store_name": f"隔离测试店铺 {suffix}"},
    )
    assert duplicate_name.status_code == 409, duplicate_name.text
    assert duplicate_name.json()["code"] == "STORE_NAME_ALREADY_EXISTS"

    renamed = await client.patch(
        f"/api/v1/admin/stores/{provisioning.store_no}",
        headers={**headers, "If-Match": store_detail.headers["etag"]},
        json={"store_name": f"商家正式店铺 {suffix}"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["data"]["store_name_changed_at"] is not None
    assert renamed.json()["data"]["store_name_change_available_at"] is not None

    cooldown = await client.patch(
        f"/api/v1/admin/stores/{provisioning.store_no}",
        headers={**headers, "If-Match": renamed.headers["etag"]},
        json={"store_name": f"商家再次改名 {suffix}"},
    )
    assert cooldown.status_code == 409, cooldown.text
    assert cooldown.json()["code"] == "STORE_NAME_CHANGE_COOLDOWN"

    forbidden = await client.get(f"/api/v1/admin/stores/{foreign_store_no}", headers=headers)
    assert forbidden.status_code == 404

    logout = await client.post(
        "/api/v1/merchant/auth/logout",
        headers={**headers, "X-CSRF-Token": refreshed["csrf_token"]},
    )
    assert logout.status_code == 204
