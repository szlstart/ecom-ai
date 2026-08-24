from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import bind_request_id, reset_request_id
from app.core.id_generator import new_request_id
from app.core.telemetry import current_trace_fields


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        incoming_request_id = request.headers.get("x-request-id", "")
        request_id = (
            incoming_request_id if _is_safe_request_id(incoming_request_id) else new_request_id()
        )
        token = bind_request_id(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            import structlog.contextvars

            structlog.contextvars.bind_contextvars(**current_trace_fields())
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_request_id(token)


def _is_safe_request_id(value: str) -> bool:
    return value.startswith("req_") and len(value) == 30 and value[4:].isalnum()
