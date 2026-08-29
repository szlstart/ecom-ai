from __future__ import annotations

from datetime import timedelta

import pytest

from app.api.dependencies import AuthContext
from app.core.config import Settings
from app.core.security import TokenClaims, utc_now
from app.modules.identity.models import AuthSession, User
from app.modules.realtime.tickets import RealtimeTicketService


class _TicketRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(
        self, key: str, value: str, *, ex: int, nx: bool
    ) -> bool:  # pragma: no cover - signature documents Redis behavior
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def getdel(self, key: str) -> str | None:
        return self.values.pop(key, None)


@pytest.mark.asyncio
async def test_realtime_ticket_is_opaque_hashed_and_one_time() -> None:
    now = utc_now()
    user = User(
        id=7,
        user_no="usr_01KTESTREALTIME000000000000",
        username="realtime-user",
        username_normalized="realtime-user",
        nickname="Realtime User",
        user_status="active",
        locale="zh-CN",
        timezone="Asia/Shanghai",
        permission_version=3,
        registered_at=now,
    )
    auth_session = AuthSession(
        id=9,
        session_no="ses_01KTESTREALTIME000000000000",
        user_id=7,
        refresh_token_hash=b"x" * 32,
        token_family_no="tfa_01KTESTREALTIME000000000000",
        device_no="dev_01KTESTREALTIME000000000000",
        device_name="test",
        client_type="web",
        audience="user",
        csrf_token_hash=b"y" * 32,
        authenticated_at=now,
        authentication_methods=["password"],
        assurance_level="aal1",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        last_seen_at=now,
    )
    context = AuthContext(
        user=user,
        session=auth_session,
        claims=TokenClaims(
            subject=user.user_no,
            session_id=auth_session.session_no,
            audience="user",
            permission_version=user.permission_version,
            expires_at=now + timedelta(minutes=15),
        ),
    )
    redis = _TicketRedis()
    settings = Settings(environment="test", realtime_ticket_ttl_seconds=30)
    service = RealtimeTicketService(redis, settings)  # type: ignore[arg-type]

    raw, ttl = await service.issue(context, "user")

    assert raw.startswith("rt_")
    assert ttl == 30
    assert all(raw not in key for key in redis.values)
    consumed = await service.consume(raw)
    assert consumed is not None
    assert consumed.audience == "user"
    assert consumed.user_no == user.user_no
    assert await service.consume(raw) is None
