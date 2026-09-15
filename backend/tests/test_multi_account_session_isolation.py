import os
import secrets
from typing import Any, cast

import pytest
from httpx import AsyncClient

from app.bootstrap.merchant import provision_store_operator
from app.core.config import get_settings
from app.core.security import SecurityService
from app.database.mysql import mysql_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def _register(client: AsyncClient, label: str) -> dict[str, Any]:
    suffix = secrets.token_hex(5)
    config = (await client.get("/api/v1/auth/registration-config")).json()["data"]
    captcha = config["captcha"]
    left, operator, right, _, _ = captcha["question"].split()
    answer = int(left) + int(right) if operator == "+" else int(left) - int(right)
    response = await client.post(
        "/api/v1/auth/registrations",
        headers={"Idempotency-Key": f"multi-tab-{label}-{suffix}"},
        json={
            "username": f"tabs_{label}_{suffix}",
            "email": f"tabs_{label}_{suffix}@example.com",
            "captcha_id": captcha["captcha_id"],
            "captcha_answer": str(answer),
            "password": f"Password-{label}-{suffix}",
            "config_version": config["config_version"],
            "agreement_acceptances": [
                {
                    "document_type": item["document_type"],
                    "document_version": item["document_version"],
                }
                for item in config["required_agreements"]
            ],
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json()["data"])


async def test_two_shopper_sessions_share_one_cookie_jar_without_identity_crossover(
    client: AsyncClient,
) -> None:
    first = await _register(client, "first")
    second = await _register(client, "second")
    first_session = first["session"]["session_id"]
    second_session = second["session"]["session_id"]

    assert f"ecom_user_refresh_{first_session}" in client.cookies
    assert f"ecom_user_refresh_{second_session}" in client.cookies

    first_resume = await client.post(
        "/api/v1/auth/session-resume",
        headers={
            "X-Auth-Session": first_session,
            "X-CSRF-Token": first["csrf_token"],
        },
    )
    second_resume = await client.post(
        "/api/v1/auth/session-resume",
        headers={
            "X-Auth-Session": second_session,
            "X-CSRF-Token": second["csrf_token"],
        },
    )
    assert first_resume.status_code == 200, first_resume.text
    assert second_resume.status_code == 200, second_resume.text
    assert first_resume.json()["data"]["user"]["user_id"] == first["user"]["user_id"]
    assert second_resume.json()["data"]["user"]["user_id"] == second["user"]["user_id"]

    first_logout = await client.post(
        "/api/v1/auth/logout",
        headers={
            "Authorization": f"Bearer {first['access_token']}",
            "X-Auth-Session": first_session,
            "X-CSRF-Token": first["csrf_token"],
        },
    )
    assert first_logout.status_code == 204, first_logout.text
    second_still_active = await client.post(
        "/api/v1/auth/session-resume",
        headers={
            "X-Auth-Session": second_session,
            "X-CSRF-Token": second["csrf_token"],
        },
    )
    assert second_still_active.status_code == 200, second_still_active.text
    assert second_still_active.json()["data"]["user"]["user_id"] == second["user"]["user_id"]


async def test_two_merchant_sessions_share_one_cookie_jar_without_store_crossover(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    credentials = [
        (f"shop_a_{suffix}", f"Password-A-{suffix}", f"隔离店铺 A {suffix}"),
        (f"shop_b_{suffix}", f"Password-B-{suffix}", f"隔离店铺 B {suffix}"),
    ]
    async for session in mysql_session():
        for username, password, store_name in credentials:
            await provision_store_operator(
                session,
                SecurityService(get_settings()),
                username=username,
                password=password,
                store_name=store_name,
            )

    bootstraps: list[dict[str, Any]] = []
    for username, password, _store_name in credentials:
        response = await client.post(
            "/api/v1/merchant/auth/login",
            json={
                "identifier": username,
                "password": password,
                "client": {"client_type": "web", "device_name": "multi-merchant-tab"},
            },
        )
        assert response.status_code == 200, response.text
        bootstraps.append(response.json()["data"])

    first_session = bootstraps[0]["session"]["session"]["session_id"]
    second_session = bootstraps[1]["session"]["session"]["session_id"]
    assert f"ecom_merchant_refresh_{first_session}" in client.cookies
    assert f"ecom_merchant_refresh_{second_session}" in client.cookies

    resumed_users: list[str] = []
    for bootstrap, session_id in zip(bootstraps, (first_session, second_session), strict=True):
        response = await client.post(
            "/api/v1/merchant/auth/session-resume",
            headers={
                "X-Auth-Session": session_id,
                "X-CSRF-Token": bootstrap["session"]["csrf_token"],
            },
        )
        assert response.status_code == 200, response.text
        resumed_users.append(response.json()["data"]["user"]["user_id"])

    assert resumed_users == [
        bootstraps[0]["session"]["user"]["user_id"],
        bootstraps[1]["session"]["user"]["user_id"],
    ]
    assert bootstraps[0]["scopes"] != bootstraps[1]["scopes"]
