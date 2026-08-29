from pathlib import Path


def test_ai_catalog_timestamp_repair_covers_all_immutable_catalog_tables() -> None:
    source = (
        Path(__file__).parents[1]
        / "migrations/mysql/versions/t94c7d8e9f0a_restore_ai_catalog_timestamp_defaults.py"
    ).read_text()
    assert 'down_revision = "s83b6c7d8e9f"' in source
    for table in (
        "ai_skill_definitions",
        "ai_skill_versions",
        "ai_tool_definitions",
        "ai_tool_versions",
    ):
        assert f'"{table}"' in source
    assert "DEFAULT (UTC_TIMESTAMP(6))" in source
