from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession, get_security_service
from app.core.config import get_settings
from app.core.security import SecurityService
from app.modules.logistics.service import LogisticsService


def get_logistics_service(
    session: DatabaseSession,
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> LogisticsService:
    return LogisticsService(
        session,
        security,
        get_settings().security_hmac_secret.get_secret_value(),
    )


LogisticsServiceDependency = Annotated[LogisticsService, Depends(get_logistics_service)]
