from typing import Annotated

from fastapi import APIRouter, Header, Path, Response, status

from app.api.dependencies import IdempotencyKey, UserContext
from app.api.schemas import Envelope
from app.modules.cart.dependencies import CartServiceDependency
from app.modules.cart.schemas import (
    CartItemCreateRequest,
    CartItemPatchRequest,
    CartSelectionReplaceRequest,
    CartView,
)
from app.modules.identity.router import _etag, _expected_version, _no_store

router = APIRouter(prefix="/users/me/cart", tags=["cart"])


@router.get("", response_model=Envelope[CartView], operation_id="Cart_GetMine")
async def get_my_cart(
    response: Response,
    service: CartServiceDependency,
    context: UserContext,
) -> Envelope[CartView]:
    item = await service.get(context.user)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/items",
    response_model=Envelope[CartView],
    status_code=status.HTTP_200_OK,
    operation_id="CartItem_Create",
)
async def add_cart_item(
    payload: CartItemCreateRequest,
    response: Response,
    service: CartServiceDependency,
    context: UserContext,
    idempotency_key: IdempotencyKey,
) -> Envelope[CartView]:
    item = await service.add(context.user, payload, idempotency_key)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.patch(
    "/items/{cart_item_id}",
    response_model=Envelope[CartView],
    operation_id="CartItem_Patch",
)
async def patch_cart_item(
    cart_item_id: Annotated[str, Path(pattern=r"^ci_[0-9A-Z]+$", max_length=40)],
    payload: CartItemPatchRequest,
    response: Response,
    service: CartServiceDependency,
    context: UserContext,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[CartView]:
    item = await service.patch(context.user, cart_item_id, payload, _expected_version(if_match))
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.delete(
    "/items/{cart_item_id}",
    response_model=Envelope[CartView],
    operation_id="CartItem_Delete",
)
async def delete_cart_item(
    cart_item_id: Annotated[str, Path(pattern=r"^ci_[0-9A-Z]+$", max_length=40)],
    response: Response,
    service: CartServiceDependency,
    context: UserContext,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[CartView]:
    item = await service.delete(context.user, cart_item_id, _expected_version(if_match))
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.put(
    "/selection",
    response_model=Envelope[CartView],
    operation_id="CartSelection_Replace",
)
async def replace_cart_selection(
    payload: CartSelectionReplaceRequest,
    response: Response,
    service: CartServiceDependency,
    context: UserContext,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[CartView]:
    item = await service.replace_selection(context.user, payload, _expected_version(if_match))
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.delete(
    "/invalid-items",
    response_model=Envelope[CartView],
    operation_id="CartInvalidItem_Clear",
)
async def clear_invalid_cart_items(
    response: Response,
    service: CartServiceDependency,
    context: UserContext,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[CartView]:
    item = await service.clear_invalid(context.user, _expected_version(if_match))
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)
