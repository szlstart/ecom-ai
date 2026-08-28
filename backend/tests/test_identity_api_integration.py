import asyncio
import json
import os
import secrets
from datetime import timedelta

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.bootstrap.admin import provision_platform_super_admin
from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, canonical_request_hash, utc_now
from app.database.mysql import mysql_session
from app.modules.identity.models import User, UserCredential
from app.modules.rbac.models import AdminApprovalRequest, Role, UserRole
from app.workers.admin_approval_executor import (
    AdminApprovalExecutor,
    ApprovalExecutionResult,
)

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
    registration_email = f"user_{suffix}@example.com"
    password = f"Correct-Horse-{suffix}-Battery-Staple!"

    config_response = await client.get("/api/v1/auth/registration-config")
    assert config_response.status_code == 200
    registration_config = config_response.json()["data"]
    assert len(registration_config["required_agreements"]) == 2
    assert registration_config["password_policy"] == {
        "non_empty": True,
        "forbid_whitespace": True,
        "allow_unicode": True,
        "policy_version": "password_v4",
    }
    captcha = registration_config["captcha"]
    left, operator, right, _, _ = captcha["question"].split()
    captcha_answer = int(left) + int(right) if operator == "+" else int(left) - int(right)

    wrong_captcha_response = await client.post(
        "/api/v1/auth/registrations",
        headers={"Idempotency-Key": f"registration-{suffix}-wrong"},
        json={
            "username": username,
            "email": registration_email,
            "captcha_id": captcha["captcha_id"],
            "captcha_answer": str(captcha_answer + 1),
            "password": password,
            "config_version": registration_config["config_version"],
            "agreement_acceptances": [
                {
                    "document_type": item["document_type"],
                    "document_version": item["document_version"],
                }
                for item in registration_config["required_agreements"]
            ],
        },
    )
    assert wrong_captcha_response.status_code == 422
    assert wrong_captcha_response.json()["code"] == "REGISTRATION_CAPTCHA_INVALID"

    agreement = registration_config["required_agreements"][0]
    legal_response = await client.get(
        f"/api/v1{agreement['content_url']}",
    )
    assert legal_response.status_code == 200
    assert legal_response.json()["data"]["content_hash"] == agreement["content_hash"]

    registration_response = await client.post(
        "/api/v1/auth/registrations",
        headers={"Idempotency-Key": f"registration-{suffix}-0001"},
        json={
            "username": username,
            "email": registration_email,
            "captcha_id": captcha["captcha_id"],
            "captcha_answer": str(captcha_answer),
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

    duplicate_config = (await client.get("/api/v1/auth/registration-config")).json()["data"]
    duplicate_captcha = duplicate_config["captcha"]
    duplicate_left, duplicate_operator, duplicate_right, _, _ = duplicate_captcha[
        "question"
    ].split()
    duplicate_answer = (
        int(duplicate_left) + int(duplicate_right)
        if duplicate_operator == "+"
        else int(duplicate_left) - int(duplicate_right)
    )
    duplicate_username_response = await client.post(
        "/api/v1/auth/registrations",
        headers={"Idempotency-Key": f"registration-{suffix}-duplicate-username"},
        json={
            "username": username,
            "email": f"duplicate_{suffix}@example.com",
            "captcha_id": duplicate_captcha["captcha_id"],
            "captcha_answer": str(duplicate_answer),
            "password": password,
            "config_version": duplicate_config["config_version"],
            "agreement_acceptances": [
                {
                    "document_type": item["document_type"],
                    "document_version": item["document_version"],
                }
                for item in duplicate_config["required_agreements"]
            ],
        },
    )
    assert duplicate_username_response.status_code == 409
    duplicate_problem = duplicate_username_response.json()
    assert duplicate_problem["detail"] == "该用户名已被注册，请更换一个用户名。"
    assert duplicate_problem["errors"] == [
        {
            "pointer": "/username",
            "code": "REGISTRATION_USERNAME_UNAVAILABLE",
            "message": "该用户名已被注册，请更换一个用户名。",
        }
    ]

    reused_captcha_response = await client.post(
        "/api/v1/auth/registrations",
        headers={"Idempotency-Key": f"registration-{suffix}-reused"},
        json={
            "username": f"other_{suffix}",
            "email": f"other_{suffix}@example.com",
            "captcha_id": captcha["captcha_id"],
            "captcha_answer": str(captcha_answer),
            "password": password,
            "config_version": registration_config["config_version"],
            "agreement_acceptances": [
                {
                    "document_type": item["document_type"],
                    "document_version": item["document_version"],
                }
                for item in registration_config["required_agreements"]
            ],
        },
    )
    assert reused_captcha_response.status_code == 422
    assert reused_captcha_response.json()["code"] == "REGISTRATION_CAPTCHA_INVALID"

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
            "recipient_name": "张",
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
    repeated_address_response = await client.post(
        "/api/v1/users/me/addresses",
        headers={**auth_headers, "Idempotency-Key": f"address-{suffix}-0000001"},
        json={
            "recipient_name": "张",
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
    assert repeated_address_response.status_code == 201
    assert repeated_address_response.json()["data"]["address_id"] == address["address_id"]

    concurrent_payload = {
        "recipient_name": "李四",
        "phone": "+8613900000000",
        "country_code": "CN",
        "province_code": "310000",
        "city_code": "310100",
        "district_code": "310104",
        "address": "并发测试路 2 号",
        "postal_code": "200000",
        "label": "并发测试",
        "is_default": False,
    }
    concurrent_headers = {
        **auth_headers,
        "Idempotency-Key": f"address-concurrent-{suffix}-001",
    }
    concurrent_responses = await asyncio.gather(
        *[
            client.post(
                "/api/v1/users/me/addresses",
                headers=concurrent_headers,
                json=concurrent_payload,
            )
            for _ in range(2)
        ]
    )
    successful = [response for response in concurrent_responses if response.status_code == 201]
    in_progress = [response for response in concurrent_responses if response.status_code == 409]
    assert successful
    assert len(successful) + len(in_progress) == 2
    assert all(response.json()["code"] == "IDEMPOTENCY_IN_PROGRESS" for response in in_progress), [
        response.json() for response in in_progress
    ]
    recovered_response = await client.post(
        "/api/v1/users/me/addresses",
        headers=concurrent_headers,
        json=concurrent_payload,
    )
    assert recovered_response.status_code == 201
    assert (
        recovered_response.json()["data"]["address_id"]
        == successful[0].json()["data"]["address_id"]
    )
    assert all(
        response.json()["data"]["address_id"] == recovered_response.json()["data"]["address_id"]
        for response in successful
    )

    address_update = await client.patch(
        f"/api/v1/users/me/addresses/{address['address_id']}",
        headers={**auth_headers, "If-Match": address_response.headers["etag"]},
        json={"recipient_name": "李", "label": "常用地址"},
    )
    assert address_update.status_code == 200
    assert address_update.json()["data"]["recipient_name"] == "李"
    assert address_update.json()["data"]["label"] == "常用地址"

    resumed_response = await client.post(
        "/api/v1/auth/session-resume",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resumed_response.status_code == 200, resumed_response.text
    resumed = resumed_response.json()["data"]
    assert resumed["session"]["session_id"] == bootstrap["session"]["session_id"]
    assert resumed["access_token"]
    original_access_after_resume = await client.get("/api/v1/users/me", headers=auth_headers)
    assert original_access_after_resume.status_code == 200

    changed_email = f"changed_{suffix}@example.com"
    contact_change_response = await client.post(
        "/api/v1/users/me/contact-changes",
        headers={
            **auth_headers,
            "Idempotency-Key": f"contact-change-{suffix}-001",
        },
        json={"new_email": changed_email},
    )
    assert contact_change_response.status_code == 200, contact_change_response.text

    security_response = await client.get("/api/v1/users/me/security", headers=auth_headers)
    assert security_response.status_code == 200
    assert security_response.json()["data"]["current_email"] == changed_email
    assert any(
        item["type"] == "email" and item["masked"].startswith("ch")
        for item in security_response.json()["data"]["bound_accounts"]
    )

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

    reset_hint = await client.post(
        "/api/v1/auth/password-reset-hints",
        json={"username": username},
    )
    assert reset_hint.status_code == 200, reset_hint.text
    assert reset_hint.json()["data"]["email_masked"].endswith("@example.com")
    mismatched_ticket = await client.post(
        "/api/v1/auth/password-reset-tickets",
        json={"username": username, "email": f"wrong_{suffix}@example.com"},
    )
    assert mismatched_ticket.status_code == 422
    assert mismatched_ticket.json()["code"] == "PASSWORD_RECOVERY_EMAIL_MISMATCH"
    reset_ticket = await client.post(
        "/api/v1/auth/password-reset-tickets",
        json={"username": username, "email": changed_email},
    )
    assert reset_ticket.status_code == 200, reset_ticket.text
    new_password = f"Reset-Correct-Horse-{suffix}-Battery!"
    reset_headers = {"Idempotency-Key": f"password-reset-{suffix}-001"}
    reset_payload = {
        "reset_ticket": reset_ticket.json()["data"]["reset_ticket"],
        "new_password": new_password,
    }
    reset = await client.post(
        "/api/v1/auth/password-resets",
        headers=reset_headers,
        json=reset_payload,
    )
    assert reset.status_code == 200, reset.text
    reset_replay = await client.post(
        "/api/v1/auth/password-resets",
        headers=reset_headers,
        json=reset_payload,
    )
    assert reset_replay.status_code == 200

    old_password_login = await client.post(
        "/api/v1/auth/login",
        json={
            "auth_method": "password",
            "identifier": username,
            "password": password,
            "client": {"client_type": "web", "device_name": "Old Password"},
            "challenge_token": None,
        },
    )
    assert old_password_login.status_code == 401
    new_password_login = await client.post(
        "/api/v1/auth/login",
        json={
            "auth_method": "password",
            "identifier": username,
            "password": new_password,
            "client": {"client_type": "web", "device_name": "Closure Test"},
            "challenge_token": None,
        },
    )
    assert new_password_login.status_code == 200, new_password_login.text
    wallet_auth = {"Authorization": f"Bearer {new_password_login.json()['data']['access_token']}"}
    initial_wallet = await client.get("/api/v1/users/me/wallet", headers=wallet_auth)
    assert initial_wallet.status_code == 200, initial_wallet.text
    assert initial_wallet.json()["data"]["balance"]["minor_units"] == "0"
    recharge_headers = {
        **wallet_auth,
        "Idempotency-Key": f"wallet-recharge-{suffix}-001",
    }
    recharge = await client.post(
        "/api/v1/users/me/wallet/recharges",
        headers=recharge_headers,
        json={
            "channel": "wechat",
            "amount": {"minor_units": "12345", "currency": "CNY"},
        },
    )
    assert recharge.status_code == 201, recharge.text
    assert recharge.json()["data"]["wallet"]["balance"]["minor_units"] == "12345"
    recharge_replay = await client.post(
        "/api/v1/users/me/wallet/recharges",
        headers=recharge_headers,
        json={
            "channel": "wechat",
            "amount": {"minor_units": "12345", "currency": "CNY"},
        },
    )
    assert recharge_replay.status_code == 201, recharge_replay.text
    assert recharge_replay.json()["data"]["wallet"]["balance"]["minor_units"] == "12345"
    transactions = await client.get("/api/v1/users/me/wallet/transactions", headers=wallet_auth)
    assert transactions.status_code == 200
    assert len(transactions.json()["data"]["items"]) == 1

    deletion = await client.request(
        "DELETE",
        "/api/v1/users/me",
        headers=wallet_auth,
        json={"confirmation": "DELETE_MY_ACCOUNT"},
    )
    assert deletion.status_code == 204, deletion.text
    assert (await client.get("/api/v1/users/me", headers=wallet_auth)).status_code == 401
    async for session in mysql_session():
        assert await session.scalar(select(User).where(User.username == username)) is None


async def test_admin_password_mfa_audience_and_reauthentication_lifecycle(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(4)
    username = f"admin_{suffix}"
    password = f"Admin-Correct-Horse-{suffix}-Battery!"
    target_username = f"target_{suffix}"
    target_email = f"target_{suffix}@example.com"
    security = SecurityService(get_settings())
    async for session in mysql_session():
        provisioning = await provision_platform_super_admin(
            session,
            security,
            username=username,
            password=password,
        )
        role = await session.scalar(select(Role).where(Role.role_code == "user"))
        assert role is not None
        now = utc_now()
        target = User(
            user_no=new_prefixed_ulid("usr_"),
            username=target_username,
            username_normalized=target_username,
            nickname=target_username,
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            registered_at=now,
        )
        session.add(target)
        await session.flush()
        session.add_all(
            [
                UserCredential(
                    user_id=target.id,
                    credential_type="password",
                    secret_hash=security.hash_password(f"Target-Correct-Horse-{suffix}-Battery!"),
                    algorithm="argon2id",
                    is_primary=True,
                    is_verified=True,
                    verified_at=now,
                    password_changed_at=now,
                    credential_status="active",
                ),
                UserCredential(
                    user_id=target.id,
                    credential_type="email",
                    identifier_ciphertext=security.encrypt("user-credential:email", target_email),
                    identifier_hash=security.keyed_hash("credential-identifier", target_email),
                    key_version=1,
                    is_primary=True,
                    is_verified=True,
                    verified_at=now,
                    credential_status="active",
                ),
                UserRole(
                    user_id=target.id,
                    role_id=role.id,
                    grant_no=new_prefixed_ulid("grt_"),
                    scope_type="platform",
                    scope_id=0,
                    grant_status="active",
                    active_grant_key=security.keyed_hash(
                        "active-role-grant", f"{target.id}:{role.id}:platform:0"
                    ),
                    granted_by=target.id,
                    granted_at=now,
                    grant_reason="integration_test_user",
                ),
            ]
        )
        await session.commit()
        target_user_no = target.user_no

    login_response = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": username,
            "password": password,
            "client": {"client_type": "web", "device_name": "Admin Integration Test"},
        },
    )
    assert login_response.status_code == 200, login_response.text
    challenge = login_response.json()["data"]
    assert "totp" in challenge["allowed_methods"]

    password_login = await client.post(
        "/api/v1/admin/auth/password-login",
        json={
            "identifier": username,
            "password": password,
            "client": {"client_type": "web", "device_name": "Admin password-only test"},
        },
    )
    assert password_login.status_code == 200, password_login.text
    password_bootstrap = password_login.json()["data"]
    assert password_bootstrap["session"]["session"]["client_type"] == "admin_password"
    assert password_bootstrap["scopes"] == [{"scope_type": "platform", "scope_id": 0}]
    assert "ecom_admin_refresh" in client.cookies
    assert "ecom_admin_csrf" in client.cookies
    assert "ecom_merchant_refresh" not in client.cookies

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

    merchant_login = await client.post(
        "/api/v1/merchant/auth/login",
        json={
            "identifier": username,
            "password": password,
            "client": {"client_type": "web", "device_name": "Wrong portal test"},
        },
    )
    assert merchant_login.status_code == 401
    assert merchant_login.json()["code"] == "MERCHANT_AUTH_INVALID_CREDENTIALS"

    mfa_payload = {
        "challenge_id": challenge["challenge_id"],
        "method": "totp",
        "code": pyotp.TOTP(provisioning.totp_secret).now(),
    }
    mfa_response = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"admin-mfa-{suffix}-0001"},
        json=mfa_payload,
    )
    assert mfa_response.status_code == 200, mfa_response.text
    assert "Path=/api/v1/admin/auth" in mfa_response.headers["set-cookie"]
    first_bootstrap = mfa_response.json()["data"]
    repeated_mfa_response = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"admin-mfa-{suffix}-0001"},
        json=mfa_payload,
    )
    assert repeated_mfa_response.status_code == 200, repeated_mfa_response.text
    bootstrap = repeated_mfa_response.json()["data"]
    assert (
        bootstrap["session"]["session"]["session_id"]
        != first_bootstrap["session"]["session"]["session_id"]
    )
    admin_session = bootstrap["session"]
    admin_headers = {"Authorization": f"Bearer {admin_session['access_token']}"}
    assert "users:read" in bootstrap["permission_codes"]

    me_response = await client.get("/api/v1/admin/me", headers=admin_headers)
    assert me_response.status_code == 200
    assert me_response.json()["data"]["assurance_level"] == "aal2"

    admin_sessions_response = await client.get("/api/v1/admin/auth/sessions", headers=admin_headers)
    assert admin_sessions_response.status_code == 200
    assert len(admin_sessions_response.json()["data"]) == 1
    assert admin_sessions_response.json()["data"][0]["is_current"] is True

    navigation_response = await client.get("/api/v1/admin/navigation", headers=admin_headers)
    assert navigation_response.status_code == 200
    assert any(item["code"] == "users" for item in navigation_response.json()["data"]["items"])
    assert any(item["code"] == "products" for item in navigation_response.json()["data"]["items"])
    assert any(item["code"] == "batch-jobs" for item in navigation_response.json()["data"]["items"])
    assert {
        "orders",
        "payments",
        "refunds",
        "refund-appeals",
        "reviews",
        "dead-letter-events",
    } <= {item["code"] for item in navigation_response.json()["data"]["items"]}

    audience_violation = await client.get("/api/v1/users/me", headers=admin_headers)
    assert audience_violation.status_code == 403
    assert audience_violation.json()["code"] == "AUTH_AUDIENCE_MISMATCH"

    reauth_response = await client.post(
        "/api/v1/admin/auth/reauthentications",
        headers=admin_headers,
        json={
            "password": password,
            "method": "recovery_code",
            "code": provisioning.recovery_codes[0],
        },
    )
    assert reauth_response.status_code == 200, reauth_response.text

    target_response = await client.get(
        f"/api/v1/admin/users/{target_user_no}",
        headers=admin_headers,
    )
    assert target_response.status_code == 200
    target_etag = target_response.headers["etag"]

    suspend_response = await client.post(
        f"/api/v1/admin/users/{target_user_no}/status-changes",
        headers={
            **admin_headers,
            "If-Match": target_etag,
            "Idempotency-Key": f"suspend-{suffix}-0000001",
        },
        json={
            "action": "suspend",
            "reason_code": "security_test",
            "reason": "Integration test suspension",
            "expires_at": None,
        },
    )
    assert suspend_response.status_code == 200, suspend_response.text
    assert suspend_response.json()["data"]["account_status"] == "suspended"

    resume_response = await client.post(
        f"/api/v1/admin/users/{target_user_no}/status-changes",
        headers={
            **admin_headers,
            "If-Match": suspend_response.headers["etag"],
            "Idempotency-Key": f"resume-{suffix}-00000001",
        },
        json={
            "action": "resume",
            "reason_code": "security_cleared",
            "reason": "Integration test resume",
            "expires_at": None,
        },
    )
    assert resume_response.status_code == 200, resume_response.text

    status_events_response = await client.get(
        f"/api/v1/admin/users/{target_user_no}/status-events",
        headers=admin_headers,
    )
    assert status_events_response.status_code == 200
    assert [item["to_status"] for item in status_events_response.json()["data"][:2]] == [
        "active",
        "suspended",
    ]

    sensitive_grant_response = await client.post(
        f"/api/v1/admin/users/{target_user_no}/sensitive-field-access-grants",
        headers={
            **admin_headers,
            "Idempotency-Key": f"sensitive-{suffix}-0001",
        },
        json={
            "fields": ["email"],
            "purpose_code": "security_investigation",
            "reason": "Verify the target account during an integration test",
            "ttl_seconds": 300,
        },
    )
    assert sensitive_grant_response.status_code == 201, sensitive_grant_response.text
    sensitive_grant_id = sensitive_grant_response.json()["data"]["grant_id"]

    sensitive_fields_response = await client.get(
        f"/api/v1/admin/users/{target_user_no}/sensitive-fields",
        headers={
            **admin_headers,
            "X-Sensitive-Access-Grant": sensitive_grant_id,
        },
    )
    assert sensitive_fields_response.status_code == 200
    assert sensitive_fields_response.json()["data"]["values"]["email"] == target_email
    replayed_sensitive_grant = await client.get(
        f"/api/v1/admin/users/{target_user_no}/sensitive-fields",
        headers={
            **admin_headers,
            "X-Sensitive-Access-Grant": sensitive_grant_id,
        },
    )
    assert replayed_sensitive_grant.status_code == 403

    revocable_grant_response = await client.post(
        f"/api/v1/admin/users/{target_user_no}/sensitive-field-access-grants",
        headers={
            **admin_headers,
            "Idempotency-Key": f"sensitive-revoke-{suffix}-0001",
        },
        json={
            "fields": ["email"],
            "purpose_code": "security_investigation",
            "reason": "Verify explicit sensitive grant revocation in integration test",
            "ttl_seconds": 300,
        },
    )
    assert revocable_grant_response.status_code == 201
    revocable_grant_id = revocable_grant_response.json()["data"]["grant_id"]
    sensitive_revoke_response = await client.post(
        f"/api/v1/admin/sensitive-field-access-grants/{revocable_grant_id}/revocations",
        headers={
            **admin_headers,
            "If-Match": revocable_grant_response.headers["etag"],
            "Idempotency-Key": f"sensitive-grant-revoke-{suffix}-001",
        },
        json={"reason": "Integration test explicit revocation"},
    )
    assert sensitive_revoke_response.status_code == 200
    revoked_sensitive_read = await client.get(
        f"/api/v1/admin/users/{target_user_no}/sensitive-fields",
        headers={
            **admin_headers,
            "X-Sensitive-Access-Grant": revocable_grant_id,
        },
    )
    assert revoked_sensitive_read.status_code == 403

    role_response = await client.post(
        "/api/v1/admin/roles",
        headers={
            **admin_headers,
            "Idempotency-Key": f"role-create-{suffix}-001",
        },
        json={
            "role_code": f"observer_{suffix}",
            "role_name": f"Observer {suffix}",
            "scope_type": "platform",
            "description": "Integration test observer",
        },
    )
    assert role_response.status_code == 201, role_response.text
    role = role_response.json()["data"]
    repeated_role_response = await client.post(
        "/api/v1/admin/roles",
        headers={
            **admin_headers,
            "Idempotency-Key": f"role-create-{suffix}-001",
        },
        json={
            "role_code": f"observer_{suffix}",
            "role_name": f"Observer {suffix}",
            "scope_type": "platform",
            "description": "Integration test observer",
        },
    )
    assert repeated_role_response.status_code == 201
    assert repeated_role_response.json()["data"]["role_id"] == role["role_id"]

    role_permissions_response = await client.put(
        f"/api/v1/admin/roles/{role['role_id']}/permissions",
        headers={**admin_headers, "If-Match": role_response.headers["etag"]},
        json={
            "permission_codes": ["dashboard:read"],
            "reason": "Integration test least-privilege role",
        },
    )
    assert role_permissions_response.status_code == 200, role_permissions_response.text

    latest_target = await client.get(
        f"/api/v1/admin/users/{target_user_no}",
        headers=admin_headers,
    )
    grant_response = await client.post(
        f"/api/v1/admin/users/{target_user_no}/role-grants",
        headers={
            **admin_headers,
            "If-Match": latest_target.headers["etag"],
            "Idempotency-Key": f"role-grant-{suffix}-0001",
        },
        json={
            "role_id": role["role_id"],
            "scope_type": "platform",
            "scope_id": 0,
            "expires_at": None,
            "reason": "Integration test grant",
        },
    )
    assert grant_response.status_code == 201, grant_response.text
    grant = grant_response.json()["data"]

    revoke_response = await client.post(
        f"/api/v1/admin/users/{target_user_no}/role-grants/{grant['grant_id']}/revocations",
        headers={
            **admin_headers,
            "If-Match": grant_response.headers["etag"],
            "Idempotency-Key": f"role-revoke-{suffix}-001",
        },
        json={"reason": "Integration test revoke"},
    )
    assert revoke_response.status_code == 200, revoke_response.text

    grant_events_response = await client.get(
        f"/api/v1/admin/users/{target_user_no}/role-grant-events",
        headers=admin_headers,
    )
    assert grant_events_response.status_code == 200
    assert [item["event_type"] for item in grant_events_response.json()["data"][:2]] == [
        "revoked",
        "granted",
    ]

    audit_response = await client.get("/api/v1/admin/audit-logs", headers=admin_headers)
    assert audit_response.status_code == 200
    assert any(item["target_id"] == target_user_no for item in audit_response.json()["data"])

    refresh_response = await client.post(
        "/api/v1/admin/auth/token-refresh",
        headers={"X-CSRF-Token": admin_session["csrf_token"]},
    )
    assert refresh_response.status_code == 200, refresh_response.text
    refreshed = refresh_response.json()["data"]
    refreshed_headers = {"Authorization": f"Bearer {refreshed['access_token']}"}

    wrong_portal_refresh = await client.post(
        "/api/v1/merchant/auth/token-refresh",
        headers={"X-CSRF-Token": refreshed["csrf_token"]},
    )
    assert wrong_portal_refresh.status_code == 401

    old_session = await client.get("/api/v1/admin/me", headers=admin_headers)
    assert old_session.status_code == 401

    logout_response = await client.post(
        "/api/v1/admin/auth/logout",
        headers={**refreshed_headers, "X-CSRF-Token": refreshed["csrf_token"]},
    )
    assert logout_response.status_code == 204


