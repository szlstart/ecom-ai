from fastapi import APIRouter

from app.modules.content.router import router as content_router
from app.modules.identity.router import auth_router, user_router
from app.modules.rbac.auth_router import router as admin_auth_router
from app.modules.rbac.router import router as admin_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(user_router)
api_router.include_router(content_router)
api_router.include_router(admin_auth_router)
api_router.include_router(admin_router)
