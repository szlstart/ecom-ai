from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession
from app.core.config import get_settings
from app.modules.catalog.admin_service import AdminCatalogService
from app.modules.catalog.product_admin_service import ProductAdminService
from app.modules.catalog.service import CatalogService


def get_catalog_service(session: DatabaseSession) -> CatalogService:
    return CatalogService(session, get_settings())


CatalogServiceDependency = Annotated[CatalogService, Depends(get_catalog_service)]


def get_admin_catalog_service(session: DatabaseSession) -> AdminCatalogService:
    return AdminCatalogService(session)


AdminCatalogServiceDependency = Annotated[AdminCatalogService, Depends(get_admin_catalog_service)]


def get_product_admin_service(session: DatabaseSession) -> ProductAdminService:
    return ProductAdminService(session, get_settings())


ProductAdminServiceDependency = Annotated[ProductAdminService, Depends(get_product_admin_service)]
