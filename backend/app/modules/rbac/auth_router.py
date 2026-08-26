from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Header, Request, Response, status

from app.api.dependencies import AdminContext, IdempotencyKey
from app.api.schemas import Envelope
from app.core.config import get_settings
from app.modules.identity.dependencies import IdentityServiceDependency
from app.modules.identity.router import _client_ip, _no_store
from app.modules.identity.schemas import SessionBootstrap, SessionSummary
from app.modules.rbac.auth_dependencies import AdminAuthServiceDependency
from app.modules.rbac.schemas import (
    AdminBootstrap,
    AdminLoginRequest,
    AdminMe,
    AdminMfaChallenge,
    AdminMfaVerificationRequest,
    AdminNavigation,
    AdminPasswordReauthenticationRequest,
    AdminReauthenticationRequest,
    MerchantReauthenticationRequest,
    ReauthenticationResult,
)

router = APIRouter(prefix="/admin", tags=["admin-authentication"])
merchant_router = APIRouter(prefix="/merchant", tags=["merchant-authentication"])

ADMIN_REFRESH_COOKIE = "ecom_admin_refresh"
ADMIN_REFRESH_COOKIE_PATH = "/api/v1/admin/auth"
ADMIN_CSRF_COOKIE = "ecom_admin_csrf"


@router.post(
    "/auth/password-login",
    response_model=Envelope[AdminBootstrap],
    operation_id="AdminAuth_PasswordLogin",
)
async def admin_password_login(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    service: AdminAuthServiceDependency,
) -> Envelope[AdminBootstrap]:
    bootstrap, refresh_token = await service.login_platform_password(
        payload,
        _client_ip(request),
        request.headers.get("user-agent", "unknown")[:512],
    )
    _set_admin_refresh_cookie(response, refresh_token, bootstrap.session.csrf_token)
    _no_store(response)
    return Envelope(data=bootstrap)


@router.post(
    "/auth/password-reauthentications",
    response_model=Envelope[ReauthenticationResult],
    operation_id="AdminAuth_PasswordReauthenticate",
)
async def reauthenticate_admin_password(
    payload: AdminPasswordReauthenticationRequest,
    request: Request,
    response: Response,
    context: AdminContext,
    service: AdminAuthServiceDependency,
) -> Envelope[ReauthenticationResult]:
    _no_store(response)
    return Envelope(
        data=await service.reauthenticate_platform_password(
            context.user,
            context.session,
            payload,
            _client_ip(request),
        )
    )


@merchant_router.post(
    "/auth/login",
    response_model=Envelope[AdminBootstrap],
    operation_id="MerchantAuth_Login",
)
async def merchant_login(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    service: AdminAuthServiceDependency,
) -> Envelope[AdminBootstrap]:
    bootstrap, refresh_token = await service.login_merchant(
        payload,
        _client_ip(request),
        request.headers.get("user-agent", "unknown")[:512],
    )
    _set_admin_refresh_cookie(response, refresh_token, bootstrap.session.csrf_token)
    _no_store(response)
    return Envelope(data=bootstrap)


@merchant_router.post(
    "/auth/reauthentications",
    response_model=Envelope[ReauthenticationResult],
    operation_id="MerchantAuth_Reauthenticate",
)
async def reauthenticate_merchant(
    payload: MerchantReauthenticationRequest,
    request: Request,
    response: Response,
    context: AdminContext,
    service: AdminAuthServiceDependency,
) -> Envelope[ReauthenticationResult]:
    _no_store(response)
    return Envelope(
        data=await service.reauthenticate_merchant(
            context.user,
            context.session,
            payload,
            _client_ip(request),
        )
    )


@router.post(
    "/auth/login",
    response_model=Envelope[AdminMfaChallenge],
    operation_id="AdminAuth_Login",
    include_in_schema=False,
)
async def admin_login(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    service: AdminAuthServiceDependency,
) -> Envelope[AdminMfaChallenge]:
    _no_store(response)
    return Envelope(data=await service.login(payload, _client_ip(request)))


