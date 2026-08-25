from typing import Any

from app.generated.operation_trace_catalog import OPERATIONS
from app.main import create_app

REQUIRED_EXTENSIONS = {
    "x-requirement-id",
    "x-owner-kind",
    "x-permission-codes",
    "x-scope-policy",
    "x-domain-command",
    "x-audit-event",
    "x-idempotency-policy",
    "x-test-case-ids",
}


def _operations() -> dict[str, dict[str, Any]]:
    schema = create_app().openapi()
    return {
        operation["operationId"]: operation
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }


def test_every_openapi_operation_has_a_packaged_trace_contract() -> None:
    operations = _operations()
    assert set(operations) == set(OPERATIONS)
    for operation_id, operation in operations.items():
        assert REQUIRED_EXTENSIONS <= operation.keys(), operation_id
        assert operation["x-requirement-id"], operation_id
        assert operation["x-owner-kind"], operation_id
        assert operation["x-scope-policy"], operation_id
        assert operation["x-test-case-ids"], operation_id
        for extension in (
            "x-requirement-id",
            "x-owner-kind",
            "x-permission-codes",
            "x-scope-policy",
            "x-test-case-ids",
        ):
            assert isinstance(operation[extension], list), (operation_id, extension)
            assert all(isinstance(item, str) and item for item in operation[extension])
        for extension in (
            "x-domain-command",
            "x-audit-event",
            "x-idempotency-policy",
        ):
            assert isinstance(operation[extension], str) and operation[extension]


def test_permissions_are_extracted_from_each_endpoint_dependency() -> None:
    operations = _operations()
    assert operations["AdminProduct_Get"]["x-permission-codes"] == ["products:read"]
    assert operations["AdminProduct_Publish"]["x-permission-codes"] == [
        "products:publish"
    ]
    assert operations["AdminRefund_Decide"]["x-permission-codes"] == ["refunds:review"]
    assert operations["Order_GetMine"]["x-permission-codes"] == []
    assert operations["PaymentWebhook_Process"]["x-permission-codes"] == []


def test_write_concurrency_and_webhook_idempotency_contracts_are_explicit() -> None:
    operations = _operations()
    assert operations["AdminRefund_Decide"]["x-idempotency-policy"] == (
        "idempotency_key_required+if_match_required_by_domain"
    )
    assert operations["PaymentWebhook_Process"]["x-idempotency-policy"] == (
        "provider_event_idempotency"
    )
    assert operations["Order_GetMine"]["x-idempotency-policy"] == "safe_read"
    assert operations["FavoriteProduct_Put"]["x-idempotency-policy"] == (
        "http_method_idempotent"
    )
    assert operations["Message_CreateMine"]["x-idempotency-policy"] == (
        "payload_client_message_id_deduplication"
    )
    assert operations["AuthToken_Refresh"]["x-idempotency-policy"] == (
        "refresh_token_rotation_replay_detection"
    )


def test_no_write_operation_can_hide_an_unclassified_retry_policy() -> None:
    schema = create_app().openapi()
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            policy = operation["x-idempotency-policy"]
            assert policy != "none", operation["operationId"]
            if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                assert policy != "safe_read", operation["operationId"]


def test_every_operation_documents_recoverable_problem_details() -> None:
    operations = _operations()
    for operation_id, operation in operations.items():
        responses = operation["responses"]
        for status in ("429", "500", "503"):
            assert responses[status] == {
                "$ref": f"#/components/responses/Problem{status}"
            }, (operation_id, status)
        if operation.get("security"):
            assert "401" in responses, operation_id
        if operation["x-permission-codes"]:
            assert "403" in responses, operation_id


def test_write_and_entity_version_error_contracts_are_machine_readable() -> None:
    schema = create_app().openapi()
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            responses = operation["responses"]
            if "{" in path:
                assert "404" in responses, operation["operationId"]
            if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                assert "409" in responses, operation["operationId"]
            if "if_match_required" in operation["x-idempotency-policy"]:
                assert {"412", "428"} <= responses.keys(), operation["operationId"]
