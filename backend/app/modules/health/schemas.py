from typing import Literal

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    build_sha: str


class DependencyStatus(BaseModel):
    status: Literal["up", "down", "degraded", "unknown", "skipped"]
    required: bool = False
    code: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded", "not_ready"]
    dependencies: dict[str, DependencyStatus]