@router.post(
    "/auth/mfa-verifications",
    response_model=Envelope[AdminBootstrap],
    operation_id="AdminAuth_MfaVerify",
    include_in_schema=False,
)
async def verify_admin_mfa(
    payload: AdminMfaVerificationRequest,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKey,
    service: AdminAuthServiceDependency,
) -> Envelope[AdminBootstrap]:
    bootstrap, refresh_token = await service.verify_mfa(
        payload,
        _client_ip(request),
        request.headers.get("user-agent", "unknown")[:512],
        idempotency_key,
    )
    _set_admin_refresh_cookie(response, refresh_token, bootstrap.session.csrf_token)
    _no_store(response)
    return Envelope(data=bootstrap)


@router.post(
    "/auth/token-refresh",
    response_model=Envelope[SessionBootstrap],
    operation_id="AdminAuthToken_Refresh",
)
async def refresh_admin_token(
    request: Request,
    response: Response,
    service: IdentityServiceDependency,
    refresh_token: Annotated[str | None, Cookie(alias=ADMIN_REFRESH_COOKIE)] = None,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Envelope[SessionBootstrap]:
    result = await service.refresh(
        refresh_token,
        csrf_token,
        "admin",
        _client_ip(request),
        request.headers.get("user-agent", "unknown")[:512],
    )
    _set_admin_refresh_cookie(response, result.refresh_token, result.payload.csrf_token)
    _no_store(response)
    return Envelope(data=result.payload)


@router.post(
    "/auth/reauthentications",
    response_model=Envelope[ReauthenticationResult],
    operation_id="AdminAuth_Reauthenticate",
    include_in_schema=False,
)
async def reauthenticate_admin(
    payload: AdminReauthenticationRequest,
    response: Response,
    context: AdminContext,
    service: AdminAuthServiceDependency,
) -> Envelope[ReauthenticationResult]:
    _no_store(response)
    return Envelope(data=await service.reauthenticate(context.user, context.session, payload))


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="AdminAuth_Logout",
)
async def admin_logout(
    response: Response,
    context: AdminContext,
    service: IdentityServiceDependency,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    await service.logout(context.session, csrf_token)
    response.delete_cookie(
        ADMIN_REFRESH_COOKIE,
        path=ADMIN_REFRESH_COOKIE_PATH,
        secure=get_settings().refresh_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        ADMIN_CSRF_COOKIE,
        path="/",
        secure=get_settings().refresh_cookie_secure,
        httponly=False,
        samesite="strict",
    )
    _no_store(response)


@router.get(
    "/auth/sessions",
    response_model=Envelope[list[SessionSummary]],
    operation_id="AdminAuthSession_ListMine",
)
async def list_admin_sessions(
    response: Response,
    context: AdminContext,
    service: IdentityServiceDependency,
) -> Envelope[list[SessionSummary]]:
    _no_store(response)
    return Envelope(
        data=await service.list_sessions(
            context.user.id,
            "admin",
            context.session.session_no,
        )
    )


@router.delete(
    "/auth/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="AdminAuthSession_Revoke",
)
async def revoke_admin_session(
    session_id: str,
    context: AdminContext,
    service: IdentityServiceDependency,
) -> None:
    await service.revoke_session(
        context.user.id,
        session_id,
        audience="admin",
        reason="admin_session_revoked",
    )


@router.get(
    "/me",
    response_model=Envelope[AdminMe],
    operation_id="AdminMe_Get",
)
async def get_admin_me(
    response: Response,
    context: AdminContext,
    service: AdminAuthServiceDependency,
) -> Envelope[AdminMe]:
    _no_store(response)
    return Envelope(data=await service.me(context.user, context.session))


@router.get(
    "/navigation",
    response_model=Envelope[AdminNavigation],
    operation_id="AdminNavigation_Get",
)
async def get_admin_navigation(
    response: Response,
    context: AdminContext,
    service: AdminAuthServiceDependency,
) -> Envelope[AdminNavigation]:
    _no_store(response)
    return Envelope(data=await service.navigation(context.user.id))


def _set_admin_refresh_cookie(response: Response, refresh_token: str, csrf_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        ADMIN_REFRESH_COOKIE,
        refresh_token,
        max_age=settings.admin_refresh_token_ttl_hours * 60 * 60,
        path=ADMIN_REFRESH_COOKIE_PATH,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        ADMIN_CSRF_COOKIE,
        csrf_token,
        max_age=settings.admin_refresh_token_ttl_hours * 60 * 60,
        path="/",
        secure=settings.refresh_cookie_secure,
        httponly=False,
        samesite="strict",
    )
