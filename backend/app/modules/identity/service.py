from __future__ import annotations

import base64
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.passwords import PASSWORD_MAX_UTF8_BYTES
from app.core.security import (
    USERNAME_PATTERN,
    SecurityService,
    canonical_request_hash,
    mask_recovery_email,
    mask_target,
    normalize_target,
    normalize_username,
    utc_now,
)
from app.modules.content.models import PlatformContentEntry, PlatformContentVersion
from app.modules.finance.models import UserWallet
from app.modules.identity.access_policy import load_identity_eligibility
from app.modules.identity.models import (
    AuthAttempt,
    AuthSession,
    CredentialChangeRecord,
    PasswordResetRecord,
    User,
    UserAddress,
    UserAgreementAcceptance,
    UserCredential,
    VerificationCode,
)
from app.modules.identity.repository import IdentityRepository
from app.modules.identity.schemas import (
    AddressList,
    AddressPatch,
    AddressView,
    AddressWrite,
    ContactChangeRequest,
    ContactChangeTicketRequest,
    ContactChangeTicketResult,
    PasswordChangeRequest,
    PasswordLoginRequest,
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
    UserSummary,
    VerificationCodeAccepted,
    VerificationCodeRequest,
)
from app.modules.messaging.models import Conversation
from app.modules.rbac.models import UserRole
from app.modules.system.models import IdempotencyRecord, OutboxEvent

MAX_ACTIVE_ADDRESSES_PER_USER = 20
REGISTRATION_CONFIG_VERSION = "regcfg_2026_08_26"
REGISTRATION_CAPTCHA_TTL_SECONDS = 600
RESERVED_USERNAMES = frozenset(
    {
        "admin",
        "administrator",
        "api",
        "ecom_ai",
        "merchant",
        "operator",
        "root",
        "security",
        "service",
        "staff",
        "support",
        "system",
        "www",
    }
)


@dataclass(frozen=True)
class BootstrapResult:
    payload: SessionBootstrap
    refresh_token: str


