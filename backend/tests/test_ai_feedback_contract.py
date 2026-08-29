from app.main import create_app


def test_ai_feedback_operations_are_explicit_and_detail_feedback_is_bounded() -> None:
    schema = create_app().openapi()
    base = "/api/v1/conversations/{conversation_id}/messages/{message_id}"
    expected = {
        (f"{base}/reaction", "put"): "AiFeedbackReaction_Put",
        (f"{base}/reaction", "delete"): "AiFeedbackReaction_Delete",
        (f"{base}/reports", "post"): "AiFeedbackReport_Create",
        (f"{base}/corrections", "post"): "AiFeedbackCorrection_Create",
    }
    for (path, method), operation_id in expected.items():
        assert schema["paths"][path][method]["operationId"] == operation_id

    reaction = schema["components"]["schemas"]["AiReactionRequest"]
    assert reaction["additionalProperties"] is False
    assert set(reaction["properties"]) == {"reaction"}
    detail = schema["components"]["schemas"]["AiFeedbackDetailRequest"]
    assert detail["additionalProperties"] is False
    assert detail["properties"]["comment"]["maxLength"] == 2000
    assert "memory" not in detail["properties"]
