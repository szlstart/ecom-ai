from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession, get_security_service
from app.core.security import SecurityService
from app.modules.finance.account_deletion import AccountDeletionService
from app.modules.finance.service import FinanceService


def get_finance_service(
    session: DatabaseSession,
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> FinanceService:
    return FinanceService(session, security)


FinanceServiceDependency = Annotated[FinanceService, Depends(get_finance_service)]


def get_account_deletion_service(mysql: DatabaseSession) -> AccountDeletionService:
    return AccountDeletionService(mysql)


AccountDeletionServiceDependency = Annotated[
    AccountDeletionService, Depends(get_account_deletion_service)
]
