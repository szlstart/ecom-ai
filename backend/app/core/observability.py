from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog.contextvars
from opentelemetry import metrics as otel_metrics
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind

from app.core.context import request_id_context
from app.core.telemetry import current_trace_fields, traced_operation

AI_COMPONENTS = frozenset({"agent", "model", "mcp", "skill", "rag", "delegation"})
AI_OUTCOMES = frozenset(
    {
        "completed",
        "denied",
        "failed",
        "fallback",
        "partial",
        "timeout",
        "cancelled",
        "user_input_required",
    }
)
DEPENDENCIES = frozenset({"mysql", "postgres", "redis", "payment", "logistics", "model"})
DEPENDENCY_OUTCOMES = frozenset({"success", "error", "timeout", "circuit_open"})
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


@dataclass(frozen=True)
class RequestMetric:
    method: str
    route: str
    status: int
    duration_seconds: float


@dataclass(frozen=True)
class AiMetric:
    component: str
    operation: str
    outcome: str
    duration_seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    ttft_seconds: float | None = None


class MetricRegistry:
    """Thread-safe low-cardinality metrics for API and worker processes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, int], int] = defaultdict(int)
        self._request_duration: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._ai_runs: dict[tuple[str, str, str], int] = defaultdict(int)
        self._ai_duration: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._ai_ttft: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._ai_tokens: dict[tuple[str, str], int] = defaultdict(int)
        self._ai_cost: dict[str, float] = defaultdict(float)
        self._dependencies: dict[tuple[str, str], int] = defaultdict(int)
        self._permission_denials: dict[str, int] = defaultdict(int)
        meter = otel_metrics.get_meter("ecom-ai")
        self._otel_http_count = meter.create_counter("ecom.http.requests")
        self._otel_http_duration = meter.create_histogram("ecom.http.request.duration", unit="s")
        self._otel_ai_count = meter.create_counter("ecom.ai.operations")
        self._otel_ai_duration = meter.create_histogram("ecom.ai.operation.duration", unit="s")
        self._otel_ai_ttft = meter.create_histogram("ecom.ai.ttft", unit="s")
        self._otel_tokens = meter.create_counter("ecom.ai.tokens", unit="{token}")
        self._otel_cost = meter.create_counter("ecom.ai.cost", unit="USD")

    def observe(self, metric: RequestMetric) -> None:
        method = _bounded_code(metric.method, 12).upper()
        route = _route_template(metric.route)
        status = metric.status if 100 <= metric.status <= 599 else 500
        with self._lock:
            self._requests[(method, route, status)] += 1
            self._request_duration[(method, route)].append(max(metric.duration_seconds, 0.0))
        attributes: dict[str, str | int] = {
            "http.request.method": method,
            "http.route": route,
            "http.response.status_code": status,
        }
        self._otel_http_count.add(1, attributes)
        self._otel_http_duration.record(max(metric.duration_seconds, 0.0), attributes)

    def observe_ai(self, metric: AiMetric) -> None:
        component = _member(metric.component, AI_COMPONENTS, "agent")
        operation = _bounded_code(metric.operation, 64)
        outcome = _member(metric.outcome, AI_OUTCOMES, "failed")
        with self._lock:
            self._ai_runs[(component, operation, outcome)] += 1
            self._ai_duration[(component, operation)].append(max(metric.duration_seconds, 0.0))
            if metric.ttft_seconds is not None:
                self._ai_ttft[(component, operation)].append(max(metric.ttft_seconds, 0.0))
            self._ai_tokens[(component, "input")] += max(metric.input_tokens, 0)
            self._ai_tokens[(component, "output")] += max(metric.output_tokens, 0)
            self._ai_cost[component] += max(metric.cost_usd, 0.0)
        ai_attributes = {
            "ecom.component": component,
            "ecom.operation": operation,
            "ecom.outcome": outcome,
        }
        self._otel_ai_count.add(1, ai_attributes)
        self._otel_ai_duration.record(max(metric.duration_seconds, 0.0), ai_attributes)
        if metric.ttft_seconds is not None:
            self._otel_ai_ttft.record(max(metric.ttft_seconds, 0.0), ai_attributes)
        self._otel_tokens.add(
            max(metric.input_tokens, 0), {**ai_attributes, "ecom.direction": "input"}
        )
        self._otel_tokens.add(
            max(metric.output_tokens, 0), {**ai_attributes, "ecom.direction": "output"}
        )
        self._otel_cost.add(max(metric.cost_usd, 0.0), ai_attributes)

    def observe_ai_run(
        self,
        *,
        agent_code: str,
        status: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        self.observe_ai(
            AiMetric(
                component="agent",
                operation=agent_code,
                outcome=status,
                duration_seconds=0.0,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
            )
        )

    def observe_dependency(self, dependency: str, outcome: str) -> None:
        with self._lock:
            self._dependencies[
                (
                    _member(dependency, DEPENDENCIES, "model"),
                    _member(outcome, DEPENDENCY_OUTCOMES, "error"),
                )
            ] += 1

    def observe_permission_denial(self, reason: str) -> None:
        with self._lock:
            self._permission_denials[_bounded_code(reason, 48)] += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "http_requests": sum(self._requests.values()),
                "http_5xx": sum(
                    count for (_, _, status), count in self._requests.items() if status >= 500
                ),
                "ai_operations": sum(self._ai_runs.values()),
                "ai_failures": sum(
                    count
                    for (_, _, outcome), count in self._ai_runs.items()
                    if outcome in {"failed", "timeout"}
                ),
                "input_tokens": sum(
                    value
                    for (_, direction), value in self._ai_tokens.items()
                    if direction == "input"
                ),
                "output_tokens": sum(
                    value
                    for (_, direction), value in self._ai_tokens.items()
                    if direction == "output"
                ),
                "estimated_cost_usd": round(sum(self._ai_cost.values()), 9),
                "permission_denials": sum(self._permission_denials.values()),
            }

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            _counter(
                lines,
                "ecom_http_requests_total",
                "Total HTTP requests.",
                self._requests,
                ("method", "route", "status"),
            )
            _histogram(
                lines,
                "ecom_http_request_duration_seconds",
                "HTTP request duration.",
                self._request_duration,
                ("method", "route"),
            )
            _counter(
                lines,
                "ecom_ai_runs_total",
                "AI component operations by outcome.",
                self._ai_runs,
                ("component", "operation", "outcome"),
            )
            _histogram(
                lines,
                "ecom_ai_operation_duration_seconds",
                "AI component operation duration.",
                self._ai_duration,
                ("component", "operation"),
            )
            _histogram(
                lines,
                "ecom_ai_ttft_seconds",
                "Model first-token latency.",
                self._ai_ttft,
                ("component", "operation"),
            )
            _counter(
                lines,
                "ecom_ai_tokens_total",
                "Model tokens by direction.",
                self._ai_tokens,
                ("component", "direction"),
            )
            _counter(
                lines,
                "ecom_ai_cost_usd_total",
                "Estimated model cost in USD.",
                self._ai_cost,
                ("component",),
            )
            _counter(
                lines,
                "ecom_dependency_calls_total",
                "Dependency calls by outcome.",
                self._dependencies,
                ("dependency", "outcome"),
            )
            _counter(
                lines,
                "ecom_permission_denials_total",
                "Permission gateway denials by bounded reason.",
                self._permission_denials,
                ("reason",),
            )
        return "\n".join(lines) + "\n"


metrics = MetricRegistry()

AsgiCallable = Callable[
    [dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[None]
]


class MetricsMiddleware:
    def __init__(self, app: AsgiCallable) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500
        method = str(scope.get("method", "UNKNOWN"))
        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        request_id = request_id_context.get()
        attributes: dict[str, object] = {"http.request.method": method}
        if request_id:
            attributes["ecom.request_id"] = request_id

        async def capture(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 500))
            await send(message)

        with traced_operation(
            f"HTTP {method}",
            attributes,
            kind=SpanKind.SERVER,
            parent_context=extract(headers),
        ) as span:
            structlog.contextvars.bind_contextvars(**current_trace_fields())
            try:
                await self.app(scope, receive, capture)
            finally:
                route = scope.get("route")
                raw_route = str(getattr(route, "path", scope.get("path", "unknown")))
                template = _route_template(raw_route)
                duration = time.perf_counter() - started
                span.update_name(f"{method} {template}")
                span.set_attribute("http.route", template)
                span.set_attribute("http.response.status_code", status)
                metrics.observe(RequestMetric(method, template, status, duration))


def _counter(
    lines: list[str],
    name: str,
    help_text: str,
    values: Mapping[Any, int | float],
    label_names: tuple[str, ...],
) -> None:
    lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} counter"))
    for raw_labels, value in sorted(values.items(), key=lambda item: str(item[0])):
        labels = raw_labels if isinstance(raw_labels, tuple) else (raw_labels,)
        lines.append(f"{name}{_labels(label_names, labels)} {value}")


def _histogram(
    lines: list[str],
    name: str,
    help_text: str,
    values: Mapping[tuple[Any, ...], list[float]],
    label_names: tuple[str, ...],
) -> None:
    lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} histogram"))
    for raw_labels, observations in sorted(values.items()):
        for boundary in LATENCY_BUCKETS:
            count = sum(value <= boundary for value in observations)
            bucket_labels = _labels(
                (*label_names, "le"), (*raw_labels, str(boundary))
            )
            lines.append(
                f"{name}_bucket{bucket_labels} {count}"
            )
        infinite_labels = _labels((*label_names, "le"), (*raw_labels, "+Inf"))
        lines.append(
            f"{name}_bucket{infinite_labels} {len(observations)}"
        )
        lines.append(f"{name}_sum{_labels(label_names, raw_labels)} {sum(observations):.9f}")
        lines.append(f"{name}_count{_labels(label_names, raw_labels)} {len(observations)}")


def _labels(names: tuple[str, ...], values: tuple[object, ...]) -> str:
    if not names:
        return ""
    body = ",".join(
        f'{name}="{_escape(str(value))}"' for name, value in zip(names, values, strict=True)
    )
    return "{" + body + "}"


def _route_template(value: str) -> str:
    if not value.startswith("/") or len(value) > 160:
        return "unknown"
    segments = value.split("/")
    safe = [
        segment if not _looks_like_resource_id(segment) else "{resource_id}"
        for segment in segments
    ]
    return "/".join(safe)


def _looks_like_resource_id(value: str) -> bool:
    return ("_" in value and len(value) >= 20) or value.isdigit()


def _bounded_code(value: str, maximum: int) -> str:
    if not value or len(value) > maximum:
        return "unknown"
    return value if all(char.isalnum() or char in "._:-" for char in value) else "unknown"


def _member(value: str, allowed: frozenset[str], fallback: str) -> str:
    return value if value in allowed else fallback


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
