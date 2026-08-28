import json
from pathlib import Path

import pytest

from app.core.exceptions import ApplicationError
from app.core.security import mask_recovery_email
from app.main import create_app
from app.modules.identity.schemas import AddressPatch, AddressWrite, RegistrationRequest
from app.modules.identity.service import IdentityService

ROOT = Path(__file__).resolve().parents[2]


def test_identity_openapi_contract_has_stable_operation_ids() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]
    expected = {
        ("/api/v1/auth/registration-config", "get"): "RegistrationConfig_Get",
        ("/api/v1/auth/password-reset-hints", "post"): "PasswordResetHint_Get",
        ("/api/v1/auth/registrations", "post"): "Registration_Create",
        ("/api/v1/auth/login", "post"): "Auth_Login",
        ("/api/v1/auth/session-resume", "post"): "AuthSession_Resume",
        ("/api/v1/auth/token-refresh", "post"): "AuthToken_Refresh",
        ("/api/v1/auth/logout", "post"): "Auth_Logout",
        ("/api/v1/users/me", "get"): "UserProfile_Get",
        ("/api/v1/users/me", "patch"): "UserProfile_Patch",
        ("/api/v1/users/me/addresses", "post"): "Address_Create",
        ("/api/v1/users/me/addresses/{address_id}", "patch"): "Address_Patch",
        ("/api/v1/admin/auth/password-login", "post"): "AdminAuth_PasswordLogin",
        ("/api/v1/admin/auth/session-resume", "post"): "AdminAuthSession_Resume",
        (
            "/api/v1/admin/auth/password-reauthentications",
            "post",
        ): "AdminAuth_PasswordReauthenticate",
        ("/api/v1/merchant/auth/login", "post"): "MerchantAuth_Login",
        ("/api/v1/merchant/auth/registrations", "post"): "MerchantAuth_Register",
        (
            "/api/v1/merchant/auth/session-resume",
            "post",
        ): "MerchantAuthSession_Resume",
        (
            "/api/v1/merchant/auth/token-refresh",
            "post",
        ): "MerchantAuthToken_Refresh",
        ("/api/v1/merchant/auth/logout", "post"): "MerchantAuth_Logout",
        (
            "/api/v1/merchant/auth/reauthentications",
            "post",
        ): "MerchantAuth_Reauthenticate",
        ("/api/v1/admin/auth/sessions", "get"): "AdminAuthSession_ListMine",
        ("/api/v1/admin/users", "get"): "AdminUser_List",
        ("/api/v1/admin/users", "post"): "AdminUser_Create",
        ("/api/v1/admin/users/{user_id}", "patch"): "AdminUser_Update",
        ("/api/v1/admin/users/{user_id}", "delete"): "AdminUser_Delete",
        (
            "/api/v1/admin/users/{user_id}/password-replacements",
            "post",
        ): "AdminUserPassword_Replace",
        (
            "/api/v1/admin/users/{user_id}/wallet-adjustments",
            "post",
        ): "AdminUserWallet_Adjust",
        ("/api/v1/admin/roles", "post"): "AdminRole_Create",
        ("/api/v1/admin/approval-requests", "get"): "AdminApproval_List",
        (
            "/api/v1/admin/sensitive-field-access-grants/{grant_id}/revocations",
            "post",
        ): "AdminSensitiveGrant_Revoke",
    }

    operation_ids = []
    for (path, method), operation_id in expected.items():
        operation = paths[path][method]
        assert operation["operationId"] == operation_id
        operation_ids.append(operation["operationId"])
    assert len(operation_ids) == len(set(operation_ids))


def test_all_openapi_operation_ids_are_globally_unique() -> None:
    schema = create_app().openapi()
    operation_ids = [
        operation["operationId"]
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert len(operation_ids) == len(set(operation_ids))


def test_committed_openapi_artifact_matches_application() -> None:
    committed = json.loads((ROOT / "docs" / "openapi-v1.json").read_text(encoding="utf-8"))
    assert committed == create_app().openapi()


def test_login_is_password_only_and_sensitive_endpoints_are_not_gets() -> None:
    schema = create_app().openapi()
    login_schema = schema["paths"]["/api/v1/auth/login"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]

    assert login_schema == {"$ref": "#/components/schemas/PasswordLoginRequest"}
    assert "get" not in schema["paths"]["/api/v1/auth/token-refresh"]
    assert "get" not in schema["paths"]["/api/v1/auth/logout"]


def test_registration_uses_arithmetic_captcha_and_recovery_email() -> None:
    request = RegistrationRequest.model_validate(
        {
            "username": "test_user",
            "email": "test_user@example.com",
            "captcha_id": "captcha-identifier-0001",
            "captcha_answer": "12",
            "password": "x" * 1024,
            "config_version": "regcfg_test",
            "agreement_acceptances": [
                {"document_type": "terms_of_service", "document_version": "v1"},
                {"document_type": "privacy_policy", "document_version": "v1"},
            ],
        }
    )
    assert request.captcha_answer == "12"
    assert request.email == "test_user@example.com"
    assert len(request.password) == 1024


def test_recovery_email_hint_masks_the_middle() -> None:
    assert mask_recovery_email("1390003212@qq.com") == "139***3212@qq.com"
    assert mask_recovery_email("short@example.com") == "sh***rt@example.com"


def test_address_recipient_name_accepts_one_character() -> None:
    address = AddressWrite.model_validate(
        {
            "recipient_name": "张",
            "phone": "+8613800000000",
            "province_code": "440000",
            "city_code": "440300",
            "district_code": "440305",
            "address": "测试路 1 号",
        }
    )
    update = AddressPatch.model_validate({"recipient_name": "A"})

    assert address.recipient_name == "张"
    assert update.recipient_name == "A"


def test_user_password_policy_only_rejects_empty_or_whitespace() -> None:
    service = IdentityService.__new__(IdentityService)
    service._validate_password("a")
    service._validate_password("长" * 1024)
    with pytest.raises(ApplicationError) as empty_error:
        service._validate_password("")
    assert empty_error.value.code == "PASSWORD_POLICY_FAILED"
    with pytest.raises(ApplicationError) as exc_info:
        service._validate_password("contains space")
    assert exc_info.value.code == "PASSWORD_WHITESPACE_FORBIDDEN"
