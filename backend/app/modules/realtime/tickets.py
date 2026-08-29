from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.api.dependencies import AuthContext
from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.security import utc_now


@dataclass(frozen=True)
class RealtimeTicket:
    audience: Literal["user", "admin"]
    user_no: str
    session_no: str
    permission_version: int
    access_expires_at: str


class RealtimeTicketService:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    async def issue(
        self, context: AuthContext, audience: Literal["user", "admin"]
    ) -> tuple[str, int]:
        raw = f"rt_{secrets.token_urlsafe(32)}"
        payload = RealtimeTicket(
            audience=audience,
            user_no=context.user.user_no,
            session_no=context.session.session_no,
            permission_version=context.user.permission_version,
            access_expires_at=context.claims.expires_at.isoformat(),
        )
        try:
            created = await self.redis.set(
                self._key(raw),
                json.dumps(asdict(payload), separators=(",", ":")),
                ex=self.settings.realtime_ticket_ttl_seconds,
                nx=True,
            )
        except RedisError as exc:
            raise _unavailable() from exc
        if not created:
            raise _unavailable()
        return raw, self.settings.realtime_ticket_ttl_seconds

    async def consume(self, raw: str) -> RealtimeTicket | None:
        if not raw.startswith("rt_") or len(raw) > 96:
            return None
        try:
            encoded = await self.redis.getdel(self._key(raw))
        except RedisError as exc:
            raise _unavailable() from exc
        if not isinstance(encoded, str):
            return None
        try:
            data = cast(dict[str, object], json.loads(encoded))
            audience = data["audience"]
            user_no = data["user_no"]
            session_no = data["session_no"]
            permission_version = data["permission_version"]
            access_expires_at = data["access_expires_at"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            audience not in {"user", "admin"}
            or not isinstance(user_no, str)
            or not isinstance(session_no, str)
            or not isinstance(permission_version, int)
            or not isinstance(access_expires_at, str)
        ):
            return None
        try:
            expires_at = datetime.fromisoformat(access_expires_at)
        except ValueError:
            return None
        if expires_at <= utc_now():
            return None
        return RealtimeTicket(
            audience=cast(Literal["user", "admin"], audience),
            user_no=user_no,
            session_no=session_no,
            permission_version=permission_version,
            access_expires_at=access_expires_at,
        )

    def _key(self, raw: str) -> str:
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return f"ecom:{self.settings.environment}:realtime:ticket:{digest}:v1"


def _unavailable() -> ApplicationError:
    return ApplicationError(
        status=503,
        code="REALTIME_TEMPORARILY_UNAVAILABLE",
        title="Realtime unavailable",
        detail="实时连接暂不可用，消息仍可通过页面刷新获取。",
    )
