from app.main import create_app


def test_ai_privacy_operations_require_confirmation_and_hide_storage_details() -> None:
    schema = create_app().openapi()
    expected = {
        ("/api/v1/users/me/ai-memory-items", "get"): "AiMemory_ListMine",
        ("/api/v1/users/me/ai-memory-items/{memory_id}/activations", "post"): "AiMemory_Activate",
        ("/api/v1/users/me/ai-memory-items/{memory_id}/revisions", "post"): "AiMemory_Revise",
        ("/api/v1/users/me/ai-memory-items/{memory_id}", "delete"): "AiMemory_Delete",
        ("/api/v1/users/me/ai-personalization/disable-all", "post"): "AiPersonalization_DisableAll",
        ("/api/v1/users/me/ai-cleanup-tasks/{task_id}", "get"): "CleanupTask_GetMine",
        ("/api/v1/users/me/ai-cleanup-tasks/{task_id}/retries", "post"): "CleanupTask_RetryMine",
    }
    for (path, method), operation_id in expected.items():
        assert schema["paths"][path][method]["operationId"] == operation_id
    memory_properties = schema["components"]["schemas"]["AiMemoryView"]["properties"]
    assert "embedding" not in memory_properties
    assert "content_ciphertext" not in memory_properties
    cleanup_properties = schema["components"]["schemas"]["AiCleanupTaskView"]["properties"]
    assert not {"object_key", "vector_key", "redis_key"} & set(cleanup_properties)
