from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession
from app.core.config import get_settings
from app.modules.stores.admin_service import AdminStoreService
from app.modules.stores.operations_service import StoreOperationsService
from app.modules.stores.service import StoreService


def get_store_service(session: DatabaseSession) -> StoreService:
    return StoreService(session, get_settings())


StoreServiceDependency = Annotated[StoreService, Depends(get_store_service)]


def get_admin_store_service(session: DatabaseSession) -> AdminStoreService:
    return AdminStoreService(session, get_settings())


AdminStoreServiceDependency = Annotated[AdminStoreService, Depends(get_admin_store_service)]


def get_store_operations_service(session: DatabaseSession) -> StoreOperationsService:
    return StoreOperationsService(session)


StoreOperationsServiceDependency = Annotated[
    StoreOperationsService, Depends(get_store_operations_service)
]
