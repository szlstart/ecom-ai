from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession
from app.core.config import get_settings
from app.modules.catalog.service import CatalogService


def get_catalog_service(session: DatabaseSession) -> CatalogService:
    return CatalogService(session, get_settings())


CatalogServiceDependency = Annotated[CatalogService, Depends(get_catalog_service)]
