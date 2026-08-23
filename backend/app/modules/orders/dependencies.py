from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession
from app.modules.orders.service import OrderService


def get_order_service(session: DatabaseSession) -> OrderService:
    return OrderService(session)


OrderServiceDependency = Annotated[OrderService, Depends(get_order_service)]
