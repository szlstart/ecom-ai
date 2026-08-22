import os
import secrets

import pytest
from httpx import AsyncClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_user_authentication_profile_address_and_session_lifecycle(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(4)
    username = f"user_{suffix}"
    email = f"user_{suffix}@example.com"
    password = f"Correct-Horse-{suffix}-Battery-Staple!"

    config_response = await client.get("/api/v1/auth/registration-config")
    assert config_response.status_code == 200
    registration_config = config_response.json()["data"]
    assert len(registration_config["required_agreements"]) == 2

    agreement = registration_config["required_agreements"][0]
    legal_response = await client.get(
        f"/api/v1{agreement['content_url']}",
    )
    assert legal_response.status_code == 200
    assert legal_response.json()["data"]["content_hash"] == agreement["content_hash"]

    verification_response = await client.post(
        "/api/v1/auth/verification-codes",
        json={
            "purpose": "register",
            "target_type": "email",
            "target": email,
            "locale": "zh-CN",
            "challenge_token": None,
            "change_ticket_id": None,
        },
    )
    assert verification_response.status_code == 202
    verification_id = verification_response.json()["data"]["verification_id"]

    registration_response = await client.post(
        "/api/v1/auth/registrations",
        headers={"Idempotency-Key": f"registration-{suffix}-0001"},
        json={
            "username": username,
            "target_type": "email",
            "target": email,
            "verification_id": verification_id,
            "verification_code": "000000",
            "password": password,
            "config_version": registration_config["config_version"],
            "agreement_acceptances": [
                {
                    "document_type": item["document_type"],
                    "document_version": item["document_version"],
                }
                for item in registration_config["required_agreements"]
            ],
            "locale": "zh-CN",
            "timezone": "Asia/Shanghai",
        },
    )
    assert registration_response.status_code == 201, registration_response.text
    assert "HttpOnly" in registration_response.headers["set-cookie"]
    bootstrap = registration_response.json()["data"]
    access_token = bootstrap["access_token"]
    csrf_token = bootstrap["csrf_token"]
    auth_headers = {"Authorization": f"Bearer {access_token}"}

    profile_response = await client.get("/api/v1/users/me", headers=auth_headers)
    assert profile_response.status_code == 200
    assert profile_response.json()["data"]["username"] == username
    profile_etag = profile_response.headers["etag"]

    update_response = await client.patch(
        "/api/v1/users/me",
        headers={**auth_headers, "If-Match": profile_etag},
        json={"nickname": f"Member {suffix}"},
    )
    assert update_response.status_code == 200
    assert update_response.headers["etag"] != profile_etag

    missing_precondition = await client.patch(
        "/api/v1/users/me",
        headers=auth_headers,
        json={"nickname": "Missing ETag"},
    )
    assert missing_precondition.status_code == 428
    assert missing_precondition.json()["code"] == "PRECONDITION_REQUIRED"

    address_response = await client.post(
        "/api/v1/users/me/addresses",
        headers={**auth_headers, "Idempotency-Key": f"address-{suffix}-0000001"},
        json={
            "recipient_name": "张三",
            "phone": "+8613800000000",
            "country_code": "CN",
            "province_code": "440000",
            "city_code": "440300",
            "district_code": "440305",
            "address": "科技园测试路 1 号",
            "postal_code": "518000",
            "label": "家",
            "is_default": True,
        },
    )
    assert address_response.status_code == 201, address_response.text
    address = address_response.json()["data"]
    assert address["phone"] == "+8613800000000"
    assert address["is_default"] is True

    address_update = await client.patch(
        f"/api/v1/users/me/addresses/{address['address_id']}",
        headers={**auth_headers, "If-Match": address_response.headers["etag"]},
        json={"label": "常用地址"},
    )
    assert address_update.status_code == 200
    assert address_update.json()["data"]["label"] == "常用地址"

    refresh_response = await client.post(
        "/api/v1/auth/token-refresh",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert refresh_response.status_code == 200, refresh_response.text
    refreshed = refresh_response.json()["data"]
    refreshed_headers = {"Authorization": f"Bearer {refreshed['access_token']}"}

    old_access_response = await client.get("/api/v1/users/me", headers=auth_headers)
    assert old_access_response.status_code == 401

    sessions_response = await client.get("/api/v1/auth/sessions", headers=refreshed_headers)
    assert sessions_response.status_code == 200
    assert any(item["is_current"] for item in sessions_response.json()["data"])

    logout_response = await client.post(
        "/api/v1/auth/logout",
        headers={**refreshed_headers, "X-CSRF-Token": refreshed["csrf_token"]},
    )
    assert logout_response.status_code == 204

    revoked_response = await client.get("/api/v1/users/me", headers=refreshed_headers)
    assert revoked_response.status_code == 401

    login_response = await client.post(
        "/api/v1/auth/login",
        json={
            "auth_method": "password",
            "identifier": username,
            "password": password,
            "client": {"client_type": "web", "device_name": "Integration Test"},
            "challenge_token": None,
        },
    )
    assert login_response.status_code == 200, login_response.text
    assert login_response.json()["data"]["user"]["username"] == username