class IdentityService:
    def __init__(
        self,
        session: AsyncSession,
        redis: Redis,
        security: SecurityService,
        settings: Settings,
    ) -> None:
        self.session = session
        self.redis = redis
        self.security = security
        self.settings = settings
        self.repository = IdentityRepository(session)
        self.idempotency = IdempotencyService(session)

    async def registration_config(self, ip_address: str) -> dict[str, object]:
        await self._enforce_rate_limit(
            f"registration-config:{self.security.keyed_hash('registration-ip', ip_address).hex()}",
            limit=60,
            window_seconds=600,
        )
        agreements = await self.repository.active_legal_versions()
        if len(agreements) != 2:
            raise ApplicationError(
                status=503,
                code="REGISTRATION_CONFIG_UNAVAILABLE",
                title="Registration unavailable",
                detail="注册配置暂不可用，请稍后重试。",
                retryable=True,
            )
        required = []
        for entry, version in sorted(agreements, key=lambda pair: pair[0].content_key):
            document_type = entry.content_key.removeprefix("legal.")
            required.append(
                {
                    "document_type": document_type,
                    "document_version": version.document_version,
                    "title": entry.title,
                    "content_url": (
                        f"/content/legal-documents/{document_type}"
                        f"?version={version.document_version}"
                    ),
                    "content_hash": _base64url(version.content_hash),
                }
            )
        captcha = await self._issue_registration_captcha()
        return {
            "config_version": REGISTRATION_CONFIG_VERSION,
            "locale": "zh-CN",
            "region_code": "CN",
            "username_policy": {
                "min_length": 4,
                "max_length": 32,
                "allowed_pattern": r"^[A-Za-z0-9_]+$",
                "policy_version": "username_v1",
            },
            "password_policy": {
                "non_empty": True,
                "forbid_whitespace": True,
                "allow_unicode": True,
                "policy_version": "password_v4",
            },
            "captcha": captcha,
            "required_agreements": required,
        }

    async def send_verification_code(
        self,
        request: VerificationCodeRequest,
        ip_address: str,
        user_id: int | None = None,
    ) -> VerificationCodeAccepted:
        try:
            target = normalize_target(request.target_type, request.target)
        except ValueError as exc:
            raise _field_error("/target", "INVALID_TARGET", "手机号或邮箱格式不正确。") from exc
        target_hash = self.security.keyed_hash("credential-identifier", target)
        change_ticket: CredentialChangeRecord | None = None
        if request.purpose in {"change_phone", "change_email"}:
            if user_id is None or request.change_ticket_id is None:
                raise ApplicationError(
                    status=403,
                    code="CONTACT_CHANGE_TICKET_REQUIRED",
                    title="Contact change ticket required",
                    detail="换绑验证码需要有效的安全验证凭据。",
                )
            change_ticket = await self.repository.credential_change_by_no(
                user_id,
                request.change_ticket_id,
                for_update=True,
            )
            expected_type = request.purpose.removeprefix("change_")
            if (
                change_ticket is None
                or change_ticket.change_status != "pending"
                or change_ticket.expires_at <= utc_now()
                or change_ticket.credential_type != expected_type
            ):
                raise ApplicationError(
                    status=410,
                    code="CONTACT_CHANGE_TICKET_EXPIRED",
                    title="Contact change ticket expired",
                    detail="换绑安全验证已失效，请重新发起。",
                )
            change_ticket.new_identifier_ciphertext = self.security.encrypt(
                f"user-credential:{expected_type}", target
            )
            change_ticket.new_identifier_hash = target_hash
        await self._enforce_rate_limit(
            f"verification:{request.purpose}:{target_hash.hex()}", limit=5, window_seconds=600
        )
        now = utc_now()
        expires_at = now + timedelta(minutes=10)
        debug_code = self.settings.debug_verification_code
        code = (
            debug_code.get_secret_value()
            if debug_code is not None and self.settings.environment != "production"
            else f"{secrets.randbelow(1_000_000):06d}"
        )
        previous = await self.session.scalars(
            select(VerificationCode).where(
                VerificationCode.target_hash == target_hash,
                VerificationCode.purpose == request.purpose,
                VerificationCode.consumed_at.is_(None),
                VerificationCode.invalidated_at.is_(None),
                VerificationCode.superseded_at.is_(None),
            )
        )
        for item in previous:
            item.superseded_at = now
        verification = VerificationCode(
            verification_no=new_prefixed_ulid("ver_"),
            purpose=request.purpose,
            target_type=request.target_type,
            target_hash=target_hash,
            code_hash=self.security.keyed_hash("verification-code", f"{target}:{code}"),
            attempt_count=0,
            max_attempts=5,
            send_channel="sms" if request.target_type == "phone" else "email",
            delivery_status="accepted",
            expires_at=expires_at,
            sent_at=now,
            request_ip_hash=self.security.keyed_hash("request-ip", ip_address),
            user_id=user_id if change_ticket is not None else None,
        )
        self.session.add(verification)
        self.session.add(
            self._auth_attempt(
                "code_send",
                "verification_code",
                "succeeded",
                "DELIVERY_ACCEPTED",
                identifier_hash=target_hash,
                ip_address=ip_address,
            )
        )
        await self.session.commit()
        return VerificationCodeAccepted(
            verification_id=verification.verification_no,
            target_masked=mask_target(request.target_type, target),
            expires_at=expires_at,
            retry_after_seconds=60,
        )

    async def register(
        self,
        request: RegistrationRequest,
        idempotency_key: str,
        ip_address: str,
        user_agent: str,
    ) -> BootstrapResult:
        self._validate_password(request.password)
        normalized_username = normalize_username(request.username)
        if not USERNAME_PATTERN.fullmatch(request.username):
            raise _field_error("/username", "INVALID_USERNAME", "用户名格式不正确。")
        if normalized_username in RESERVED_USERNAMES:
            raise _field_error(
                "/username",
                "REGISTRATION_USERNAME_RESERVED",
                "该用户名属于系统保留名称，请更换后再注册。",
            )
        username_hash = self.security.keyed_hash("username", normalized_username)
        try:
            normalized_email = normalize_target("email", request.email)
        except ValueError as exc:
            raise _field_error("/email", "INVALID_EMAIL", "邮箱格式不正确。") from exc
        email_hash = self.security.keyed_hash("credential-identifier", normalized_email)
        request_hash = self.security.keyed_hash(
            "registration-idempotency-payload",
            canonical_request_hash(request.model_dump(mode="json")),
        )
        scope = f"registration:{username_hash.hex()}:{request.config_version}"

        existing_idempotency = await self.repository.idempotency_record(
            scope, idempotency_key, for_update=True
        )
        if existing_idempotency is not None:
            if not hmac.compare_digest(existing_idempotency.request_hash, request_hash):
                raise ApplicationError(
                    status=409,
                    code="IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD",
                    title="Idempotency key reused",
                    detail="同一幂等键不能用于不同注册请求。",
                )
            if existing_idempotency.resource_no:
                user = await self.repository.user_by_no(existing_idempotency.resource_no)
                if user is not None:
                    return await self.issue_session(
                        user,
                        audience="user",
                        client_type="web",
                        device_name="Registration recovery",
                        auth_methods=["registration_recovery"],
                        assurance_level="aal1",
                        ip_address=ip_address,
                        user_agent=user_agent,
                    )
            raise ApplicationError(
                status=409,
                code="IDEMPOTENCY_IN_PROGRESS",
                title="Registration in progress",
                detail="注册正在处理中，请使用相同幂等键稍后重试。",
                retryable=True,
            )

        await self._enforce_rate_limit(
            f"registration-ip:{self.security.keyed_hash('registration-ip', ip_address).hex()}",
            limit=10,
            window_seconds=600,
        )
        await self._verify_registration_captcha(request.captcha_id, request.captcha_answer)
        legal_versions = await self._validate_registration_contract(request)
        if await self.repository.user_by_username(normalized_username) is not None:
            raise ApplicationError(
                status=409,
                code="REGISTRATION_USERNAME_UNAVAILABLE",
                title="Username unavailable",
                detail="该用户名已被注册，请更换一个用户名。",
                errors=[
                    {
                        "pointer": "/username",
                        "code": "REGISTRATION_USERNAME_UNAVAILABLE",
                        "message": "该用户名已被注册，请更换一个用户名。",
                    }
                ],
            )
        now = utc_now()
        user = User(
            user_no=new_prefixed_ulid("usr_"),
            username=request.username,
            username_normalized=normalized_username,
            nickname=request.username,
            user_status="active",
            locale=request.locale,
            timezone=request.timezone,
            registered_at=now,
            last_login_at=now,
        )
        self.session.add(user)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ApplicationError(
                status=409,
                code="REGISTRATION_CONFLICT",
                title="Registration conflict",
                detail="用户名刚刚被使用，请更换后重试。",
            ) from exc
        self.session.add(
            UserCredential(
                user_id=user.id,
                credential_type="password",
                secret_hash=self.security.hash_password(request.password),
                algorithm="argon2id",
                is_primary=True,
                is_verified=True,
                verified_at=now,
                password_changed_at=now,
                credential_status="active",
            )
        )
        self.session.add(
            UserCredential(
                user_id=user.id,
                credential_type="email",
                identifier_ciphertext=self.security.encrypt(
                    "user-credential:email", normalized_email
                ),
                identifier_hash=email_hash,
                key_version=1,
                is_primary=True,
                is_verified=False,
                credential_status="active",
            )
        )
        self.session.add(
            UserWallet(
                wallet_no=new_prefixed_ulid("wal_"),
                user_id=user.id,
                balance_amount=0,
                total_recharged_amount=0,
                currency="CNY",
                wallet_status="active",
            )
        )
        role = await self.repository.role_by_code("user")
        if role is None:
            raise ApplicationError(
                status=503,
                code="REGISTRATION_CONFIG_UNAVAILABLE",
                title="Registration unavailable",
                detail="默认角色尚未初始化。",
            )
        grant_key = self.security.keyed_hash("active-role-grant", f"{user.id}:{role.id}:platform:0")
        self.session.add(
            UserRole(
                user_id=user.id,
                role_id=role.id,
                grant_no=new_prefixed_ulid("grt_"),
                scope_type="platform",
                scope_id=0,
                grant_status="active",
                active_grant_key=grant_key,
                granted_by=user.id,
                granted_at=now,
                grant_reason="registration_default_role",
            )
        )
        for entry, version in legal_versions:
            self.session.add(
                UserAgreementAcceptance(
                    acceptance_no=new_prefixed_ulid("uaa_"),
                    user_id=user.id,
                    document_type=entry.content_key.removeprefix("legal."),
                    content_entry_id=entry.id,
                    content_version_id=version.id,
                    document_version=version.document_version,
                    content_hash=version.content_hash,
                    acceptance_context="registration",
                    accepted_at=now,
                    locale=request.locale,
                    region_code="CN",
                    ip_hash=self.security.keyed_hash("agreement-ip", ip_address),
                    user_agent_hash=self.security.keyed_hash("agreement-ua", user_agent),
                    request_id=_request_id(),
                    trace_id=_request_id(),
                )
            )
        self.session.add(
            Conversation(
                conversation_no=new_prefixed_ulid("con_"),
                user_id=user.id,
                conversation_type="exclusive",
                is_fixed=True,
                conversation_status="active",
            )
        )
        self.session.add(
            IdempotencyRecord(
                scope_key=scope,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                resource_type="user",
                resource_no=user.user_no,
                expires_at=now + timedelta(days=7),
            )
        )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="identity.user_registered.v1",
                aggregate_type="user",
                aggregate_no=user.user_no,
                aggregate_version=0,
                payload={"user_id": user.user_no, "locale": user.locale},
                event_status="pending",
                available_at=now,
                trace_id=_request_id(),
            )
        )
        try:
            result = await self.issue_session(
                user,
                audience="user",
                client_type="web",
                device_name="Registration",
                auth_methods=["password", "arithmetic_captcha"],
                assurance_level="aal1",
                ip_address=ip_address,
                user_agent=user_agent,
                commit=False,
            )
            self.session.add(
                self._auth_attempt(
                    "registration",
                    "arithmetic_captcha",
                    "succeeded",
                    "REGISTERED",
                    user_id=user.id,
                    session_id=None,
                    identifier_hash=username_hash,
                    ip_address=ip_address,
                )
            )
            await self.session.commit()
            return result
        except IntegrityError as exc:
            await self.session.rollback()
            raise ApplicationError(
                status=409,
                code="REGISTRATION_CONFLICT",
                title="Registration conflict",
                detail="用户名或邮箱刚刚被使用，请更换后重试。",
            ) from exc

    async def login(
        self,
        request: PasswordLoginRequest,
        ip_address: str,
        user_agent: str,
    ) -> BootstrapResult:
        await self._enforce_rate_limit(
            f"login-ip:{self.security.keyed_hash('login-ip', ip_address).hex()}",
            limit=20,
            window_seconds=600,
        )
        user, credential, monitoring_hash = await self._resolve_password_identity(
            request.identifier
        )
        await self._enforce_rate_limit(
            f"login-account:{monitoring_hash.hex()}",
            limit=10,
            window_seconds=600,
        )
        valid = self.security.verify_password(
            credential.secret_hash if credential is not None else None, request.password
        )
        now = utc_now()
        if credential is not None and credential.locked_until and credential.locked_until > now:
            valid = False
        if user is None or credential is None or not valid:
            if credential is not None:
                credential.failed_attempts = min(credential.failed_attempts + 1, 32_767)
                if credential.failed_attempts >= 5:
                    penalty_seconds = min(2 ** min(credential.failed_attempts - 5, 6), 60)
                    credential.locked_until = now + timedelta(seconds=penalty_seconds)
            self.session.add(
                self._auth_attempt(
                    "password_login",
                    "password",
                    "invalid",
                    "INVALID_CREDENTIALS",
                    user_id=user.id if user else None,
                    identifier_hash=monitoring_hash,
                    ip_address=ip_address,
                )
            )
            await self.session.commit()
            raise _invalid_credentials()
        credential.failed_attempts = 0
        credential.locked_until = None

        eligibility = await load_identity_eligibility(self.session, user.id, now)
        if not eligibility.consumer:
            self.session.add(
                self._auth_attempt(
                    "password_login",
                    "password",
                    "invalid",
                    "IDENTITY_SCOPE_MISMATCH",
                    user_id=user.id,
                    identifier_hash=monitoring_hash,
                    ip_address=ip_address,
                )
            )
            await self.session.commit()
            raise _invalid_credentials()

        if user.user_status != "active":
            raise ApplicationError(
                status=403,
                code="AUTH_ACCOUNT_UNAVAILABLE",
                title="Account unavailable",
                detail="账号当前不可用。",
            )
        if credential.must_change_password:
            raise ApplicationError(
                status=403,
                code="AUTH_PASSWORD_CHANGE_REQUIRED",
                title="Password change required",
                detail="必须先重置密码后才能登录。",
            )
        user.last_login_at = utc_now()
        result = await self.issue_session(
            user,
            audience="user",
            client_type="web",
            device_name=request.client.device_name,
            auth_methods=["password"],
            assurance_level="aal1",
            ip_address=ip_address,
            user_agent=user_agent,
            commit=False,
        )
        self.session.add(
            self._auth_attempt(
                "password_login",
                "password",
                "succeeded",
                "AUTHENTICATED",
                user_id=user.id,
                identifier_hash=monitoring_hash,
                ip_address=ip_address,
            )
        )
        await self.session.commit()
        return result

    async def refresh(
        self,
        refresh_token: str | None,
        csrf_token: str | None,
        audience: Literal["user", "admin"],
        ip_address: str,
        user_agent: str,
        *,
        allowed_client_types: frozenset[str],
    ) -> BootstrapResult:
        current, user, now, _validated_csrf = await self._validated_refresh_session(
            refresh_token,
            csrf_token,
            audience,
            allowed_client_types=allowed_client_types,
        )
        current.revoked_at = now
        current.revoke_reason = "rotated"
        return await self.issue_session(
            user,
            audience=audience,
            client_type=current.client_type,
            device_name=current.device_name or "Unknown device",
            auth_methods=list(current.authentication_methods),
            assurance_level=current.assurance_level,
            ip_address=ip_address,
            user_agent=user_agent,
            parent_session_id=current.id,
            token_family_no=current.token_family_no,
            authenticated_at=current.authenticated_at,
        )

    async def resume(
        self,
        refresh_token: str | None,
        csrf_token: str | None,
        audience: Literal["user", "admin"],
        *,
        allowed_client_types: frozenset[str],
    ) -> SessionBootstrap:
        """Restore an in-memory access token without rotating the browser session.

        Access tokens intentionally live only in page memory. A reload or a newly
        opened tab therefore needs a fresh access token, but that is not itself a
        refresh-token renewal event. Keeping this operation non-rotating prevents
        one tab from revoking the access token currently used by another tab.
        """
        current, user, now, validated_csrf = await self._validated_refresh_session(
            refresh_token,
            csrf_token,
            audience,
            allowed_client_types=allowed_client_types,
        )
        current.last_seen_at = now
        access_token, _access_expires_at = self.security.create_access_token(
            user_no=user.user_no,
            session_no=current.session_no,
            audience=audience,
            permission_version=user.permission_version,
        )
        await self.session.commit()
        return SessionBootstrap(
            user=await self._user_summary(user),
            session=self._session_summary(current, is_current=True),
            access_token=access_token,
            expires_in=self.settings.access_token_ttl_seconds,
            csrf_token=validated_csrf,
        )

    async def _validated_refresh_session(
        self,
        refresh_token: str | None,
        csrf_token: str | None,
        audience: Literal["user", "admin"],
        *,
        allowed_client_types: frozenset[str],
    ) -> tuple[AuthSession, User, datetime, str]:
        if not refresh_token or not csrf_token:
            raise _invalid_refresh()
        token_hash = self.security.keyed_hash("refresh-token", refresh_token)
        current = await self.repository.session_by_refresh_hash(token_hash, for_update=True)
        now = utc_now()
        if current is None or current.audience != audience:
            raise _invalid_refresh()
        if current.client_type not in allowed_client_types:
            raise _invalid_refresh()
        if not hmac.compare_digest(
            current.csrf_token_hash, self.security.keyed_hash("csrf-token", csrf_token)
        ):
            raise ApplicationError(
                status=403,
                code="AUTH_CSRF_INVALID",
                title="Invalid CSRF token",
                detail="安全校验失败，请重新登录。",
            )
        if current.revoked_at is not None:
            await self.repository.revoke_family(current.token_family_no, now, "refresh_token_reuse")
            await self.session.commit()
            raise ApplicationError(
                status=401,
                code="AUTH_REFRESH_REUSE_DETECTED",
                title="Refresh token reuse detected",
                detail="检测到登录凭证重放，相关会话已全部撤销。",
            )
        if current.expires_at <= now:
            current.revoked_at = now
            current.revoke_reason = "expired"
            await self.session.commit()
            raise _invalid_refresh()
        user = await self.session.get(User, current.user_id)
        if user is None or user.user_status != "active":
            raise _invalid_refresh()
        eligibility = await load_identity_eligibility(self.session, user.id, now)
        if not eligibility.allows_session(audience, current.client_type):
            await self.repository.revoke_family(
                current.token_family_no,
                now,
                "identity_scope_mismatch",
            )
            await self.session.commit()
            raise _invalid_refresh()
        return current, user, now, csrf_token

    async def logout(self, auth_session: AuthSession, csrf_token: str | None) -> None:
        self._validate_csrf(auth_session, csrf_token)
        if auth_session.revoked_at is None:
            auth_session.revoked_at = utc_now()
            auth_session.revoke_reason = "user_logout"
            await self.session.commit()

    async def list_sessions(
        self, user_id: int, audience: str, current_session_no: str
    ) -> list[SessionSummary]:
        sessions = await self.repository.active_sessions(user_id, audience)
        return [
            self._session_summary(item, is_current=item.session_no == current_session_no)
            for item in sessions
        ]

    async def revoke_session(
        self,
        user_id: int,
        session_no: str,
        *,
        audience: str = "user",
        reason: str = "user_revoked",
    ) -> None:
        item = await self.repository.session_by_no(user_id, session_no, for_update=True)
        if item is None or item.audience != audience:
            raise ApplicationError(
                status=404,
                code="AUTH_SESSION_NOT_FOUND",
                title="Session not found",
                detail="未找到该登录会话。",
            )
        if item.revoked_at is None:
            item.revoked_at = utc_now()
            item.revoke_reason = reason
            await self.session.commit()

    async def revoke_other_sessions(self, user_id: int, current_session_id: int) -> None:
        await self.repository.revoke_user_sessions(
            user_id, utc_now(), "user_revoked_others", except_session_id=current_session_id
        )
        await self.session.commit()

    async def password_reset_hint(
        self, request: PasswordResetHintRequest, ip_address: str
    ) -> PasswordResetHintResult:
        normalized_username = normalize_username(request.username)
        await self._enforce_rate_limit(
            f"password-reset-hint:{self.security.keyed_hash('request-ip', ip_address).hex()}",
            limit=10,
            window_seconds=600,
        )
        user = await self.repository.user_by_username(normalized_username)
        eligibility = (
            await load_identity_eligibility(self.session, user.id) if user is not None else None
        )
        email = (
            await self._recovery_email_for_user(user.id)
            if user is not None
            and eligibility is not None
            and (eligibility.consumer if request.audience == "consumer" else eligibility.merchant)
            else None
        )
        if email is None:
            raise ApplicationError(
                status=404,
                code="PASSWORD_RECOVERY_EMAIL_UNAVAILABLE",
                title="Recovery email unavailable",
                detail="未找到该用户名对应的可用找回邮箱。",
            )
        return PasswordResetHintResult(email_masked=mask_recovery_email(email))

    async def create_password_reset_ticket(
        self, request: PasswordResetTicketRequest, ip_address: str
    ) -> PasswordResetTicketResult:
        normalized_username = normalize_username(request.username)
        try:
            submitted_email = normalize_target("email", request.email)
        except ValueError as exc:
            raise _field_error("/email", "INVALID_EMAIL", "邮箱格式不正确。") from exc
        await self._enforce_rate_limit(
            f"password-reset-ticket:{self.security.keyed_hash('request-ip', ip_address).hex()}",
            limit=10,
            window_seconds=600,
        )
        user = await self.repository.user_by_username(normalized_username)
        eligibility = (
            await load_identity_eligibility(self.session, user.id) if user is not None else None
        )
        registered_email = (
            await self._recovery_email_for_user(user.id)
            if user is not None
            and eligibility is not None
            and (eligibility.consumer if request.audience == "consumer" else eligibility.merchant)
            else None
        )
        matches = registered_email is not None and hmac.compare_digest(
            self.security.keyed_hash("credential-identifier", registered_email),
            self.security.keyed_hash("credential-identifier", submitted_email),
        )
        if user is None or not matches:
            raise _field_error(
                "/email",
                "PASSWORD_RECOVERY_EMAIL_MISMATCH",
                "输入的完整邮箱与该账号登记邮箱不一致。",
            )
        password = await self.repository.password_credential(user.id, for_update=True)
        if password is None:
            raise _invalid_credentials()
        token = self.security.new_opaque_token()
        expires_at = utc_now() + timedelta(minutes=15)
        self.session.add(
            PasswordResetRecord(
                reset_no=new_prefixed_ulid("rst_"),
                user_id=user.id,
                verification_id=None,
                reset_token_hash=self.security.keyed_hash("reset-token", token),
                credential_version_before=password.credential_version,
                expires_at=expires_at,
                request_ip_hash=self.security.keyed_hash("request-ip", ip_address),
            )
        )
        await self.session.commit()
        return PasswordResetTicketResult(reset_ticket=token, expires_at=expires_at)

    async def reset_password(self, request: PasswordResetRequest, idempotency_key: str) -> None:
        reset_token_hash = self.security.keyed_hash("reset-token", request.reset_ticket).hex()
        claim = await self.idempotency.begin(
            scope_key=f"password-reset:{reset_token_hash}",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("password-reset", request.model_dump(mode="json")),
            resource_type="password_reset",
        )
        if claim.replayed:
            return
        self._validate_password(request.new_password)
        token_hash = self.security.keyed_hash("reset-token", request.reset_ticket)
        record = await self.repository.reset_by_hash(token_hash, for_update=True)
        now = utc_now()
        if record is None or record.invalidated_at or record.expires_at <= now:
            raise ApplicationError(
                status=410,
                code="PASSWORD_RESET_TICKET_EXPIRED",
                title="Password reset expired",
                detail="重置凭证已过期，请重新找回密码。",
            )
        if record.consumed_at is not None:
            self.idempotency.complete(
                claim,
                response_status=200,
                resource_no=record.reset_no,
            )
            await self.session.commit()
            return
        credential = await self.repository.password_credential(record.user_id, for_update=True)
        if credential is None or credential.credential_version != record.credential_version_before:
            raise ApplicationError(
                status=409,
                code="PASSWORD_RESET_CREDENTIAL_CHANGED",
                title="Credential changed",
                detail="密码已经变化，请重新发起找回流程。",
            )
        credential.secret_hash = self.security.hash_password(request.new_password)
        credential.credential_version += 1
        credential.password_changed_at = now
        credential.must_change_password = False
        record.consumed_at = now
        await self.repository.revoke_user_sessions(record.user_id, now, "password_reset")
        self.idempotency.complete(claim, response_status=200, resource_no=record.reset_no)
        await self.session.commit()

    async def profile(self, user: User) -> UserProfile:
        credentials = await self.repository.credentials_for_user(user.id)
        return UserProfile(
            user_id=user.user_no,
            username=user.username,
            nickname=user.nickname,
            avatar_url=await self._avatar_url(user),
            account_status=user.user_status,
            locale=user.locale,
            timezone=user.timezone,
            bound_accounts=self._bound_accounts(credentials),
            version=user.version,
        )

    async def update_profile(
        self, user: User, request: UserProfileUpdate, expected_version: int
    ) -> UserProfile:
        if user.version != expected_version:
            raise _version_mismatch()
        if request.nickname is not None:
            nickname = request.nickname.strip()
            if len(nickname) < 2:
                raise _field_error("/nickname", "INVALID_NICKNAME", "昵称至少需要 2 个字符。")
            user.nickname = nickname
        if request.locale is not None:
            user.locale = request.locale
        if request.timezone is not None:
            user.timezone = request.timezone
        if "avatar_file_id" in request.model_fields_set:
            if request.avatar_file_id is None:
                user.avatar_object_key = None
            else:
                avatar = await self.repository.file_by_no(request.avatar_file_id)
                if (
                    avatar is None
                    or avatar.purpose != "user_avatar"
                    or avatar.owner_type != "user"
                    or avatar.owner_no != user.user_no
                    or avatar.file_status != "active"
                    or avatar.scan_status != "safe"
                    or avatar.visibility != "public_derivative"
                ):
                    raise ApplicationError(
                        status=422,
                        code="FILE_NOT_BINDABLE",
                        title="File cannot be bound",
                        detail="头像文件不存在、尚未完成安全处理或不属于当前用户。",
                    )
                user.avatar_object_key = avatar.object_key
        user.version += 1
        await self.session.commit()
        return await self.profile(user)

    async def change_password(
        self,
        user: User,
        current_session: AuthSession,
        request: PasswordChangeRequest,
        idempotency_key: str,
    ) -> None:
        claim = await self.idempotency.begin(
            scope_key=f"user:{user.user_no}:password-change",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("password-change", request.model_dump(mode="json")),
            resource_type="user_password",
        )
        if claim.replayed:
            return
        self._validate_password(request.new_password)
        credential = await self.repository.password_credential(user.id, for_update=True)
        if credential is None or not self.security.verify_password(
            credential.secret_hash, request.current_password
        ):
            raise ApplicationError(
                status=422,
                code="USER_PASSWORD_MISMATCH",
                title="Password mismatch",
                detail="当前密码不正确。",
            )
        if self.security.verify_password(credential.secret_hash, request.new_password):
            raise _field_error("/new_password", "PASSWORD_REUSED", "新密码不能与当前密码相同。")
        credential.secret_hash = self.security.hash_password(request.new_password)
        credential.credential_version += 1
        credential.password_changed_at = utc_now()
        await self.repository.revoke_user_sessions(
            user.id,
            utc_now(),
            "password_changed",
            except_session_id=current_session.id,
            audience="user",
        )
        self.idempotency.complete(claim, response_status=200, resource_no=user.user_no)
        await self.session.commit()

    async def security_summary(self, user: User) -> SecuritySummary:
        credentials = await self.repository.credentials_for_user(user.id)
        password = next((item for item in credentials if item.credential_type == "password"), None)
        email_credential = next(
            (
                item
                for item in credentials
                if item.credential_type == "email" and item.identifier_ciphertext is not None
            ),
            None,
        )
        current_email = (
            self.security.decrypt("user-credential:email", email_credential.identifier_ciphertext)
            if email_credential is not None and email_credential.identifier_ciphertext is not None
            else None
        )
        sessions = await self.repository.active_sessions(user.id, "user")
        return SecuritySummary(
            password_set=password is not None,
            password_changed_at=password.password_changed_at if password else None,
            current_email=current_email,
            bound_accounts=self._bound_accounts(credentials),
            active_session_count=len(sessions),
        )

    async def dashboard(
        self, user_id: int, *, order_counts: dict[str, int] | None = None
    ) -> UserDashboard:
        addresses = await self.repository.addresses(user_id)
        default_address = next((item for item in addresses if item.is_default), None)
        unavailable_sections = ["reviews", "favorites", "messages"]
        if order_counts is None:
            order_counts = {
                "pending_payment": 0,
                "pending_shipment": 0,
                "in_transit": 0,
                "pending_review": 0,
                "after_sale": 0,
            }
            unavailable_sections.insert(0, "orders")
        return UserDashboard(
            order_counts=order_counts,
            review_counts={"pending": 0, "published": 0},
            default_address=self._address_view(default_address) if default_address else None,
            unread_message_count=0,
            favorite_product_count=0,
            followed_store_count=0,
            unavailable_sections=unavailable_sections,
        )

    async def create_contact_change_ticket(
        self,
        user: User,
        request: ContactChangeTicketRequest,
        ip_address: str,
        idempotency_key: str,
    ) -> ContactChangeTicketResult:
        claim = await self.idempotency.begin(
            scope_key=f"user:{user.user_no}:contact-change-ticket:{request.credential_type}",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload(
                "contact-change-ticket", request.model_dump(mode="json")
            ),
            resource_type="contact_change_ticket",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.credential_change_by_no(
                user.id,
                claim.record.resource_no,
            )
            if existing is not None:
                return ContactChangeTicketResult(
                    change_ticket_id=existing.change_no,
                    credential_type=cast(Literal["phone", "email"], existing.credential_type),
                    expires_at=existing.expires_at,
                )
            raise ApplicationError(
                status=409,
                code="IDEMPOTENCY_RESULT_UNAVAILABLE",
                title="Idempotency result unavailable",
                detail="原换绑安全验证结果已不可用，不能重复创建。",
            )
        password = await self.repository.password_credential(user.id, for_update=True)
        if password is None or not self.security.verify_password(
            password.secret_hash, request.current_password
        ):
            raise ApplicationError(
                status=422,
                code="USER_PASSWORD_MISMATCH",
                title="Password mismatch",
                detail="当前密码不正确。",
            )
        credentials = await self.repository.credentials_for_user(user.id)
        current = next(
            (item for item in credentials if item.credential_type == request.credential_type),
            None,
        )
        now = utc_now()
        expires_at = now + timedelta(minutes=15)
        await self.session.execute(
            update(CredentialChangeRecord)
            .where(
                CredentialChangeRecord.user_id == user.id,
                CredentialChangeRecord.credential_type == request.credential_type,
                CredentialChangeRecord.change_status == "pending",
            )
            .values(change_status="cancelled", cancelled_at=now)
        )
        ticket = CredentialChangeRecord(
            change_no=new_prefixed_ulid("cct_"),
            user_id=user.id,
            credential_type=request.credential_type,
            old_credential_id=current.id if current else None,
            credential_version_before=current.credential_version if current else 0,
            change_status="pending",
            expires_at=expires_at,
            request_ip_hash=self.security.keyed_hash("request-ip", ip_address),
        )
        self.session.add(ticket)
        self.idempotency.complete(
            claim,
            response_status=201,
            resource_no=ticket.change_no,
        )
        await self.session.commit()
        return ContactChangeTicketResult(
            change_ticket_id=ticket.change_no,
            credential_type=request.credential_type,
            expires_at=expires_at,
        )

    async def complete_contact_change(
        self,
        user: User,
        current_session: AuthSession,
        request: ContactChangeRequest,
        idempotency_key: str,
    ) -> None:
        claim = await self.idempotency.begin(
            scope_key=f"user:{user.user_no}:contact-change-complete",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("contact-change", request.model_dump(mode="json")),
            resource_type="contact_change",
        )
        if claim.replayed:
            return
        now = utc_now()
        try:
            new_email = normalize_target("email", request.new_email)
        except ValueError as exc:
            raise _field_error("/new_email", "INVALID_EMAIL", "邮箱格式不正确。") from exc
        target_hash = self.security.keyed_hash("credential-identifier", new_email)
        credentials = await self.repository.credentials_for_user(user.id)
        current = next((item for item in credentials if item.credential_type == "email"), None)
        if current is None:
            current = UserCredential(
                user_id=user.id,
                credential_type="email",
                credential_status="active",
                credential_version=1,
            )
            self.session.add(current)
        else:
            current.credential_version += 1
        current.identifier_ciphertext = self.security.encrypt("user-credential:email", new_email)
        current.identifier_hash = target_hash
        current.key_version = 1
        current.is_primary = True
        current.is_verified = False
        current.verified_at = None
        await self.repository.revoke_user_sessions(
            user.id,
            now,
            "contact_changed",
            except_session_id=current_session.id,
            audience="user",
        )
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=user.user_no,
        )
        await self.session.commit()

    async def _recovery_email_for_user(self, user_id: int) -> str | None:
        credentials = await self.repository.credentials_for_user(user_id)
        email = next(
            (
                item
                for item in credentials
                if item.credential_type == "email" and item.identifier_ciphertext is not None
            ),
            None,
        )
        if email is None or email.identifier_ciphertext is None:
            return None
        return self.security.decrypt("user-credential:email", email.identifier_ciphertext)

    async def cancel_contact_change(self, user_id: int, change_no: str) -> None:
        ticket = await self.repository.credential_change_by_no(
            user_id,
            change_no,
            for_update=True,
        )
        if ticket is None:
            return
        if ticket.change_status == "pending":
            ticket.change_status = "cancelled"
            ticket.cancelled_at = utc_now()
            await self.session.commit()

    async def list_addresses(self, user_id: int) -> AddressList:
        items = await self.repository.addresses(user_id)
        return AddressList(
            items=[self._address_view(item) for item in items],
            active_count=len(items),
            can_create=len(items) < MAX_ACTIVE_ADDRESSES_PER_USER,
        )

    async def get_address(self, user_id: int, address_no: str) -> AddressView:
        return self._address_view(await self._require_address(user_id, address_no))

    async def create_address(
        self, user: User, request: AddressWrite, idempotency_key: str
    ) -> AddressView:
        claim = await self.idempotency.begin(
            scope_key=f"user:{user.user_no}:address-create",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("address-create", request.model_dump(mode="json")),
            resource_type="user_address",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.address_by_no(user.id, claim.record.resource_no)
            if existing is not None:
                return self._address_view(existing)
            raise ApplicationError(
                status=409,
                code="IDEMPOTENCY_RESULT_UNAVAILABLE",
                title="Idempotency result unavailable",
                detail="原创建结果已不可用，不能使用同一幂等键再次创建。",
            )
        await self.session.refresh(user, with_for_update=True)
        count = await self.repository.address_count(user.id)
        if count >= MAX_ACTIVE_ADDRESSES_PER_USER:
            raise ApplicationError(
                status=409,
                code="ADDRESS_LIMIT_REACHED",
                title="Address limit reached",
                detail="最多只能保存 20 个有效收货地址。",
            )
        if request.is_default or count == 0:
            await self._clear_default_address(user.id)
            await self.session.flush()
        address = UserAddress(
            address_no=new_prefixed_ulid("adr_"),
            user_id=user.id,
            recipient_name_ciphertext=self.security.encrypt(
                "address-recipient", request.recipient_name
            ),
            phone_ciphertext=self.security.encrypt("address-phone", request.phone),
            phone_last4=request.phone[-4:],
            country_code=request.country_code,
            province_code=request.province_code,
            city_code=request.city_code,
            district_code=request.district_code,
            address_ciphertext=self.security.encrypt("address-detail", request.address),
            postal_code=request.postal_code,
            label=request.label,
            is_default=request.is_default or count == 0,
            key_version=1,
        )
        self.session.add(address)
        self.idempotency.complete(
            claim,
            response_status=201,
            resource_no=address.address_no,
        )
        await self.session.commit()
        await self.session.refresh(address)
        return self._address_view(address)

    async def update_address(
        self,
        user_id: int,
        address_no: str,
        request: AddressPatch,
        expected_version: int,
    ) -> AddressView:
        address = await self._require_address(user_id, address_no, for_update=True)
        if address.version != expected_version:
            raise _version_mismatch()
        values = request.model_dump(exclude_unset=True)
        if values.get("is_default") is True and not address.is_default:
            await self._clear_default_address(user_id)
            await self.session.flush()
        if "recipient_name" in values:
            address.recipient_name_ciphertext = self.security.encrypt(
                "address-recipient", values.pop("recipient_name")
            )
        if "phone" in values:
            phone = values.pop("phone")
            address.phone_ciphertext = self.security.encrypt("address-phone", phone)
            address.phone_last4 = phone[-4:]
        if "address" in values:
            address.address_ciphertext = self.security.encrypt(
                "address-detail", values.pop("address")
            )
        for key, value in values.items():
            setattr(address, key, value)
        address.version += 1
        await self.session.commit()
        return self._address_view(address)

    async def delete_address(self, user_id: int, address_no: str, expected_version: int) -> None:
        address = await self._require_address(user_id, address_no, for_update=True)
        if address.version != expected_version:
            raise _version_mismatch()
        was_default = address.is_default
        address.is_default = False
        address.deleted_at = utc_now()
        address.version += 1
        if was_default:
            await self.session.flush()
            remaining = await self.repository.addresses(user_id)
            replacement = next((item for item in remaining if item.id != address.id), None)
            if replacement is not None:
                replacement.is_default = True
                replacement.version += 1
        await self.session.commit()

    async def set_default_address(self, user_id: int, address_no: str) -> AddressView:
        address = await self._require_address(user_id, address_no, for_update=True)
        if not address.is_default:
            await self._clear_default_address(user_id)
            await self.session.flush()
            address.is_default = True
            address.version += 1
            await self.session.commit()
        return self._address_view(address)

    async def _validate_registration_contract(
        self, request: RegistrationRequest
    ) -> list[tuple[PlatformContentEntry, PlatformContentVersion]]:
        if request.config_version != REGISTRATION_CONFIG_VERSION:
            raise ApplicationError(
                status=409,
                code="AGREEMENT_VERSION_CHANGED",
                title="Registration policy changed",
                detail="注册规则已更新，请重新载入并确认协议。",
            )
        current = await self.repository.active_legal_versions(request.locale, "CN")
        expected = {
            (entry.content_key.removeprefix("legal."), version.document_version)
            for entry, version in current
        }
        submitted = {
            (item.document_type, item.document_version) for item in request.agreement_acceptances
        }
        if len(submitted) != len(request.agreement_acceptances) or submitted != expected:
            raise ApplicationError(
                status=409,
                code="AGREEMENT_VERSION_CHANGED",
                title="Agreement version changed",
                detail="协议版本已变化，请重新查看并主动确认。",
            )
        return current

    async def _verify_code(
        self,
        verification_no: str,
        code: str,
        purpose: str,
        target_type: str,
        target: str,
        *,
        consume: bool,
    ) -> VerificationCode:
        item = await self.repository.verification_by_no(verification_no, for_update=True)
        now = utc_now()
        if item is None or item.purpose != purpose or item.target_type != target_type:
            raise _verification_error("VERIFICATION_CODE_INVALID", 422, "验证码不正确。")
        expected_target_hash = self.security.keyed_hash("credential-identifier", target)
        if not hmac.compare_digest(item.target_hash, expected_target_hash):
            raise _verification_error("VERIFICATION_CODE_INVALID", 422, "验证码不正确。")
        if item.consumed_at is not None:
            raise _verification_error("VERIFICATION_CODE_CONSUMED", 409, "验证码已使用。")
        if (
            item.invalidated_at is not None
            or item.superseded_at is not None
            or item.expires_at <= now
        ):
            raise _verification_error("VERIFICATION_CODE_EXPIRED", 410, "验证码已过期。")
        if item.attempt_count >= item.max_attempts:
            raise _verification_error("VERIFICATION_CODE_EXPIRED", 410, "验证码已失效。")
        item.attempt_count += 1
        item.last_attempt_at = now
        expected_code_hash = self.security.keyed_hash("verification-code", f"{target}:{code}")
        if not hmac.compare_digest(item.code_hash, expected_code_hash):
            if item.attempt_count >= item.max_attempts:
                item.invalidated_at = now
            await self.session.commit()
            raise _verification_error("VERIFICATION_CODE_INVALID", 422, "验证码不正确。")
        if consume:
            item.consumed_at = now
        return item

    async def _resolve_password_identity(
        self, identifier: str
    ) -> tuple[User | None, UserCredential | None, bytes]:
        raw = identifier.strip()
        monitoring_hash = self.security.keyed_hash("auth-monitoring", normalize_username(raw))
        user = await self.repository.user_by_username(normalize_username(raw), for_update=True)
        credential = (
            await self.repository.password_credential(user.id, for_update=True) if user else None
        )
        return user, credential, monitoring_hash

    async def issue_session(
        self,
        user: User,
        *,
        audience: Literal["user", "admin"],
        client_type: str,
        device_name: str,
        auth_methods: list[str],
        assurance_level: str,
        ip_address: str,
        user_agent: str,
        parent_session_id: int | None = None,
        token_family_no: str | None = None,
        authenticated_at: datetime | None = None,
        commit: bool = True,
    ) -> BootstrapResult:
        now = utc_now()
        eligibility = await load_identity_eligibility(self.session, user.id, now)
        if not eligibility.allows_session(audience, client_type):
            raise ApplicationError(
                status=403,
                code="AUTH_IDENTITY_SCOPE_MISMATCH",
                title="Identity scope mismatch",
                detail="当前账号类型不能创建此入口的登录会话。",
            )
        refresh_token = self.security.new_opaque_token()
        csrf_token = self.security.new_opaque_token(24)
        ttl = (
            timedelta(days=self.settings.refresh_token_ttl_days)
            if audience == "user"
            else timedelta(hours=self.settings.admin_refresh_token_ttl_hours)
        )
        auth_session = AuthSession(
            session_no=new_prefixed_ulid("ses_"),
            user_id=user.id,
            refresh_token_hash=self.security.keyed_hash("refresh-token", refresh_token),
            token_family_no=token_family_no or new_prefixed_ulid("tfa_"),
            parent_session_id=parent_session_id,
            device_no=new_prefixed_ulid("dev_"),
            device_name=device_name[:128],
            client_type=client_type,
            audience=audience,
            csrf_token_hash=self.security.keyed_hash("csrf-token", csrf_token),
            ip_ciphertext=self.security.encrypt("session-ip", ip_address),
            user_agent_hash=self.security.keyed_hash("session-ua", user_agent),
            authenticated_at=authenticated_at or now,
            authentication_methods=auth_methods,
            assurance_level=assurance_level,
            issued_at=now,
            expires_at=now + ttl,
            last_seen_at=now,
        )
        self.session.add(auth_session)
        await self.session.flush()
        access_token, _access_expires_at = self.security.create_access_token(
            user_no=user.user_no,
            session_no=auth_session.session_no,
            audience=audience,
            permission_version=user.permission_version,
        )
        result = BootstrapResult(
            payload=SessionBootstrap(
                user=await self._user_summary(user),
                session=self._session_summary(auth_session, is_current=True),
                access_token=access_token,
                expires_in=self.settings.access_token_ttl_seconds,
                csrf_token=csrf_token,
            ),
            refresh_token=refresh_token,
        )
        if commit:
            await self.session.commit()
        return result

    async def _enforce_rate_limit(self, key: str, limit: int, window_seconds: int) -> None:
        try:
            redis_key = f"ecom:rl:auth:{key}"
            value = await self.redis.incr(redis_key)
            if value == 1:
                await self.redis.expire(redis_key, window_seconds)
            if value > limit:
                raise ApplicationError(
                    status=429,
                    code="AUTH_RATE_LIMITED",
                    title="Too many attempts",
                    detail="操作过于频繁，请稍后重试。",
                    retryable=True,
                )
        except RedisError as exc:
            raise ApplicationError(
                status=503,
                code="AUTH_RATE_LIMIT_UNAVAILABLE",
                title="Authentication temporarily unavailable",
                detail="安全校验暂不可用，请稍后重试。",
                retryable=True,
            ) from exc

    def _validate_password(self, password: str) -> None:
        if not password:
            raise _field_error(
                "/password",
                "PASSWORD_POLICY_FAILED",
                "密码不能为空。",
            )
        if any(character.isspace() for character in password):
            raise _field_error(
                "/password",
                "PASSWORD_WHITESPACE_FORBIDDEN",
                "密码不能包含空格、换行或其他空白字符。",
            )
        if len(password.encode("utf-8")) > PASSWORD_MAX_UTF8_BYTES:
            raise _field_error(
                "/password",
                "PASSWORD_INPUT_TOO_LARGE",
                f"密码不能超过 {PASSWORD_MAX_UTF8_BYTES} 个 UTF-8 字节。",
            )
        if len(password.encode("utf-8")) > PASSWORD_MAX_UTF8_BYTES:
            raise _field_error(
                "/password",
                "PASSWORD_INPUT_TOO_LARGE",
                f"密码不能超过 {PASSWORD_MAX_UTF8_BYTES} 个 UTF-8 字节。",
            )

    async def _issue_registration_captcha(self) -> dict[str, object]:
        left = secrets.randbelow(20) + 1
        right = secrets.randbelow(20) + 1
        operator = "+" if secrets.randbelow(2) == 0 else "-"
        if operator == "-" and right > left:
            left, right = right, left
        answer = left + right if operator == "+" else left - right
        captcha_id = secrets.token_urlsafe(24)
        answer_hash = self.security.keyed_hash(
            "registration-captcha", f"{captcha_id}:{answer}"
        ).hex()
        try:
            await self.redis.set(
                f"ecom:auth:registration-captcha:{captcha_id}",
                answer_hash,
                ex=REGISTRATION_CAPTCHA_TTL_SECONDS,
            )
        except RedisError as exc:
            raise ApplicationError(
                status=503,
                code="REGISTRATION_CAPTCHA_UNAVAILABLE",
                title="Registration captcha unavailable",
                detail="注册验证码暂不可用，请稍后重试。",
                retryable=True,
            ) from exc
        return {
            "captcha_id": captcha_id,
            "question": f"{left} {operator} {right} = ?",
            "expires_in_seconds": REGISTRATION_CAPTCHA_TTL_SECONDS,
        }

    async def _verify_registration_captcha(self, captcha_id: str, answer: str) -> None:
        key = f"ecom:auth:registration-captcha:{captcha_id}"
        submitted_hash = self.security.keyed_hash(
            "registration-captcha", f"{captcha_id}:{answer}"
        ).hex()
        try:
            stored_hash = await self.redis.get(key)
            if stored_hash is None or not hmac.compare_digest(stored_hash, submitted_hash):
                raise _field_error(
                    "/captcha_answer",
                    "REGISTRATION_CAPTCHA_INVALID",
                    "算术验证码不正确或已过期，请重新计算。",
                )
            if await self.redis.delete(key) != 1:
                raise _field_error(
                    "/captcha_answer",
                    "REGISTRATION_CAPTCHA_EXPIRED",
                    "算术验证码已使用或已过期，请刷新后重试。",
                )
        except RedisError as exc:
            raise ApplicationError(
                status=503,
                code="REGISTRATION_CAPTCHA_UNAVAILABLE",
                title="Registration captcha unavailable",
                detail="注册验证码暂不可用，请稍后重试。",
                retryable=True,
            ) from exc

    async def verify_registration_captcha(self, captcha_id: str, answer: str) -> None:
        """Consume the shared arithmetic captcha for another registration audience."""
        await self._verify_registration_captcha(captcha_id, answer)

    def _idempotency_payload(self, purpose: str, value: object) -> dict[str, str]:
        return {
            "fingerprint": self.security.keyed_hash(
                f"idempotency:{purpose}", canonical_request_hash(value)
            ).hex()
        }

    def _validate_csrf(self, auth_session: AuthSession, csrf_token: str | None) -> None:
        if not csrf_token or not hmac.compare_digest(
            auth_session.csrf_token_hash, self.security.keyed_hash("csrf-token", csrf_token)
        ):
            raise ApplicationError(
                status=403,
                code="AUTH_CSRF_INVALID",
                title="Invalid CSRF token",
                detail="安全校验失败，请刷新页面后重试。",
            )

    async def _require_address(
        self, user_id: int, address_no: str, *, for_update: bool = False
    ) -> UserAddress:
        item = await self.repository.address_by_no(user_id, address_no, for_update=for_update)
        if item is None:
            raise ApplicationError(
                status=404,
                code="ADDRESS_NOT_FOUND",
                title="Address not found",
                detail="未找到该收货地址。",
            )
        return item

    async def _clear_default_address(self, user_id: int) -> None:
        for item in await self.repository.addresses(user_id):
            if item.is_default:
                item.is_default = False
                item.version += 1

    def _address_view(self, item: UserAddress) -> AddressView:
        phone = self.security.decrypt("address-phone", item.phone_ciphertext)
        return AddressView(
            address_id=item.address_no,
            recipient_name=self.security.decrypt(
                "address-recipient", item.recipient_name_ciphertext
            ),
            phone=phone,
            phone_masked=f"{phone[:3]}****{item.phone_last4}",
            country_code=item.country_code,
            province_code=item.province_code,
            city_code=item.city_code,
            district_code=item.district_code,
            address=self.security.decrypt("address-detail", item.address_ciphertext),
            postal_code=item.postal_code,
            label=item.label,
            is_default=item.is_default,
            version=item.version,
        )

    def _bound_accounts(self, credentials: list[UserCredential]) -> list[dict[str, str | bool]]:
        result: list[dict[str, str | bool]] = []
        for item in credentials:
            if item.credential_type not in {"email", "phone"} or item.identifier_ciphertext is None:
                continue
            value = self.security.decrypt(
                f"user-credential:{item.credential_type}", item.identifier_ciphertext
            )
            result.append(
                {
                    "type": item.credential_type,
                    "masked": mask_target(item.credential_type, value),
                    "is_primary": item.is_primary,
                    "is_verified": item.is_verified,
                }
            )
        return result

    async def _user_summary(self, user: User) -> UserSummary:
        return UserSummary(
            user_id=user.user_no,
            username=user.username,
            nickname=user.nickname,
            avatar_url=await self._avatar_url(user),
            account_status=user.user_status,
        )

    async def _avatar_url(self, user: User) -> str | None:
        if not user.avatar_object_key:
            return None
        avatar = await self.repository.file_by_object_key(user.avatar_object_key)
        if (
            avatar is None
            or avatar.file_status != "active"
            or avatar.scan_status != "safe"
            or avatar.visibility != "public_derivative"
        ):
            return None
        return f"/api/v1/files/{avatar.file_no}"

    @staticmethod
    def _session_summary(item: AuthSession, *, is_current: bool) -> SessionSummary:
        return SessionSummary(
            session_id=item.session_no,
            client_type=item.client_type,
            device_name=item.device_name,
            audience=item.audience,
            authenticated_at=item.authenticated_at,
            last_seen_at=item.last_seen_at,
            expires_at=item.expires_at,
            is_current=is_current,
        )

    def _auth_attempt(
        self,
        attempt_type: str,
        auth_method: str,
        result: str,
        reason_code: str,
        *,
        user_id: int | None = None,
        session_id: int | None = None,
        identifier_hash: bytes | None = None,
        ip_address: str,
    ) -> AuthAttempt:
        now = utc_now()
        return AuthAttempt(
            attempt_no=new_prefixed_ulid("aat_"),
            attempt_type=attempt_type,
            auth_method=auth_method,
            user_id=user_id,
            identifier_monitoring_hash=identifier_hash,
            session_id=session_id,
            result=result,
            reason_code=reason_code,
            risk_level="low",
            risk_policy_version="risk_v1",
            ip_hash=self.security.keyed_hash("auth-ip", ip_address),
            occurred_at=now,
            request_id=_request_id(),
            trace_id=_request_id(),
        )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _request_id() -> str:
    return request_id_context.get() or new_prefixed_ulid("req_")


def _invalid_credentials() -> ApplicationError:
    return ApplicationError(
        status=401,
        code="AUTH_INVALID_CREDENTIALS",
        title="Invalid credentials",
        detail="账号或验证信息不正确。",
    )


def _invalid_refresh() -> ApplicationError:
    return ApplicationError(
        status=401,
        code="AUTH_REFRESH_INVALID",
        title="Invalid refresh credential",
        detail="登录已失效，请重新登录。",
    )


def _verification_error(code: str, status: int, message: str) -> ApplicationError:
    return ApplicationError(
        status=status,
        code=code,
        title="Verification failed",
        detail=message,
        errors=[{"pointer": "/verification_code", "code": code, "message": message}],
    )


def _field_error(pointer: str, code: str, message: str) -> ApplicationError:
    return ApplicationError(
        status=422,
        code=code,
        title="Validation failed",
        detail=message,
        errors=[{"pointer": pointer, "code": code, "message": message}],
    )


def _version_mismatch() -> ApplicationError:
    return ApplicationError(
        status=412,
        code="RESOURCE_VERSION_MISMATCH",
        title="Resource version mismatch",
        detail="资源已被其他操作修改，请刷新后重试。",
    )