async def test_admin_approval_separation_of_duties_and_executor(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(4)
    security = SecurityService(get_settings())
    credentials: list[tuple[str, str, str]] = []
    async for session in mysql_session():
        for label in ("initiator", "approver"):
            username = f"{label}_{suffix}"
            password = f"Admin-{label}-{suffix}-Correct-Horse!"
            provisioning = await provision_platform_super_admin(
                session,
                security,
                username=username,
                password=password,
            )
            credentials.append((username, password, provisioning.totp_secret))
        initiator = await session.scalar(
            select(User).where(User.username_normalized == credentials[0][0])
        )
        assert initiator is not None
        command_payload = {"target_user_id": f"usr_target_{suffix}", "action": "suspend"}
        now = utc_now()
        approval = AdminApprovalRequest(
            approval_request_no=new_prefixed_ulid("aar_"),
            approval_type="user_status_change",
            action_code="integration.user_suspend",
            initiator_user_id=initiator.id,
            scope_type="platform",
            scope_id=0,
            required_permission_code="users:manage",
            target_type="user",
            target_no=f"usr_target_{suffix}",
            command_schema_version=1,
            command_payload_ciphertext=security.encrypt(
                "admin-approval-command",
                json.dumps(command_payload, separators=(",", ":")),
            ),
            command_arguments_hash=canonical_request_hash(command_payload),
            display_snapshot={"action": "Suspend integration target"},
            resource_versions={"user": 1},
            approval_policy_snapshot={
                "policy": "two_person_v1",
                "initiator_assurance_level": "aal2",
                "initiator_authenticated_at": now.isoformat(),
            },
            required_approval_count=1,
            approved_count=0,
            request_status="pending",
            idempotency_key=f"approval-{suffix}",
            expires_at=now + timedelta(minutes=30),
            trace_id=f"trace_{suffix}",
            key_version=1,
        )
        session.add(approval)
        await session.commit()
        approval_no = approval.approval_request_no

    async def admin_login(
        username: str,
        password: str,
        totp_secret: str,
    ) -> dict[str, str]:
        login = await client.post(
            "/api/v1/admin/auth/login",
            json={
                "identifier": username,
                "password": password,
                "client": {"client_type": "web", "device_name": "Approval Test"},
            },
        )
        challenge = login.json()["data"]["challenge_id"]
        mfa = await client.post(
            "/api/v1/admin/auth/mfa-verifications",
            headers={"Idempotency-Key": f"approval-login-{username}-{suffix}"},
            json={
                "challenge_id": challenge,
                "method": "totp",
                "code": pyotp.TOTP(totp_secret).now(),
            },
        )
        assert mfa.status_code == 200, mfa.text
        token = mfa.json()["data"]["session"]["access_token"]
        return {"Authorization": f"Bearer {token}"}

    initiator_headers = await admin_login(*credentials[0])
    approval_get = await client.get(
        f"/api/v1/admin/approval-requests/{approval_no}",
        headers=initiator_headers,
    )
    assert approval_get.status_code == 200
    approval_etag = approval_get.headers["etag"]
    decision_payload = {
        "decision": "approve",
        "reason_code": "verified",
        "reason": "Approval conditions verified",
    }
    self_approval = await client.post(
        f"/api/v1/admin/approval-requests/{approval_no}/decisions",
        headers={
            **initiator_headers,
            "If-Match": approval_etag,
            "Idempotency-Key": f"self-approval-{suffix}-001",
        },
        json=decision_payload,
    )
    assert self_approval.status_code == 403
    assert self_approval.json()["code"] == "APPROVAL_SELF_DECISION_FORBIDDEN"

    approver_headers = await admin_login(*credentials[1])
    approved = await client.post(
        f"/api/v1/admin/approval-requests/{approval_no}/decisions",
        headers={
            **approver_headers,
            "If-Match": approval_etag,
            "Idempotency-Key": f"approval-decision-{suffix}-001",
        },
        json=decision_payload,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == "approved"

    async def fake_domain_handler(
        _payload: dict[str, object],
        _approval: AdminApprovalRequest,
    ) -> ApprovalExecutionResult:
        return ApprovalExecutionResult(resource_type="user", resource_no=f"usr_target_{suffix}")

    async for session in mysql_session():
        item = await session.scalar(
            select(AdminApprovalRequest).where(
                AdminApprovalRequest.approval_request_no == approval_no
            )
        )
        assert item is not None and item.execution_no is not None
        executor = AdminApprovalExecutor(
            session,
            security,
            {"integration.user_suspend": fake_domain_handler},
        )
        await executor.execute(approval_no, item.execution_no)
        await session.refresh(item)
        assert item.request_status == "succeeded"
