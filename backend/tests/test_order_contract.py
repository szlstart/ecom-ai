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
    operation = create_app().openapi()["paths"]["/api/v1/orders"]["post"]
    assert operation["operationId"] == "Order_Create"
    assert operation["responses"]["201"]
