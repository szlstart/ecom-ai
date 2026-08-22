from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.core.exceptions import ApplicationError
from app.core.security import utc_now


@dataclass(frozen=True)
class CursorPosition:
    values: tuple[str, ...]
    direction: str


class CursorCodec:
    """Signed opaque cursor bound to one filter/sort contract."""

    def __init__(self, secret: str, *, ttl: timedelta = timedelta(hours=1)) -> None:
        self._secret = secret.encode()
        self._ttl = ttl

    def encode(
        self,
        *,
        filter_key: str,
        values: tuple[str, ...],
        direction: str = "next",
    ) -> str:
        payload = {
            "v": 1,
            "f": self.filter_digest(filter_key),
            "p": list(values),
            "d": direction,
            "exp": int((utc_now() + self._ttl).timestamp()),
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(self._secret, raw, hashlib.sha256).digest()
        return f"{_b64(raw)}.{_b64(signature)}"

    def decode(self, token: str | None, *, filter_key: str) -> CursorPosition | None:
        if token is None:
            return None
        try:
            payload_token, signature_token = token.split(".", maxsplit=1)
            raw = _unb64(payload_token)
            provided_signature = _unb64(signature_token)
            expected_signature = hmac.new(self._secret, raw, hashlib.sha256).digest()
            if not hmac.compare_digest(provided_signature, expected_signature):
                raise ValueError("signature mismatch")
            payload: dict[str, Any] = json.loads(raw)
            if payload.get("v") != 1 or payload.get("f") != self.filter_digest(filter_key):
                raise ValueError("cursor contract mismatch")
            expires_at = int(payload["exp"])
            if expires_at <= int(utc_now().timestamp()):
                raise ApplicationError(
                    status=410,
                    code="PAGINATION_CURSOR_EXPIRED",
                    title="Pagination cursor expired",
                    detail="分页位置已过期，请从第一批重新加载。",
                )
            raw_values = payload["p"]
            direction = payload["d"]
            if (
                not isinstance(raw_values, list)
                or not all(isinstance(value, str) for value in raw_values)
                or direction not in {"next", "previous"}
            ):
                raise ValueError("invalid cursor position")
            return CursorPosition(values=tuple(raw_values), direction=direction)
        except ApplicationError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ApplicationError(
                status=400,
                code="PAGINATION_CURSOR_INVALID",
                title="Invalid pagination cursor",
                detail="分页位置无效，请重新加载列表。",
            ) from exc

    @staticmethod
    def filter_digest(filter_key: str) -> str:
        return hashlib.sha256(filter_key.encode()).hexdigest()[:24]


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(value + padding)
    if _b64(decoded) != value:
        raise ValueError("non-canonical base64url value")
    return decoded
