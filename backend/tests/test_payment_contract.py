import pytest
from pydantic import ValidationError

from app.main import create_app
from app.modules.payments.schemas import PaymentCreateRequest


def test_payment_create_request_rejects_client_amount_and_unregistered_provider() -> None:
    request = PaymentCreateRequest(
        trade_order_id="trd_01TEST",
        provider="fake",
        payment_method="fake_balance",
        return_url_key="payment_result",
    )
    assert request.provider == "fake"
    with pytest.raises(ValidationError):
        PaymentCreateRequest.model_validate(
            {**request.model_dump(), "requested_amount": {"minor_units": "1", "currency": "CNY"}}
        )
    with pytest.raises(ValidationError):
        PaymentCreateRequest.model_validate({**request.model_dump(), "provider": "unknown"})


def test_payment_user_operations_are_published() -> None:
    schema = create_app().openapi()
    assert schema["paths"]["/api/v1/payments"]["post"]["operationId"] == "Payment_Create"
    assert (
        schema["paths"]["/api/v1/payments/{payment_id}"]["get"]["operationId"] == "Payment_GetMine"
    )
    assert (
        schema["paths"]["/api/v1/trade-orders/{trade_order_id}/payments"]["get"]["operationId"]
        == "Payment_ListForTradeOrder"
    )
    assert (
        schema["paths"]["/api/v1/webhooks/payments/{provider}"]["post"]["operationId"]
        == "PaymentWebhook_Process"
    )
    assert (
        schema["paths"]["/api/v1/payments/{payment_id}/closures"]["post"]["operationId"]
        == "Payment_CloseMine"
    )


def test_admin_payment_operations_do_not_accept_a_target_status() -> None:
    schema = create_app().openapi()
    expected = {
        ("/api/v1/admin/payments", "get"): "AdminPayment_List",
        ("/api/v1/admin/payments/{payment_id}", "get"): "AdminPayment_Get",
        (
            "/api/v1/admin/payments/{payment_id}/reconciliations",
            "post",
        ): "AdminPayment_Reconcile",
    }
    for (path, method), operation_id in expected.items():
        assert schema["paths"][path][method]["operationId"] == operation_id
    request_schema = schema["components"]["schemas"]["AdminPaymentReconciliationRequest"]
    assert set(request_schema["properties"]) == {"reason_code", "reason"}
    assert "payment_status" not in request_schema["properties"]
