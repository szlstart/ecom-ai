from __future__ import annotations

import pytest

from app.bootstrap.development_cleanup import (
    assert_safe_development_database,
    eligible_test_admin_username,
)
from app.core.config import Settings


def test_cleanup_candidate_is_restricted_to_legacy_test_admin_names() -> None:
    assert eligible_test_admin_username("admin_c193cb6a", keep_username="admin")
    assert eligible_test_admin_username("approver_fe92f4af", keep_username="admin")
    assert eligible_test_admin_username("initiator_fe92f4af", keep_username="admin")
    assert not eligible_test_admin_username("admin", keep_username="admin")
    assert not eligible_test_admin_username("alice", keep_username="admin")
    assert not eligible_test_admin_username("admin_operations", keep_username="admin")


def test_cleanup_rejects_non_development_or_unexpected_database() -> None:
    assert_safe_development_database(
        Settings(
            environment="development",
            mysql_dsn="mysql+asyncmy://user:pass@localhost/ecom_ai",
            access_token_secret="a" * 40,
            security_hmac_secret="b" * 40,
        )
    )
    with pytest.raises(RuntimeError):
        assert_safe_development_database(
            Settings(
                environment="testing",
                mysql_dsn="mysql+asyncmy://user:pass@localhost/ecom_ai_test",
                access_token_secret="a" * 40,
                security_hmac_secret="b" * 40,
            )
        )
    with pytest.raises(RuntimeError):
        assert_safe_development_database(
            Settings(
                environment="development",
                mysql_dsn="mysql+asyncmy://user:pass@localhost/another_database",
                access_token_secret="a" * 40,
                security_hmac_secret="b" * 40,
            )
        )
