from app.core.observability import (
    AiMetric,
    MetricRegistry,
    RequestMetric,
    _HistogramAccumulator,
)
from app.core.telemetry import _safe_key, sanitize_span_attributes


def test_metric_registry_uses_bounded_route_labels() -> None:
    registry = MetricRegistry()
    registry.observe(RequestMetric("GET", "/api/v1/orders/{order_id}", 200, 0.25))
    rendered = registry.render_prometheus()
    assert 'route="/api/v1/orders/{order_id}"' in rendered
    assert 'status="200"' in rendered
    assert "0.250000000" in rendered


def test_ai_token_and_cost_metrics_are_exposed() -> None:
    registry = MetricRegistry()
    registry.observe_ai_run(
        agent_code="store_support",
        status="completed",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.01,
    )
    rendered = registry.render_prometheus()
    assert "ecom_ai_runs_total" in rendered
    assert "ecom_ai_tokens_total" in rendered
    assert "ecom_ai_cost_usd_total" in rendered


def test_telemetry_span_attributes_reject_sensitive_keys() -> None:
    assert _safe_key("ecom.intent")
    assert _safe_key("ecom.scope.type")
    assert not _safe_key("prompt.text")
    assert not _safe_key("arguments.raw")
    assert sanitize_span_attributes(
        {"ecom.intent": "catalog.search", "prompt.text": "do not retain"}
    ) == {"ecom.intent": "catalog.search"}


def test_ai_metrics_cover_component_latency_and_bound_labels() -> None:
    registry = MetricRegistry()
    registry.observe_ai(
        AiMetric(
            component="model",
            operation="answer.generate",
            outcome="completed",
            duration_seconds=0.4,
            ttft_seconds=0.1,
        )
    )
    registry.observe_ai(
        AiMetric(
            component="unknown-component",
            operation="free form is forbidden",
            outcome="made-up",
            duration_seconds=0.2,
        )
    )
    rendered = registry.render_prometheus()
    assert 'component="model",operation="answer.generate",outcome="completed"' in rendered
    assert 'component="agent",operation="unknown",outcome="failed"' in rendered
    assert "ecom_ai_ttft_seconds_bucket" in rendered


def test_histogram_keeps_constant_memory_for_one_million_observations() -> None:
    accumulator = _HistogramAccumulator()

    for index in range(1_000_000):
        accumulator.observe((index % 1000) / 1000)

    assert accumulator.count == 1_000_000
    assert len(accumulator.bucket_counts) == 11
    assert set(vars(accumulator)) == {"bucket_counts", "count", "total"}


def test_metric_series_cardinality_is_bounded() -> None:
    registry = MetricRegistry()

    for index in range(600):
        registry.observe_permission_denial(f"reason_{index}")

    rendered = registry.render_prometheus()
    assert len(registry._permission_denials) == 512
    assert 'family="permission_denials"' in rendered
    assert 'reason="other"' in rendered
