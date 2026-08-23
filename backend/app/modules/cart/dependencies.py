from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession
from app.modules.cart.service import CartService


def get_cart_service(session: DatabaseSession) -> CartService:
    return CartService(session)


CartServiceDependency = Annotated[CartService, Depends(get_cart_service)]
