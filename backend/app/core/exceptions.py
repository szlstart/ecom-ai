from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.context import request_id_context


class ProblemDetails(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str
    code: str
    request_id: str | None = None
    retryable: bool = False
    errors: list[dict[str, str]] | None = None


class ApplicationError(Exception):
    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str,
        retryable: bool = False,
        errors: list[dict[str, str]] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.retryable = retryable
        self.errors = errors
        super().__init__(detail)


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = []
        for item in exc.errors():
            location = "/" + "/".join(str(part) for part in item.get("loc", ()))
            errors.append(
                {
                    "pointer": location,
                    "code": str(item.get("type", "invalid")),
                    "message": str(item.get("msg", "Invalid value")),
                }
            )
        problem = ProblemDetails(
            title="Request validation failed",
            status=422,
            detail="请求字段校验失败。",
            code="REQUEST_VALIDATION_FAILED",
            request_id=request_id_context.get(),
            errors=errors,
        )
        return JSONResponse(
            status_code=422,
            content=problem.model_dump(mode="json"),
            media_type="application/problem+json",
        )

    @app.exception_handler(ApplicationError)
    async def handle_application_error(_request: Request, exc: ApplicationError) -> JSONResponse:
        problem = ProblemDetails(
            title=exc.title,
            status=exc.status,
            detail=exc.detail,
            code=exc.code,
            request_id=request_id_context.get(),
            retryable=exc.retryable,
            errors=exc.errors,
        )
        return JSONResponse(
            status_code=exc.status,
            content=problem.model_dump(mode="json"),
            media_type="application/problem+json",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
        problem: dict[str, Any] = ProblemDetails(
            title="Internal Server Error",
            status=500,
            detail="The server could not complete the request.",
            code="INTERNAL_SERVER_ERROR",
            request_id=request_id_context.get(),
        ).model_dump(mode="json")
        return JSONResponse(
            status_code=500,
            content=problem,
            media_type="application/problem+json",
        )
