from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.context import request_id_context


class ResponseMeta(BaseModel):
    request_id: str | None = None


class Envelope[T](BaseModel):
    data: T
    meta: ResponseMeta = Field(
        default_factory=lambda: ResponseMeta(request_id=request_id_context.get())
    )


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
