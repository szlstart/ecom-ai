from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession, get_security_service
from app.core.config import get_settings
from app.core.security import SecurityService
from app.database.redis import get_redis
from app.modules.rbac.auth_service import AdminAuthService


def get_admin_auth_service(
    session: DatabaseSession,
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> AdminAuthService:
    return AdminAuthService(session, get_redis(), security, get_settings())


AdminAuthServiceDependency = Annotated[AdminAuthService, Depends(get_admin_auth_service)]
