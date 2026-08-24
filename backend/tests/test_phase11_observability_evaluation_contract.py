from pathlib import Path

from app.main import create_app
from app.modules.evaluation.models import AiEvaluationRun
from app.modules.evaluation.service import DATASET_SHA256


def test_phase11_admin_operations_are_registered() -> None:
    operations = {
        route.operation_id
        for route in create_app().routes
        if getattr(route, "operation_id", None)
    }
    assert {
        "AdminAiEvaluation_List",
        "AdminAiEvaluation_Run",
        "AdminObservability_Query",
    } <= operations


def test_evaluation_model_keeps_only_versioned_evidence() -> None:
    columns = AiEvaluationRun.__table__.columns
    assert columns["evaluation_run_no"].type.length == 40
    assert columns["dataset_hash"].type.length == 32
    assert "prompt" not in columns
    assert "response" not in columns
    assert len(bytes.fromhex(DATASET_SHA256)) == 32


def test_evaluation_migration_is_reversible() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations/mysql/versions/r72a5b6c7d8e_add_ai_evaluation_runs.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "q61f4a5b6c7d"' in migration
    assert '"ai_evaluation_runs",' in migration
    assert 'op.drop_table("ai_evaluation_runs")' in migration


def test_collector_uses_native_loki_otlp_and_memory_guard() -> None:
    root = Path(__file__).resolve().parents[2]
    collector = (root / "observability/otel-collector.yaml").read_text(encoding="utf-8")
    assert "otlphttp/loki:" in collector
    assert "endpoint: http://loki:3100/otlp" in collector
    assert "memory_limiter" in collector
    assert "exporters: [loki]" not in collector
