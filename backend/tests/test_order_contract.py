import pytest
from pydantic import ValidationError

from app.main import create_app
from app.modules.orders.schemas import OrderCreateRequest


def test_order_create_accepts_only_checkout_identity_and_version() -> None:
    request = OrderCreateRequest(checkout_id="chk_01TEST", checkout_version=2)
    assert request.checkout_version == 2
    with pytest.raises(ValidationError):
        OrderCreateRequest.model_validate(
            {
                "checkout_id": "chk_01TEST",
                "checkout_version": 2,
                "payable_amount": "1",
            }
        )
    with pytest.raises(ValidationError):
        OrderCreateRequest(checkout_id="1", checkout_version=0)


def test_order_create_operation_is_present_in_openapi() -> None:
    schema = create_app().openapi()
    operation = schema["paths"]["/api/v1/orders"]["post"]
    assert operation["operationId"] == "Order_Create"
    assert operation["responses"]["201"]
    assert schema["paths"]["/api/v1/users/me/orders"]["get"]["operationId"] == "Order_ListMine"
    assert schema["paths"]["/api/v1/orders/{order_id}"]["get"]["operationId"] == "Order_GetMine"
    assert (
        schema["paths"]["/api/v1/orders/{order_id}/events"]["get"]["operationId"]
        == "OrderEvent_ListMine"
    )
    assert (
        schema["paths"]["/api/v1/trade-orders/{trade_order_id}"]["get"]["operationId"]
        == "TradeOrder_GetMine"
    )
    expected_commands = {
        ("/api/v1/orders/{order_id}/cancellations", "post"): "Order_CancelMine",
        ("/api/v1/orders/{order_id}/receipt-confirmations", "post"): ("Order_ConfirmReceiptMine"),
        ("/api/v1/users/me/orders/{order_id}", "delete"): "Order_HideMine",
        ("/api/v1/users/me/orders/{order_id}/restorations", "post"): ("Order_RestoreMine"),
        ("/api/v1/orders/{order_id}/repurchases", "post"): "Order_RepurchaseMine",
    }
    for (path, method), operation_id in expected_commands.items():
        assert schema["paths"][path][method]["operationId"] == operation_id
