from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

from app.core.config import Settings

AttributeValue = (
    str
    | bool
    | int
    | float
    | Sequence[str]
    | Sequence[bool]
    | Sequence[int]
    | Sequence[float]
)

_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None
logger = logging.getLogger(__name__)

_ALLOWED_ATTRIBUTE_KEYS = frozenset(
    {
        "deployment.environment.name",
        "ecom.action_ref",
        "ecom.agent.code",
        "ecom.agent.run_id",
        "ecom.agent.version",
        "ecom.audit.outcome",
        "ecom.confirmation.outcome",
        "ecom.delegation.id",
        "ecom.event_ref",
        "ecom.handoff.outcome",
        "ecom.intent",
        "ecom.knowledge.version",
        "ecom.mcp.server",
        "ecom.model.provider",
        "ecom.model.profile",
        "ecom.rag.outcome",
        "ecom.request_id",
        "ecom.scope.type",
        "ecom.skill.code",
        "ecom.skill.version",
        "ecom.tool.code",
        "ecom.tool.version",
        "error.type",
        "http.request.method",
        "http.response.status_code",
        "http.route",
        "server.address",
    }
)


def configure_telemetry(settings: Settings) -> None:
    global _meter_provider, _provider
    if not settings.otel_enabled or _provider is not None:
        return
    try:
        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.namespace": "ecom",
                "deployment.environment.name": settings.environment,
            }
        )
        insecure = settings.otel_exporter_otlp_endpoint.startswith("http://")
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=settings.otel_exporter_otlp_endpoint,
                    insecure=insecure,
                )
            )
        )
        trace.set_tracer_provider(provider)
        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(
                endpoint=settings.otel_exporter_otlp_endpoint,
                insecure=insecure,
            ),
            export_interval_millis=15_000,
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        otel_metrics.set_meter_provider(meter_provider)
        HTTPXClientInstrumentor().instrument()
        _provider = provider
        _meter_provider = meter_provider
    except Exception:
        # Telemetry is diagnostic infrastructure. Business traffic must remain available when
        # exporter configuration is temporarily invalid; security audits have a separate sink.
        logger.exception("telemetry_configuration_failed")


def shutdown_telemetry() -> None:
    global _meter_provider, _provider
    if _provider is not None:
        _provider.shutdown()
        HTTPXClientInstrumentor().uninstrument()
    _provider = None
    if _meter_provider is not None:
        _meter_provider.shutdown()
    _meter_provider = None


@contextmanager
def traced_operation(
    name: str,
    attributes: Mapping[str, object],
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    parent_context: Context | None = None,
) -> Iterator[trace.Span]:
    safe_attributes = sanitize_span_attributes(attributes)
    with trace.get_tracer("ecom-ai").start_as_current_span(
        name, context=parent_context, kind=kind, attributes=safe_attributes
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            span.set_attribute("error.type", type(exc).__name__[:128])
            raise


def current_trace_fields() -> dict[str, str]:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {"trace_id": f"{context.trace_id:032x}", "span_id": f"{context.span_id:016x}"}


def sanitize_span_attributes(attributes: Mapping[str, object]) -> dict[str, AttributeValue]:
    result: dict[str, AttributeValue] = {}
    for key, raw in attributes.items():
        if not _safe_key(key) or raw is None:
            continue
        value = _safe_attribute_value(raw)
        if value is not None:
            result[key] = value
    return result


def _safe_attribute_value(value: object) -> AttributeValue | None:
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        safe = [item[:128] for item in value[:16] if isinstance(item, str)]
        return safe or None
    return None


def _safe_key(key: str) -> bool:
    return key in _ALLOWED_ATTRIBUTE_KEYS
