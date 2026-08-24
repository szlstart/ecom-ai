from app.main import create_app


def test_knowledge_search_contract_is_published() -> None:
    operation = create_app().openapi()["paths"]["/api/v1/admin/knowledge/searches"]["post"]
    assert operation["operationId"] == "AdminKnowledge_Search"


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
        "/api/v1/admin/ai/agents": {"get": "AdminAgent_List"},
        "/api/v1/admin/ai/kill-switches": {"get": "AdminAiKillSwitch_List"},
        "/api/v1/admin/knowledge/documents": {
            "get": "AdminKnowledgeDocument_List",
            "post": "AdminKnowledgeDocument_Create",
        },
    }
    for path, operations in expected.items():
        for method, operation_id in operations.items():
            assert paths[path][method]["operationId"] == operation_id


def test_ai_publication_contracts_require_idempotency_and_return_approval_resource() -> None:
    paths = create_app().openapi()["paths"]
    operations = (
        paths["/api/v1/admin/ai/skills/{skill_id}/versions/{version_no}/publications"][
            "post"
        ],
        paths["/api/v1/admin/ai/tools/{tool_code}/versions/{version_no}/publications"]["post"],
        paths["/api/v1/admin/ai/agents/{agent_id}/versions/{version_no}/publications"][
            "post"
        ],
    )
    for operation in operations:
        assert operation["responses"]["202"]
        parameters = {(item["in"], item["name"]) for item in operation["parameters"]}
        assert ("header", "Idempotency-Key") in parameters
