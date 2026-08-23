from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession, get_security_service
from app.core.security import SecurityService
from app.modules.payments.service import PaymentService


def get_payment_service(
    session: DatabaseSession,
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> PaymentService:
    return PaymentService(session, security)


PaymentServiceDependency = Annotated[PaymentService, Depends(get_payment_service)]
