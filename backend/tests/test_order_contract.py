import pytest
from pydantic import ValidationError

from app.main import create_app
from app.modules.orders.schemas import AdminOrderAmountAdjustmentRequest, OrderCreateRequest


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


def test_order_list_exposes_the_eight_normative_views() -> None:
    operation = create_app().openapi()["paths"]["/api/v1/users/me/orders"]["get"]
    view_parameter = next(item for item in operation["parameters"] if item["name"] == "view")
    assert view_parameter["schema"]["enum"] == [
        "all",
        "pending_payment",
        "pending_shipment",
        "in_transit",
        "completed",
        "pending_review",
        "after_sale",
        "cancelled",
    ]


def test_order_item_projects_current_product_availability() -> None:
    schema = create_app().openapi()["components"]["schemas"]["OrderItemView"]
    assert "product_available" in schema["required"]
    assert schema["properties"]["product_available"]["type"] == "boolean"


def test_admin_order_operations_are_explicit_resources() -> None:
    paths = create_app().openapi()["paths"]
    expected = {
        ("/api/v1/admin/orders", "get"): "AdminOrder_List",
        ("/api/v1/admin/orders/{order_id}", "get"): "AdminOrder_Get",
        (
            "/api/v1/admin/orders/{order_id}/amount-adjustments",
            "post",
        ): "AdminOrder_AdjustAmount",
        (
            "/api/v1/admin/orders/{order_id}/cancellations",
            "post",
        ): "AdminOrder_Cancel",
    }
    for (path, method), operation_id in expected.items():
        assert paths[path][method]["operationId"] == operation_id


def test_admin_order_adjustment_rejects_unknown_fields() -> None:
    payload = {
        "adjustment_amount": {"minor_units": "-100", "currency": "CNY"},
        "reason_code": "MANUAL_PRICE_ADJUSTMENT",
        "reason": "活动差价人工修正",
    }
    assert AdminOrderAmountAdjustmentRequest.model_validate(payload).reason_code
    with pytest.raises(ValidationError):
        AdminOrderAmountAdjustmentRequest.model_validate(
            {**payload, "payable_amount": {"minor_units": "1", "currency": "CNY"}}
        )
