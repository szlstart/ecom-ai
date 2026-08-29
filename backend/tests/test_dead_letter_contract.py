from app.main import create_app


def test_dead_letter_operations_are_explicit_and_replay_has_no_payload_field() -> None:
    schema = create_app().openapi()
    expected = {
        ("/api/v1/admin/dead-letter-events", "get"): "AdminDeadLetter_List",
        ("/api/v1/admin/dead-letter-events/{dead_letter_id}", "get"): "AdminDeadLetter_Get",
        (
            "/api/v1/admin/dead-letter-events/{dead_letter_id}/replay-previews",
            "post",
        ): "AdminDeadLetter_Preview",
        (
            "/api/v1/admin/dead-letter-events/{dead_letter_id}/replays",
            "post",
        ): "AdminDeadLetter_Replay",
        (
            "/api/v1/admin/dead-letter-events/{dead_letter_id}/ignore",
            "post",
        ): "AdminDeadLetter_Ignore",
    }
    for (path, method), operation_id in expected.items():
        assert schema["paths"][path][method]["operationId"] == operation_id
    properties = schema["components"]["schemas"]["DeadLetterReplayRequest"]["properties"]
    assert set(properties) == {"preview_token", "reason_code", "reason"}
    assert "payload" not in properties
