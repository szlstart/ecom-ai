from pathlib import Path


def test_postgres_agent_and_knowledge_migrations_have_acl_and_runtime_keys() -> None:
    directory = Path(__file__).resolve().parents[1] / "migrations/postgres/versions"
    runtime = (directory / "20260824_0002_agent_knowledge_runtime.py").read_text(
        encoding="utf-8"
    )
    knowledge = (directory / "20260824_0003_knowledge_memory.py").read_text(encoding="utf-8")
    for required in (
        "agent_runtime.run_state_refs",
        "conversation_no",
        "agent_version_no",
        "trace_id",
    ):
        assert required in runtime
    for required in (
        "knowledge.document_chunks",
        "scope_type",
        "scope_no",
        "VECTOR(1536)",
        "knowledge.indexing_jobs",
        "knowledge.retrieval_logs",
        "memory.items",
        "memory.summaries",
        "USING hnsw",
    ):
        assert required in knowledge


def test_mysql_phase_nine_migration_fixes_version_ownership_and_bindings() -> None:
    text = (
        Path(__file__).resolve().parents[1]
        / "migrations/mysql/versions/p50e3f4a5b6c_add_skill_bindings_and_kill_switches.py"
    ).read_text(encoding="utf-8")
    for required in (
        "ai_agent_skill_bindings",
        "ai_skill_tool_bindings",
        "ai_runtime_kill_switches",
        'op.drop_column("ai_tool_definitions", "input_schema")',
        'op.drop_column("ai_tool_definitions", "output_schema")',
        "confirmation_policy",
        "call_budget",
    ):
        assert required in text
