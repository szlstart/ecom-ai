import pytest
from pydantic import ValidationError

from app.modules.cart.schemas import CartItemPatchRequest, CartSelectionReplaceRequest


def test_cart_item_patch_requires_an_explicit_final_value() -> None:
    with pytest.raises(ValidationError):
        CartItemPatchRequest()


def test_cart_selection_only_accepts_unique_public_item_ids() -> None:
    with pytest.raises(ValidationError):
        CartSelectionReplaceRequest(cart_item_ids=["1"], is_selected=True)
    with pytest.raises(ValidationError):
        CartSelectionReplaceRequest(cart_item_ids=["ci_01TEST", "ci_01TEST"], is_selected=False)
