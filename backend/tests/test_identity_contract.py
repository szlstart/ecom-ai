from app.main import create_app


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
    }

    operation_ids = []
    for (path, method), operation_id in expected.items():
        operation = paths[path][method]
        assert operation["operationId"] == operation_id
        operation_ids.append(operation["operationId"])
    assert len(operation_ids) == len(set(operation_ids))


def test_login_is_a_discriminated_union_and_sensitive_endpoints_are_not_gets() -> None:
    schema = create_app().openapi()
    login_schema = schema["paths"]["/api/v1/auth/login"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]

    assert "oneOf" in login_schema
    assert login_schema["discriminator"]["propertyName"] == "auth_method"
    assert "get" not in schema["paths"]["/api/v1/auth/token-refresh"]
    assert "get" not in schema["paths"]["/api/v1/auth/logout"]
