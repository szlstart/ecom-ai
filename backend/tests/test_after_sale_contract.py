from sqlalchemy import CheckConstraint, UniqueConstraint

from app.database.base import MySQLBase
from app.main import create_app
from app.modules.after_sale import models as after_sale_models  # noqa: F401


def test_after_sale_schema_guards_capacity_and_history() -> None:
    assert {
        "refund_applications",
        "refund_items",
        "refund_events",
        "refund_appeals",
        "refund_shipments",
    } <= set(MySQLBase.metadata.tables)
    applications = MySQLBase.metadata.tables["refund_applications"]
    checks = {
        constraint.name
        for constraint in applications.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert {
        "ck_refund_applications_refund_application_status",
        "ck_refund_applications_refund_application_type",
    } <= checks
    items = MySQLBase.metadata.tables["refund_items"]
    uniques = {
        constraint.name
        for constraint in items.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uk_refund_items_refund_item" in uniques


def test_after_sale_user_contract_is_published() -> None:
    schema = create_app().openapi()
    operations = {
        "/api/v1/refund-eligibility-checks": ("post", "RefundEligibility_Check"),
        "/api/v1/refund-applications": ("post", "RefundApplication_Create"),
        "/api/v1/users/me/refund-applications": ("get", "RefundApplication_ListMine"),
        "/api/v1/refund-applications/{refund_id}": ("get", "RefundApplication_GetMine"),
        "/api/v1/refund-applications/{refund_id}/events": ("get", "RefundEvent_ListMine"),
        "/api/v1/refund-applications/{refund_id}/cancellations": (
            "post",
            "RefundApplication_Cancel",
        ),
        "/api/v1/refund-applications/{refund_id}/appeals": (
            "post",
            "RefundAppeal_Create",
        ),
        "/api/v1/refund-appeals/{appeal_id}": ("get", "RefundAppeal_GetMine"),
        "/api/v1/refund-appeals/{appeal_id}/events": (
            "get",
            "RefundAppealEvent_ListMine",
        ),
        "/api/v1/refund-appeals/{appeal_id}/cancellations": (
            "post",
            "RefundAppeal_Cancel",
        ),
        "/api/v1/admin/refund-applications/{refund_id}/claims": (
            "post",
            "AdminRefund_Claim",
        ),
        "/api/v1/admin/refund-appeals/{appeal_id}/claims": (
            "post",
            "AdminRefundAppeal_Claim",
        ),
        "/api/v1/refund-applications/{refund_id}/return-shipment": (
            "put",
            "RefundReturnShipment_Upsert",
        ),
        "/api/v1/webhooks/refunds/{provider}": ("post", "RefundPaymentWebhook_Process"),
        "/api/v1/admin/refund-applications/{refund_id}": ("get", "AdminRefund_Get"),
        "/api/v1/admin/refund-applications/{refund_id}/decisions": (
            "post",
            "AdminRefund_Decide",
        ),
    }
    for path, (method, operation_id) in operations.items():
        assert schema["paths"][path][method]["operationId"] == operation_id

    for path in (
        "/api/v1/refund-appeals/{appeal_id}/cancellations",
        "/api/v1/admin/refund-applications/{refund_id}/claims",
        "/api/v1/admin/refund-applications/{refund_id}/decisions",
        "/api/v1/admin/refund-appeals/{appeal_id}/claims",
        "/api/v1/admin/refund-appeals/{appeal_id}/decisions",
    ):
        parameters = schema["paths"][path]["post"]["parameters"]
        assert any(
            parameter["name"] == "Idempotency-Key" and parameter["required"] is True
            for parameter in parameters
        )
    appeal_cancel_parameters = schema["paths"][
        "/api/v1/refund-appeals/{appeal_id}/cancellations"
    ]["post"]["parameters"]
    assert any(
        parameter["name"] == "If-Match" and parameter["required"] is False
        for parameter in appeal_cancel_parameters
    )
    for path in (
        "/api/v1/admin/refund-applications/{refund_id}/decisions",
        "/api/v1/admin/refund-appeals/{appeal_id}/decisions",
    ):
        assert "202" in schema["paths"][path]["post"]["responses"]

    request = schema["components"]["schemas"]["RefundApplicationCreateRequest"]
    assert request["additionalProperties"] is False
    assert request["properties"]["items"]["maxItems"] == 50
