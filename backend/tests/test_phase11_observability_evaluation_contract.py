from pathlib import Path

from fastapi.routing import APIRoute
from sqlalchemy import String
from sqlalchemy.dialects.mysql import BINARY

from app.main import create_app
from app.modules.evaluation.models import AiEvaluationRun
from app.modules.evaluation.service import DATASET_SHA256


def test_phase11_admin_operations_are_registered() -> None:
    operations = {
        route.operation_id
        for route in create_app().routes
        if isinstance(route, APIRoute) and route.operation_id
    }
    assert {
        "AdminAiEvaluation_List",
        "AdminAiEvaluation_Run",
        "AdminObservability_Query",
    } <= operations


def test_evaluation_model_keeps_only_versioned_evidence() -> None:
    columns = AiEvaluationRun.__table__.columns
    run_no_type = columns["evaluation_run_no"].type
    dataset_hash_type = columns["dataset_hash"].type
    assert isinstance(run_no_type, String)
    assert isinstance(dataset_hash_type, BINARY)
    assert run_no_type.length == 40
    assert dataset_hash_type.length == 32
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
