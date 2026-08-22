from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from typing import Annotated, Literal

import jwt
from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.security import SecurityService, TokenClaims, utc_now
from app.database.mysql import mysql_session
from app.modules.identity.models import AuthSession, User

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    user: User
    session: AuthSession
    claims: TokenClaims


@lru_cache
def get_security_service() -> SecurityService:
    return SecurityService(get_settings())


DatabaseSession = Annotated[AsyncSession, Depends(mysql_session)]


async def _authenticate(
    expected_audience: Literal["user", "admin"],
    credentials: HTTPAuthorizationCredentials | None,
    session: AsyncSession,
    security: SecurityService,
) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApplicationError(
            status=401,
            code="AUTH_REQUIRED",
            title="Authentication required",
            detail="请先登录。",
        )
    try:
        claims = security.decode_access_token(credentials.credentials, expected_audience)
    except jwt.InvalidAudienceError as exc:
        raise ApplicationError(
            status=403,
            code="AUTH_AUDIENCE_MISMATCH",
            title="Authentication audience mismatch",
            detail="当前登录身份不能访问此入口。",
        ) from exc
    except jwt.PyJWTError as exc:
        raise ApplicationError(
            status=401,
            code="AUTH_TOKEN_INVALID",
            title="Invalid access token",
            detail="登录状态无效或已过期。",
        ) from exc

    result = await session.execute(
        select(User, AuthSession)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(
            User.user_no == claims.subject,
            AuthSession.session_no == claims.session_id,
            AuthSession.audience == expected_audience,
        )
    )
    row = result.one_or_none()
    now = utc_now()
    if row is None:
        raise ApplicationError(
            status=401,
            code="AUTH_SESSION_REVOKED",
            title="Session unavailable",
            detail="登录已失效，请重新登录。",
        )
    user, auth_session = row
    if auth_session.revoked_at is not None or auth_session.expires_at <= now:
        raise ApplicationError(
            status=401,
            code="AUTH_SESSION_REVOKED",
            title="Session unavailable",
            detail="登录已失效，请重新登录。",
        )
    if user.user_status != "active":
        raise ApplicationError(
            status=403,
            code="AUTH_ACCOUNT_UNAVAILABLE",
            title="Account unavailable",
            detail="账号当前不可用。",
        )
    if user.permission_version != claims.permission_version:
        raise ApplicationError(
            status=401,
            code="AUTH_PERMISSION_VERSION_CHANGED",
            title="Permissions changed",
            detail="权限已变化，请重新登录。",
        )
    if auth_session.last_seen_at < now - timedelta(minutes=5):
        auth_session.last_seen_at = now
        await session.commit()
    return AuthContext(user=user, session=auth_session, claims=claims)


async def require_user(
    session: DatabaseSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> AuthContext:
    return await _authenticate("user", credentials, session, security)


async def require_admin(
    session: DatabaseSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> AuthContext:
    return await _authenticate("admin", credentials, session, security)


UserContext = Annotated[AuthContext, Depends(require_user)]
AdminContext = Annotated[AuthContext, Depends(require_admin)]


def require_idempotency_key(
    value: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if value is None or not 16 <= len(value) <= 128 or not value.isascii():
        raise ApplicationError(
            status=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            title="Idempotency key required",
            detail="该操作需要 16 到 128 字符的 Idempotency-Key。",
        )
    return value


IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]
