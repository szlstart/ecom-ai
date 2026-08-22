from fastapi import APIRouter

from app.modules.content.router import router as content_router
from app.modules.identity.router import auth_router, user_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(user_router)
api_router.include_router(content_router)
