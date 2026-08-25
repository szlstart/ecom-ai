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
