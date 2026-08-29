from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession, get_security_service
from app.core.config import get_settings
from app.core.security import SecurityService
from app.modules.after_sale.service import AfterSaleService


def get_after_sale_service(
    session: DatabaseSession,
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> AfterSaleService:
    return AfterSaleService(session, get_settings(), security)


AfterSaleServiceDependency = Annotated[AfterSaleService, Depends(get_after_sale_service)]
