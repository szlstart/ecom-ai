from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.core.config import Settings, get_settings
from app.modules.health.schemas import LivenessResponse, ReadinessResponse
from app.modules.health.service import get_readiness

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", operation_id="Health_Live")
async def liveness(settings: Annotated[Settings, Depends(get_settings)]) -> LivenessResponse:
    return LivenessResponse(version=settings.app_version, build_sha=settings.build_sha)


@router.get("/ready", operation_id="Health_Ready")
async def readiness(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReadinessResponse:
    result = await get_readiness(settings)
    if result.status == "not_ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
