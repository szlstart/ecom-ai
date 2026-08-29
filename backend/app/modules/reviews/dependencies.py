from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession
from app.core.config import get_settings
from app.modules.reviews.service import ReviewService


def get_review_service(session: DatabaseSession) -> ReviewService:
    return ReviewService(session, get_settings())


ReviewServiceDependency = Annotated[ReviewService, Depends(get_review_service)]
