from app.modules.rbac.dependencies import requires_mfa_for_session, requires_recent_auth_for_session


def test_merchant_store_operations_do_not_require_password_step_up() -> None:
    assert requires_recent_auth_for_session(True, "merchant") is False


def test_platform_admin_does_not_require_time_based_password_step_up() -> None:
    assert requires_recent_auth_for_session(True, "admin_password") is False
    assert requires_recent_auth_for_session(False, "admin_password") is False


def test_merchant_store_operations_do_not_require_unavailable_mfa_step_up() -> None:
    assert requires_mfa_for_session(True, "merchant") is False


def test_platform_admin_does_not_require_mfa_step_up() -> None:
    assert requires_mfa_for_session(True, "admin_password") is False
    assert requires_mfa_for_session(False, "admin_password") is False
