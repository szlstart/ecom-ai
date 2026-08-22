from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession
from app.core.config import get_settings
from app.modules.stores.service import StoreService


def get_store_service(session: DatabaseSession) -> StoreService:
    return StoreService(session, get_settings())


StoreServiceDependency = Annotated[StoreService, Depends(get_store_service)]
