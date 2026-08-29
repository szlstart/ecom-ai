from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials

from app.api.dependencies import (
    AuthContext,
    DatabaseSession,
    _authenticate,
    bearer,
    get_security_service,
)
from app.core.exceptions import ApplicationError
from app.core.security import SecurityService


@dataclass(frozen=True)
class FileActor:
    context: AuthContext
    audience: Literal["user", "admin"]


async def optional_file_actor(
    session: DatabaseSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> FileActor | None:
    if credentials is None:
        return None
    token = credentials.credentials
    try:
        unverified = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise _invalid_token() from exc
    audience = unverified.get("aud")
    if audience not in {"user", "admin"}:
        raise _invalid_token()
    context = await _authenticate(audience, credentials, session, security)
    return FileActor(context=context, audience=audience)


async def require_file_actor(
    actor: Annotated[FileActor | None, Depends(optional_file_actor)],
) -> FileActor:
    if actor is None:
        raise ApplicationError(
            status=401,
            code="AUTH_REQUIRED",
            title="Authentication required",
            detail="请先登录。",
        )
    return actor


FileActorDependency = Annotated[FileActor, Depends(require_file_actor)]
OptionalFileActorDependency = Annotated[FileActor | None, Depends(optional_file_actor)]


def _invalid_token() -> ApplicationError:
    return ApplicationError(
        status=401,
        code="AUTH_TOKEN_INVALID",
        title="Invalid access token",
        detail="登录状态无效或已过期。",
    )
