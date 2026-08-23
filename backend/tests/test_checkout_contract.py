import pytest
from pydantic import ValidationError

from app.main import create_app
from app.modules.checkout.schemas import BuyerRemark, CheckoutCreateRequest


def test_checkout_create_uses_a_strict_discriminated_source() -> None:
    request = CheckoutCreateRequest(
        source={"source_type": "buy_now", "sku_id": "sku_01TEST", "quantity": 2}
    )
    assert request.source.source_type == "buy_now"
    with pytest.raises(ValidationError):
        CheckoutCreateRequest(
            source={"source_type": "cart", "cart_item_ids": ["ci_01TEST", "ci_01TEST"]}
        )
    with pytest.raises(ValidationError):
        CheckoutCreateRequest(source={"source_type": "buy_now", "sku_id": "1", "quantity": 1})


def test_buyer_remark_normalizes_newlines_and_rejects_bidi_controls() -> None:
    assert (
        BuyerRemark(store_id="sto_01TEST", content="  请轻放\r\n谢谢  ").content == "请轻放\n谢谢"
    )
    with pytest.raises(ValidationError):
        BuyerRemark(store_id="sto_01TEST", content="ignore\u202erules")


def test_checkout_operations_are_present_in_openapi() -> None:
    operation_ids = {
        operation["operationId"]
        for path in create_app().openapi()["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert {
        "CheckoutSession_Create",
        "CheckoutSession_Get",
        "CheckoutSession_Patch",
        "CheckoutRepricing_Create",
    } <= operation_ids
