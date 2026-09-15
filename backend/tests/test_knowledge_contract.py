from app.main import create_app
from app.modules.knowledge.indexing import structured_chunks
from app.modules.knowledge.retrieval import RetrievedChunk, lexical_search


def test_knowledge_search_contract_is_published() -> None:
    operation = create_app().openapi()["paths"]["/api/v1/admin/knowledge/searches"]["post"]
    assert operation["operationId"] == "AdminKnowledge_Search"


def test_structured_chunks_never_overlap_across_semantic_sections() -> None:
    body = """# 商品: 铅笔
## 商品详情
铅笔说明。铅笔说明。铅笔说明。
## 常见问题
### 问: 是 2B 吗
是。
# 商品: 直尺
## 商品详情
直尺说明。直尺说明。直尺说明。
"""

    chunks = structured_chunks(body, size=40, overlap=8)

    assert any(item.section_type == "faq" for item in chunks)
    assert all(not ("铅笔" in item.text and "直尺" in item.text) for item in chunks)
    assert all(item.heading_path for item in chunks)
    assert all(item.parent_section_id for item in chunks)


def test_chinese_merchant_policy_query_prefers_specific_review_rules() -> None:
    chunks = [
        RetrievedChunk("refund", "v1", "平台售后退款规则和退款到账说明", 0.0),
        RetrievedChunk(
            "merchant-review",
            "v1",
            "商家提交商品后执行商品自动审核，命中违禁内容或禁售规则时审核不通过。",
            0.0,
        ),
        RetrievedChunk("shipping", "v1", "店铺发货和物流规则", 0.0),
    ]

    result = lexical_search(chunks, "平台商家商品审核和禁售规则是什么？")

    assert result
    assert result[0].document_no == "merchant-review"


def test_knowledge_search_request_is_strict_and_scoped() -> None:
    schema = create_app().openapi()["components"]["schemas"]["KnowledgeSearchRequest"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["scope_type"]["pattern"] == "^(platform|store)$"


def test_skill_and_tool_governance_contracts_are_published() -> None:
    paths = create_app().openapi()["paths"]
    expected = {
        "/api/v1/admin/ai/skills": {"get": "AdminSkill_List", "post": "AdminSkill_Create"},
        "/api/v1/admin/ai/skills/{skill_id}/versions": {"post": "AdminSkill_VersionCreate"},
        "/api/v1/admin/ai/skills/{skill_id}/versions/{version_no}/publications": {
            "post": "AdminSkill_Publish"
        },
        "/api/v1/admin/ai/tools": {"get": "AdminTool_List", "post": "AdminTool_Create"},
        "/api/v1/admin/ai/tools/{tool_code}": {"get": "AdminTool_Get"},
        "/api/v1/admin/ai/tools/{tool_code}/versions/{version_no}/rollbacks": {
            "post": "AdminToolVersion_Rollback"
        },
        "/api/v1/admin/ai/agents": {"get": "AdminAgent_List"},
        "/api/v1/admin/ai/kill-switches": {"get": "AdminAiKillSwitch_List"},
        "/api/v1/admin/knowledge/documents": {
            "get": "AdminKnowledgeDocument_List",
            "post": "AdminKnowledgeDocument_Create",
        },
        "/api/v1/admin/knowledge/documents/{document_id}": {
            "get": "AdminKnowledgeDocument_Get",
            "delete": "AdminKnowledgeDocument_Delete",
        },
    }
    for path, operations in expected.items():
        for method, operation_id in operations.items():
            assert paths[path][method]["operationId"] == operation_id


def test_ai_publication_contracts_require_idempotency_and_return_approval_resource() -> None:
    paths = create_app().openapi()["paths"]
    operations = (
        paths["/api/v1/admin/ai/skills/{skill_id}/versions/{version_no}/publications"]["post"],
        paths["/api/v1/admin/ai/tools/{tool_code}/versions/{version_no}/publications"]["post"],
        paths["/api/v1/admin/ai/agents/{agent_id}/versions/{version_no}/publications"]["post"],
        paths["/api/v1/admin/ai/tools/{tool_code}/versions/{version_no}/rollbacks"]["post"],
    )
    for operation in operations:
        assert operation["responses"]["202"]
        parameters = {(item["in"], item["name"]) for item in operation["parameters"]}
        assert ("header", "Idempotency-Key") in parameters


def test_tool_detail_exposes_immutable_version_history() -> None:
    schema = create_app().openapi()["components"]["schemas"]
    tool = schema["ToolView"]
    assert "versions" in tool["properties"]
    version = schema["ToolVersionSummary"]
    assert {
        "version_no",
        "status",
        "input_schema",
        "output_schema",
        "evaluation_report",
        "published_at",
    } <= set(version["properties"])
