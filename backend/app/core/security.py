from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
PHONE_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_username(value: str) -> str:
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def normalize_target(target_type: str, value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.strip())
    if target_type == "email":
        normalized = normalized.casefold()
        if not EMAIL_PATTERN.fullmatch(normalized):
            raise ValueError("invalid email address")
        return normalized
    if target_type == "phone":
        normalized = normalized.replace(" ", "").replace("-", "")
        if not PHONE_PATTERN.fullmatch(normalized):
            raise ValueError("invalid E.164 phone number")
        return normalized
    raise ValueError("unsupported target type")


def mask_target(target_type: str, value: str) -> str:
    if target_type == "email":
        local, domain = value.split("@", 1)
        visible = local[:2] if len(local) > 2 else local[:1]
        return f"{visible}***@{domain}"
    return f"{value[:3]}****{value[-4:]}"


def mask_recovery_email(value: str) -> str:
    """Return a recognizable hint without exposing the full registered address."""
    local, domain = value.split("@", 1)
    if len(local) >= 8:
        masked_local = f"{local[:3]}xxx{local[-4:]}"
    elif len(local) >= 5:
        masked_local = f"{local[:2]}xxx{local[-2:]}"
    elif len(local) >= 3:
        masked_local = f"{local[:1]}xxx{local[-1:]}"
    else:
        masked_local = f"{local[:1]}xxx"
    return f"{masked_local}@{domain}"


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    session_id: str
    audience: Literal["user", "admin"]
    permission_version: int
    expires_at: datetime


class SecurityService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._hmac_key = settings.security_hmac_secret.get_secret_value().encode()
        encryption_key = base64.urlsafe_b64decode(
            settings.field_encryption_key.get_secret_value().encode()
        )
        if len(encryption_key) != 32:
            raise ValueError("field encryption key must decode to 32 bytes")
        self._aesgcm = AESGCM(encryption_key)
        self._password_hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
        )
        self._dummy_hash = self.hash_password("dummy-password-that-is-never-a-real-credential")

    def keyed_hash(self, purpose: str, value: str | bytes) -> bytes:
        payload = value.encode() if isinstance(value, str) else value
        return hmac.digest(self._hmac_key, purpose.encode() + b"\x00" + payload, "sha256")

    def encrypt(self, purpose: str, plaintext: str) -> bytes:
        nonce = secrets.token_bytes(12)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode(), purpose.encode())
        return b"\x01" + nonce + ciphertext

    def decrypt(self, purpose: str, value: bytes) -> str:
        if len(value) < 30 or value[0] != 1:
            raise ValueError("unsupported ciphertext envelope")
        return self._aesgcm.decrypt(value[1:13], value[13:], purpose.encode()).decode()

    def hash_password(self, password: str) -> bytes:
        return self._password_hasher.hash(password).encode()

    def verify_password(self, password_hash: bytes | None, password: str) -> bool:
        candidate = password_hash.decode() if password_hash else self._dummy_hash.decode()
        try:
            return self._password_hasher.verify(candidate, password) and password_hash is not None
        except (InvalidHashError, VerifyMismatchError):
            return False

    def password_needs_rehash(self, password_hash: bytes) -> bool:
        return self._password_hasher.check_needs_rehash(password_hash.decode())

    def new_opaque_token(self, byte_length: int = 32) -> str:
        return secrets.token_urlsafe(byte_length)

    def create_access_token(
        self,
        *,
        user_no: str,
        session_no: str,
        audience: Literal["user", "admin"],
        permission_version: int,
    ) -> tuple[str, datetime]:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self.settings.access_token_ttl_seconds)
        payload = {
            "sub": user_no,
            "sid": session_no,
            "aud": audience,
            "iss": "ecom-ai",
            "iat": now,
            "nbf": now,
            "exp": expires_at,
            "pv": permission_version,
            "jti": secrets.token_urlsafe(16),
        }
        token = jwt.encode(
            payload,
            self.settings.access_token_secret.get_secret_value(),
            algorithm="HS256",
        )
        return token, expires_at.replace(tzinfo=None)

    def decode_access_token(self, token: str, audience: Literal["user", "admin"]) -> TokenClaims:
        payload: dict[str, Any] = jwt.decode(
            token,
            self.settings.access_token_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=audience,
            issuer="ecom-ai",
            options={"require": ["exp", "iat", "nbf", "sub", "sid", "aud", "pv"]},
        )
        return TokenClaims(
            subject=str(payload["sub"]),
            session_id=str(payload["sid"]),
            audience=audience,
            permission_version=int(payload["pv"]),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), UTC).replace(tzinfo=None),
        )


def canonical_request_hash(value: object) -> bytes:
    import json

    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).digest()


def etag_for_version(version: int) -> str:
    return f'"v{version}"'
