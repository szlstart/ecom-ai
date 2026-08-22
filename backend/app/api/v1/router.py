from fastapi import APIRouter

from app.modules.catalog.admin_router import router as admin_catalog_router
from app.modules.catalog.router import favorite_router
from app.modules.catalog.router import router as catalog_router
from app.modules.content.router import router as content_router
from app.modules.identity.router import auth_router, user_router
from app.modules.rbac.auth_router import router as admin_auth_router
from app.modules.rbac.router import router as admin_router
from app.modules.stores.router import follow_router
from app.modules.stores.router import router as stores_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(user_router)
api_router.include_router(content_router)
api_router.include_router(catalog_router)
api_router.include_router(favorite_router)
api_router.include_router(stores_router)
api_router.include_router(follow_router)
api_router.include_router(admin_catalog_router)
api_router.include_router(admin_auth_router)
api_router.include_router(admin_router)
