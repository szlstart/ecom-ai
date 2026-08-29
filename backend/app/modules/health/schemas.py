from typing import Literal

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    build_sha: str


class DependencyStatus(BaseModel):
    status: Literal["up", "down", "skipped"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    dependencies: dict[str, DependencyStatus]
