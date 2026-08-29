from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from app.api.v1.router import api_router
from app.core.client_ip import TrustedProxyMiddleware
from app.core.config import get_settings
from app.core.exceptions import install_exception_handlers
from app.core.lifespan import application_lifespan
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.observability import MetricsMiddleware, metrics
from app.core.openapi_trace import install_traced_openapi
from app.modules.health.router import router as health_router
from app.modules.realtime.websocket import realtime_websocket


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.debug else None,
        lifespan=application_lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Request-ID",
        ],
        expose_headers=["ETag", "Retry-After", "X-Request-ID"],
        max_age=600,
    )
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(TrustedProxyMiddleware, trusted_proxy_cidrs=settings.trusted_proxy_cidrs)
    install_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    app.add_api_websocket_route("/ws/v1", realtime_websocket, name="realtime-websocket")
    install_traced_openapi(app)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> PlainTextResponse:
        return PlainTextResponse(
            metrics.render_prometheus(), media_type="text/plain; version=0.0.4"
        )

    return app
