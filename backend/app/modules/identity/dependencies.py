from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession, get_security_service
from app.core.config import get_settings
from app.core.security import SecurityService
from app.database.redis import get_redis
from app.modules.identity.service import IdentityService


def get_identity_service(
    session: DatabaseSession,
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> IdentityService:
    return IdentityService(session, get_redis(), security, get_settings())


IdentityServiceDependency = Annotated[IdentityService, Depends(get_identity_service)]
