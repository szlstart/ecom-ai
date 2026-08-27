from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Header, Request, Response, status

from app.api.dependencies import IdempotencyKey, UserContext
from app.api.schemas import Envelope
from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.modules.identity.dependencies import IdentityServiceDependency
from app.modules.identity.schemas import (
    AccountClosureRequest,
    AddressList,
    AddressPatch,
    AddressView,
    AddressWrite,
    ContactChangeRequest,
    DefaultAddressRequest,
    LoginRequest,
    MessageResult,
    PasswordChangeRequest,
    PasswordResetHintRequest,
    PasswordResetHintResult,
    PasswordResetRequest,
    PasswordResetTicketRequest,
    PasswordResetTicketResult,
    RegistrationRequest,
    SecuritySummary,
    SessionBootstrap,
    SessionSummary,
    UserDashboard,
    UserProfile,
    UserProfileUpdate,
)
from app.modules.orders.dependencies import OrderServiceDependency

auth_router = APIRouter(prefix="/auth", tags=["authentication"])
user_router = APIRouter(prefix="/users/me", tags=["current-user"])

USER_REFRESH_COOKIE = "ecom_user_refresh"
USER_REFRESH_COOKIE_PATH = "/api/v1/auth"
USER_CSRF_COOKIE = "ecom_user_csrf"


@auth_router.get(
    "/registration-config",
    response_model=Envelope[dict[str, object]],
    operation_id="RegistrationConfig_Get",
)
async def get_registration_config(
    request: Request,
    service: IdentityServiceDependency,
) -> Envelope[dict[str, object]]:
    return Envelope(data=await service.registration_config(_client_ip(request)))


@auth_router.post(
    "/registrations",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[SessionBootstrap],
    operation_id="Registration_Create",
)
async def register_user(
    payload: RegistrationRequest,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKey,
    service: IdentityServiceDependency,
) -> Envelope[SessionBootstrap]:
    result = await service.register(
        payload,
        idempotency_key,
        _client_ip(request),
        request.headers.get("user-agent", "unknown")[:512],
    )
    _set_refresh_cookie(response, result.refresh_token, result.payload.csrf_token)
    _no_store(response)
    return Envelope(data=result.payload)


@auth_router.post(
    "/login",
    response_model=Envelope[SessionBootstrap],
    operation_id="Auth_Login",
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: IdentityServiceDependency,
) -> Envelope[SessionBootstrap]:
    result = await service.login(
        payload,
        _client_ip(request),
        request.headers.get("user-agent", "unknown")[:512],
    )
    _set_refresh_cookie(response, result.refresh_token, result.payload.csrf_token)
    _no_store(response)
    return Envelope(data=result.payload)


@auth_router.post(
    "/token-refresh",
    response_model=Envelope[SessionBootstrap],
    operation_id="AuthToken_Refresh",
)
async def refresh_token(
    request: Request,
    response: Response,
    service: IdentityServiceDependency,
    refresh_token: Annotated[str | None, Cookie(alias=USER_REFRESH_COOKIE)] = None,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Envelope[SessionBootstrap]:
    result = await service.refresh(
        refresh_token,
        csrf_token,
        "user",
        _client_ip(request),
        request.headers.get("user-agent", "unknown")[:512],
        allowed_client_types=frozenset({"web"}),
    )
    _set_refresh_cookie(response, result.refresh_token, result.payload.csrf_token)
    _no_store(response)
    return Envelope(data=result.payload)


@auth_router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="Auth_Logout",
)
async def logout(
    response: Response,
    context: UserContext,
    service: IdentityServiceDependency,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    await service.logout(context.session, csrf_token)
    response.delete_cookie(
        USER_REFRESH_COOKIE,
        path=USER_REFRESH_COOKIE_PATH,
        secure=get_settings().refresh_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        USER_CSRF_COOKIE,
        path="/",
        secure=get_settings().refresh_cookie_secure,
        httponly=False,
        samesite="lax",
    )
    _no_store(response)


@auth_router.get(
    "/sessions",
    response_model=Envelope[list[SessionSummary]],
    operation_id="AuthSession_ListMine",
)
async def list_my_sessions(
    context: UserContext,
    service: IdentityServiceDependency,
) -> Envelope[list[SessionSummary]]:
    return Envelope(
        data=await service.list_sessions(context.user.id, "user", context.session.session_no)
    )


@auth_router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="AuthSession_Revoke",
)
async def revoke_session(
    session_id: str,
    context: UserContext,
    service: IdentityServiceDependency,
) -> None:
    await service.revoke_session(context.user.id, session_id)


