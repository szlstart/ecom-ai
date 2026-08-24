from pathlib import Path

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.database.base import MySQLBase
from app.modules.agent_runtime import models as agent_models  # noqa: F401


def test_delegation_ledger_has_idempotency_depth_and_status_guards() -> None:
    table = MySQLBase.metadata.tables["ai_agent_delegations"]
    uniques = {
        item.name for item in table.constraints if isinstance(item, UniqueConstraint)
    }
    checks = {item.name for item in table.constraints if isinstance(item, CheckConstraint)}
    assert {
        "uk_ai_agent_delegations_no",
        "uk_ai_agent_delegations_fingerprint",
        "uk_ai_agent_delegations_run_subtask_version",
    } <= uniques
    assert {
        "ck_ai_agent_delegations_agent_delegation_depth",
        "ck_ai_agent_delegations_agent_delegation_status",
    } <= checks
    assert {
        "scope_snapshot",
        "resource_refs",
        "dependency_nos",
        "allowed_tools_snapshot",
        "budget_snapshot",
        "result_snapshot",
        "trace_id",
        "span_id",
    } <= {column.name for column in table.columns}


def test_phase_ten_migration_is_chained_after_phase_nine() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations/mysql/versions/q61f4a5b6c7d_add_agent_delegation_ledger.py"
    )
    text = path.read_text(encoding="utf-8")
    assert 'down_revision = "p50e3f4a5b6c"' in text
    assert '"ai_agent_delegations"' in text
    assert 'sa.CheckConstraint("depth = 1"' in text
