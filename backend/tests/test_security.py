import jwt
import pytest
from cryptography.exceptions import InvalidTag

from app.core.config import Settings
from app.core.security import SecurityService, canonical_request_hash, normalize_target


@pytest.fixture(scope="module")
def security() -> SecurityService:
    return SecurityService(Settings())


def test_password_hashing_never_accepts_missing_or_wrong_hash(
    security: SecurityService,
) -> None:
    password_hash = security.hash_password("A-correct-horse-battery-staple-2026")

    assert security.verify_password(password_hash, "A-correct-horse-battery-staple-2026")
    assert not security.verify_password(password_hash, "wrong-password")
    assert not security.verify_password(None, "A-correct-horse-battery-staple-2026")


def test_encryption_is_randomized_and_purpose_bound(security: SecurityService) -> None:
    first = security.encrypt("address-phone", "+8613800000000")
    second = security.encrypt("address-phone", "+8613800000000")

    assert first != second
    assert security.decrypt("address-phone", first) == "+8613800000000"
    with pytest.raises(InvalidTag):
        security.decrypt("session-ip", first)


def test_access_token_is_audience_bound(security: SecurityService) -> None:
    token, _ = security.create_access_token(
        user_no="usr_test",
        session_no="ses_test",
        audience="user",
        permission_version=3,
    )

    claims = security.decode_access_token(token, "user")
    assert claims.subject == "usr_test"
    assert claims.session_id == "ses_test"
    assert claims.permission_version == 3
    with pytest.raises(jwt.InvalidAudienceError):
        security.decode_access_token(token, "admin")


def test_normalization_and_request_hash_are_canonical() -> None:
    assert normalize_target("email", " User@Example.COM ") == "user@example.com"
    assert normalize_target("phone", "+86 138-0000-0000") == "+8613800000000"
    assert canonical_request_hash({"b": 2, "a": 1}) == canonical_request_hash({"a": 1, "b": 2})