@auth_router.delete(
    "/sessions",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="AuthSession_RevokeOthers",
)
async def revoke_other_sessions(
    context: UserContext,
    service: IdentityServiceDependency,
) -> None:
    await service.revoke_other_sessions(context.user.id, context.session.id)


@auth_router.post(
    "/password-reset-hints",
    response_model=Envelope[PasswordResetHintResult],
    operation_id="PasswordResetHint_Get",
)
async def get_password_reset_hint(
    payload: PasswordResetHintRequest,
    request: Request,
    response: Response,
    service: IdentityServiceDependency,
) -> Envelope[PasswordResetHintResult]:
    _no_store(response)
    return Envelope(data=await service.password_reset_hint(payload, _client_ip(request)))


@auth_router.post(
    "/password-reset-tickets",
    response_model=Envelope[PasswordResetTicketResult],
    operation_id="PasswordResetTicket_Create",
)
async def create_password_reset_ticket(
    payload: PasswordResetTicketRequest,
    request: Request,
    response: Response,
    service: IdentityServiceDependency,
) -> Envelope[PasswordResetTicketResult]:
    _no_store(response)
    return Envelope(data=await service.create_password_reset_ticket(payload, _client_ip(request)))


@auth_router.post(
    "/password-resets",
    response_model=Envelope[MessageResult],
    operation_id="PasswordReset_Complete",
)
async def reset_password(
    payload: PasswordResetRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    service: IdentityServiceDependency,
) -> Envelope[MessageResult]:
    await service.reset_password(payload, idempotency_key)
    _no_store(response)
    return Envelope(data=MessageResult(message="密码已重置，请重新登录。"))


@user_router.get(
    "/dashboard",
    response_model=Envelope[UserDashboard],
    operation_id="UserDashboard_Get",
)
async def get_dashboard(
    context: UserContext,
    service: IdentityServiceDependency,
    order_service: OrderServiceDependency,
) -> Envelope[UserDashboard]:
    order_counts = await order_service.dashboard_counts(context.user)
    return Envelope(data=await service.dashboard(context.user.id, order_counts=order_counts))


@user_router.get(
    "",
    response_model=Envelope[UserProfile],
    operation_id="UserProfile_Get",
)
async def get_profile(
    response: Response,
    context: UserContext,
    service: IdentityServiceDependency,
) -> Envelope[UserProfile]:
    profile = await service.profile(context.user)
    response.headers["ETag"] = _etag(profile.version)
    return Envelope(data=profile)


@user_router.patch(
    "",
    response_model=Envelope[UserProfile],
    operation_id="UserProfile_Patch",
)
async def update_profile(
    payload: UserProfileUpdate,
    response: Response,
    context: UserContext,
    service: IdentityServiceDependency,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[UserProfile]:
    profile = await service.update_profile(context.user, payload, _expected_version(if_match))
    response.headers["ETag"] = _etag(profile.version)
    return Envelope(data=profile)


@user_router.put(
    "/password",
    response_model=Envelope[MessageResult],
    operation_id="UserPassword_Replace",
)
async def change_password(
    payload: PasswordChangeRequest,
    response: Response,
    context: UserContext,
    idempotency_key: IdempotencyKey,
    service: IdentityServiceDependency,
) -> Envelope[MessageResult]:
    await service.change_password(context.user, context.session, payload, idempotency_key)
    _no_store(response)
    return Envelope(data=MessageResult(message="密码已修改，其他登录会话已退出。"))


@user_router.get(
    "/security",
    response_model=Envelope[SecuritySummary],
    operation_id="UserSecurity_Get",
)
async def get_security_summary(
    response: Response,
    context: UserContext,
    service: IdentityServiceDependency,
) -> Envelope[SecuritySummary]:
    _no_store(response)
    return Envelope(data=await service.security_summary(context.user))


@user_router.post(
    "/contact-changes",
    response_model=Envelope[MessageResult],
    operation_id="UserContactChange_Complete",
)
async def complete_contact_change(
    payload: ContactChangeRequest,
    response: Response,
    context: UserContext,
    idempotency_key: IdempotencyKey,
    service: IdentityServiceDependency,
) -> Envelope[MessageResult]:
    await service.complete_contact_change(
        context.user,
        context.session,
        payload,
        idempotency_key,
    )
    _no_store(response)
    return Envelope(data=MessageResult(message="邮箱已更新。"))


@user_router.get(
    "/addresses",
    response_model=Envelope[AddressList],
    operation_id="Address_ListMine",
)
async def list_addresses(
    context: UserContext,
    service: IdentityServiceDependency,
) -> Envelope[AddressList]:
    return Envelope(data=await service.list_addresses(context.user.id))


@user_router.post(
    "/addresses",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AddressView],
    operation_id="Address_Create",
)
async def create_address(
    payload: AddressWrite,
    response: Response,
    context: UserContext,
    idempotency_key: IdempotencyKey,
    service: IdentityServiceDependency,
) -> Envelope[AddressView]:
    address = await service.create_address(context.user, payload, idempotency_key)
    response.headers["ETag"] = _etag(address.version)
    return Envelope(data=address)


