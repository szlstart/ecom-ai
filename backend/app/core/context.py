from contextvars import ContextVar, Token

import structlog.contextvars

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


def bind_request_id(request_id: str) -> Token[str | None]:
    token = request_id_context.set(request_id)
    structlog.contextvars.bind_contextvars(request_id=request_id)
    return token


def reset_request_id(token: Token[str | None]) -> None:
    request_id_context.reset(token)
    structlog.contextvars.unbind_contextvars("request_id", "trace_id", "span_id")
