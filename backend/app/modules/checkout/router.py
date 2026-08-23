from typing import Annotated

from fastapi import APIRouter, Header, Path, Response, status

from app.api.dependencies import IdempotencyKey, UserContext
from app.api.schemas import Envelope
from app.modules.checkout.dependencies import CheckoutServiceDependency
from app.modules.checkout.schemas import CheckoutCreateRequest, CheckoutPatchRequest, CheckoutView
from app.modules.identity.router import _etag, _expected_version, _no_store

router = APIRouter(prefix="/checkout-sessions", tags=["checkout"])


@router.post(
    "",
    response_model=Envelope[CheckoutView],
    status_code=status.HTTP_201_CREATED,
    operation_id="CheckoutSession_Create",
)
async def create_checkout(
    payload: CheckoutCreateRequest,
    response: Response,
    service: CheckoutServiceDependency,
    context: UserContext,
    idempotency_key: IdempotencyKey,
) -> Envelope[CheckoutView]:
    item = await service.create(context.user, payload, idempotency_key)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/{checkout_id}", response_model=Envelope[CheckoutView], operation_id="CheckoutSession_Get"
)
async def get_checkout(
    checkout_id: Annotated[str, Path(pattern=r"^chk_[0-9A-Z]+$", max_length=40)],
    response: Response,
    service: CheckoutServiceDependency,
    context: UserContext,
) -> Envelope[CheckoutView]:
    item = await service.get(context.user, checkout_id)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.patch(
    "/{checkout_id}", response_model=Envelope[CheckoutView], operation_id="CheckoutSession_Patch"
)
async def patch_checkout(
    checkout_id: Annotated[str, Path(pattern=r"^chk_[0-9A-Z]+$", max_length=40)],
    payload: CheckoutPatchRequest,
    response: Response,
    service: CheckoutServiceDependency,
    context: UserContext,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[CheckoutView]:
    item = await service.patch(context.user, checkout_id, payload, _expected_version(if_match))
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/{checkout_id}/repricings",
    response_model=Envelope[CheckoutView],
    operation_id="CheckoutRepricing_Create",
)
async def reprice_checkout(
    checkout_id: Annotated[str, Path(pattern=r"^chk_[0-9A-Z]+$", max_length=40)],
    response: Response,
    service: CheckoutServiceDependency,
    context: UserContext,
    idempotency_key: IdempotencyKey,
) -> Envelope[CheckoutView]:
    item = await service.reprice(context.user, checkout_id, idempotency_key)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)