@user_router.get(
    "/addresses/{address_id}",
    response_model=Envelope[AddressView],
    operation_id="Address_GetMine",
)
async def get_address(
    address_id: str,
    response: Response,
    context: UserContext,
    service: IdentityServiceDependency,
) -> Envelope[AddressView]:
    address = await service.get_address(context.user.id, address_id)
    response.headers["ETag"] = _etag(address.version)
    return Envelope(data=address)


@user_router.patch(
    "/addresses/{address_id}",
    response_model=Envelope[AddressView],
    operation_id="Address_Patch",
)
async def update_address(
    address_id: str,
    payload: AddressPatch,
    response: Response,
    context: UserContext,
    service: IdentityServiceDependency,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AddressView]:
    address = await service.update_address(
        context.user.id,
        address_id,
        payload,
        _expected_version(if_match),
    )
    response.headers["ETag"] = _etag(address.version)
    return Envelope(data=address)


@user_router.delete(
    "/addresses/{address_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="Address_Delete",
)
async def delete_address(
    address_id: str,
    context: UserContext,
    service: IdentityServiceDependency,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> None:
    await service.delete_address(context.user.id, address_id, _expected_version(if_match))


@user_router.put(
    "/default-address",
    response_model=Envelope[AddressView],
    operation_id="Address_SetDefault",
)
async def set_default_address(
    payload: DefaultAddressRequest,
    context: UserContext,
    service: IdentityServiceDependency,
) -> Envelope[AddressView]:
    return Envelope(data=await service.set_default_address(context.user.id, payload.address_id))


@user_router.post(
    "/account-closure-requests",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[MessageResult],
    operation_id="UserAccountClosureRequest_Create",
)
async def request_account_closure(
    payload: AccountClosureRequest,
    context: UserContext,
    idempotency_key: IdempotencyKey,
    service: IdentityServiceDependency,
) -> Envelope[MessageResult]:
    await service.request_account_closure(
        context.user,
        payload.reason_code,
        payload.reason,
        idempotency_key,
    )
    return Envelope(data=MessageResult(message="账号注销申请已受理，当前进入冷静期。"))


def _set_refresh_cookie(response: Response, refresh_token: str, csrf_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        USER_REFRESH_COOKIE,
        refresh_token,
        max_age=settings.refresh_token_ttl_days * 24 * 60 * 60,
        path=USER_REFRESH_COOKIE_PATH,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        USER_CSRF_COOKIE,
        csrf_token,
        max_age=settings.refresh_token_ttl_days * 24 * 60 * 60,
        path="/",
        secure=settings.refresh_cookie_secure,
        httponly=False,
        samesite="lax",
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _etag(version: int) -> str:
    return f'"v{version}"'


def _expected_version(value: str | None) -> int:
    if value is None:
        raise ApplicationError(
            status=428,
            code="PRECONDITION_REQUIRED",
            title="Precondition required",
            detail="该操作必须携带当前资源的 If-Match。",
        )
    candidate = value.strip()
    if len(candidate) < 4 or not candidate.startswith('"v') or not candidate.endswith('"'):
        raise ApplicationError(
            status=400,
            code="INVALID_IF_MATCH",
            title="Invalid If-Match",
            detail="If-Match 必须使用资源返回的 ETag。",
        )
    try:
        return int(candidate[2:-1])
    except ValueError as exc:
        raise ApplicationError(
            status=400,
            code="INVALID_IF_MATCH",
            title="Invalid If-Match",
            detail="If-Match 必须使用资源返回的 ETag。",
        ) from exc
