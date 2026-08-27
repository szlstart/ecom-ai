from app.modules.rbac.dependencies import requires_recent_auth_for_session


def test_merchant_store_operations_do_not_require_password_step_up() -> None:
    assert requires_recent_auth_for_session(True, "merchant") is False


def test_platform_admin_recent_auth_policy_is_unchanged() -> None:
    assert requires_recent_auth_for_session(True, "admin_password") is True
    assert requires_recent_auth_for_session(False, "admin_password") is False
