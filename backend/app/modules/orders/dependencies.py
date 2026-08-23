from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession, get_security_service
from app.core.config import get_settings
from app.core.security import SecurityService
from app.modules.orders.service import OrderService


def get_order_service(
    session: DatabaseSession,
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> OrderService:
    return OrderService(session, get_settings(), security)


OrderServiceDependency = Annotated[OrderService, Depends(get_order_service)]
