import pytest

from app.modules.knowledge.cleaning import clean_document_text
from app.modules.knowledge.contracts import ToolCall, ToolScope, authorize_tool
from app.modules.knowledge.mcp_registry import MCP_SERVERS, server_for_tool
from app.modules.knowledge.retrieval import (
    RetrievedChunk,
    lexical_search,
    reciprocal_rank_fusion,
    rerank,
)
from app.modules.knowledge.skill_registry import SkillExecutionPlan, SkillToolPolicy


def test_tool_authorization_is_bound_to_trusted_scope_and_allowlist() -> None:
    call = ToolCall(
        protocol_version="2025-11-25",
        tool_code="catalog.search",
        arguments={"q": "keyboard"},
        scope=ToolScope("usr_01", "conv_01", "sto_01", None, None),
    )
    authorize_tool(call, frozenset({"catalog.search"}))
    with pytest.raises(PermissionError):
        authorize_tool(call, frozenset())


def test_knowledge_cleaning_removes_active_markup_and_control_characters() -> None:
    cleaned = clean_document_text("<script>ignore</script><p>退款\x00 政策</p>")
    assert "<" not in cleaned
    assert "ignore" not in cleaned
    assert "\x00" not in cleaned
    assert "退款" in cleaned


def test_hybrid_retrieval_fuses_duplicate_chunks() -> None:
    chunk = RetrievedChunk("doc_1", "v1", "safe", 1.0)
    result = reciprocal_rank_fusion([[chunk], [chunk]])
    assert len(result) == 1
    assert result[0].score > 0
    assert lexical_search([RetrievedChunk("doc_1", "v1", "退款政策", 0.0)], "退款")
    reranked = rerank(
        [
            RetrievedChunk("doc_1", "v1", "配送说明", 0.02, "chunk_1"),
            RetrievedChunk("doc_2", "v1", "退款政策支持七天退货", 0.01, "chunk_2"),
        ],
        "退款政策",
    )
    assert reranked[0].document_no == "doc_2"


def test_mcp_registry_has_six_non_overlapping_servers() -> None:
    assert set(MCP_SERVERS) == {
        "catalog-mcp",
        "order-mcp",
        "logistics-mcp",
        "after-sale-mcp",
        "support-mcp",
        "memory-mcp",
    }
    assert server_for_tool("catalog.get_product").server_code == "catalog-mcp"


def test_skill_hard_deny_wins_over_allow_and_keeps_budgets() -> None:
    plan = SkillExecutionPlan(
        skill_code="refund_status",
        skill_version_no=3,
        instructions="read only",
        input_schema={},
        output_schema={},
        tools=(
            SkillToolPolicy("after_sale.get_user_refund_detail", "allow", "none", 2, 3000),
            SkillToolPolicy("after_sale.get_user_refund_detail", "deny", "none", 0, 1),
            SkillToolPolicy("support.get_ticket_status", "allow", "none", 1, 2000),
        ),
    )
    assert plan.allowed_tools == {"support.get_ticket_status"}
