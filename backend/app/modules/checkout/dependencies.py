from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession
from app.modules.checkout.service import CheckoutService


def get_checkout_service(session: DatabaseSession) -> CheckoutService:
    return CheckoutService(session)


CheckoutServiceDependency = Annotated[CheckoutService, Depends(get_checkout_service)]
