import json
from pathlib import Path

from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]


def test_identity_openapi_contract_has_stable_operation_ids() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]
    expected = {
        ("/api/v1/auth/registration-config", "get"): "RegistrationConfig_Get",
        ("/api/v1/auth/verification-codes", "post"): "AuthVerificationCode_Create",
        ("/api/v1/auth/registrations", "post"): "Registration_Create",
        ("/api/v1/auth/login", "post"): "Auth_Login",
        ("/api/v1/auth/token-refresh", "post"): "AuthToken_Refresh",
        ("/api/v1/auth/logout", "post"): "Auth_Logout",
        ("/api/v1/users/me", "get"): "UserProfile_Get",
        ("/api/v1/users/me", "patch"): "UserProfile_Patch",
        ("/api/v1/users/me/addresses", "post"): "Address_Create",
        ("/api/v1/users/me/addresses/{address_id}", "patch"): "Address_Patch",
        ("/api/v1/admin/auth/login", "post"): "AdminAuth_Login",
        ("/api/v1/admin/auth/sessions", "get"): "AdminAuthSession_ListMine",
        ("/api/v1/admin/users", "get"): "AdminUser_List",
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


def test_login_is_a_discriminated_union_and_sensitive_endpoints_are_not_gets() -> None:
    schema = create_app().openapi()
    login_schema = schema["paths"]["/api/v1/auth/login"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]

    assert "oneOf" in login_schema
    assert login_schema["discriminator"]["propertyName"] == "auth_method"
    assert "get" not in schema["paths"]["/api/v1/auth/token-refresh"]
    assert "get" not in schema["paths"]["/api/v1/auth/logout"]
