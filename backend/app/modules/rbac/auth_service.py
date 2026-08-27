from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta
from typing import Literal

import pyotp
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.idempotency import IdempotencyService
from app.core.security import (
    SecurityService,
    canonical_request_hash,
    normalize_target,
    normalize_username,
    utc_now,
)
from app.modules.identity.access_policy import classify_identity_grants
from app.modules.identity.models import AuthSession, User, UserCredential
from app.modules.identity.repository import IdentityRepository
from app.modules.identity.service import IdentityService
from app.modules.rbac.models import AdminMfaAuthenticator
from app.modules.rbac.repository import RbacRepository
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
    NavigationItem,
    ReauthenticationResult,
)

ADMIN_NAVIGATION = (
    ("dashboard", "仪表盘", "/admin/dashboard", "dashboard:read"),
    ("users", "用户与权限", "/admin/users", "users:read"),
    ("roles", "角色权限", "/admin/roles", "rbac:read"),
    ("stores", "店铺运营", "/admin/stores", "stores:read"),
    ("store-certifications", "店铺认证", "/admin/store-certifications", "stores:review"),
    ("products", "商品管理", "/admin/products", "products:read"),
    ("inventories", "库存调整", "/admin/inventories", "inventories:read"),
    ("orders", "订单管理", "/admin/orders", "orders:read"),
    ("payments", "支付管理", "/admin/payments", "payments:read"),
    ("refunds", "退款申请", "/admin/refund-applications", "refunds:read"),
    ("refund-appeals", "退款申诉", "/admin/refund-appeals", "refund_appeals:read"),
    ("reviews", "评价管理", "/admin/reviews", "reviews:read"),
    ("batch-jobs", "批处理任务", "/admin/system/jobs", "jobs:read"),
    ("categories", "平台分类", "/admin/categories", "catalog_taxonomy:manage"),
    ("brands", "品牌管理", "/admin/brands", "catalog_taxonomy:manage"),
    ("approvals", "审批中心", "/admin/approval-requests", "admin_approvals:read"),
    ("support", "人工客服", "/admin/support/tickets", "support:queue_read"),
    ("ai-agents", "Agent 管理", "/admin/ai/agents", "ai_agents:read"),
    ("ai-skills", "Skill 管理", "/admin/ai/skills", "ai_skills:read"),
    ("ai-tools", "MCP Tool 管理", "/admin/ai/tools", "ai_tools:read"),
    ("ai-policies", "AI 权限策略", "/admin/ai/policies", "ai_policies:read"),
    ("knowledge", "知识库", "/admin/knowledge/documents", "knowledge:read"),
    ("ai-evaluations", "AI 评估", "/admin/ai/evaluations", "ai_evaluations:read"),
    ("observability", "可观测性", "/admin/observability", "observability:read"),
    ("content", "平台内容", "/admin/content", "content:read"),
    (
        "dead-letter-events",
        "死信事件",
        "/admin/system/dead-letter-events",
        "events:read",
    ),
    ("audit", "审计日志", "/admin/audit-logs", "audit:read"),
)


class AdminAuthService:
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
        self.identity = IdentityRepository(session)
        self.rbac = RbacRepository(session)
        self.identity_service = IdentityService(session, redis, security, settings)
        self.idempotency = IdempotencyService(session)

    async def login(self, request: AdminLoginRequest, ip_address: str) -> AdminMfaChallenge:
        await self._rate_limit(
            f"login-ip:{self.security.keyed_hash('admin-login-ip', ip_address).hex()}",
            10,
            600,
        )
        user, credential = await self._resolve_identity(request.identifier)
        valid = self.security.verify_password(
            credential.secret_hash if credential is not None else None,
            request.password,
        )
        if user is None or credential is None or not valid:
            raise _invalid_admin_credentials()
        if user.user_status != "active":
            raise _invalid_admin_credentials()
        grants = await self.rbac.active_grants(user.id, utc_now())
        eligibility = classify_identity_grants(
            (role.role_code, grant.scope_type, grant.scope_id) for grant, role in grants
        )
        if not eligibility.platform_admin:
            raise _invalid_admin_credentials()
        authenticator = await self.rbac.active_admin_mfa(user.id)
        if authenticator is None:
            raise ApplicationError(
                status=403,
                code="ADMIN_MFA_NOT_ENROLLED",
                title="MFA enrollment required",
                detail="该管理账号尚未配置多因素认证。",
            )

        challenge = self.security.new_opaque_token(32)
        expires_at = utc_now() + timedelta(minutes=5)
        payload = json.dumps(
            {
                "user_no": user.user_no,
                "device_name": request.client.device_name,
                "expires_at": expires_at.isoformat(),
            },
            separators=(",", ":"),
        )
        await self.redis.set(self._challenge_key(challenge), payload, ex=300)
        methods: list[Literal["totp", "recovery_code"]] = ["totp"]
        if authenticator.recovery_codes_hashes:
            methods.append("recovery_code")
        return AdminMfaChallenge(
            challenge_id=challenge,
            allowed_methods=methods,
            expires_at=expires_at,
        )

    async def login_platform_password(
        self,
        request: AdminLoginRequest,
        ip_address: str,
        user_agent: str,
    ) -> tuple[AdminBootstrap, str]:
        await self._rate_limit(
            f"platform-login-ip:"
            f"{self.security.keyed_hash('platform-login-ip', ip_address).hex()}",
            10,
            600,
        )
        user, credential = await self._resolve_identity(request.identifier)
        valid = self.security.verify_password(
            credential.secret_hash if credential is not None else None,
            request.password,
        )
        if (
            user is None
            or credential is None
            or not valid
            or user.user_status != "active"
            or credential.must_change_password
        ):
            raise _invalid_admin_credentials()
        grants = await self.rbac.active_grants(user.id, utc_now())
        eligibility = classify_identity_grants(
            (role.role_code, grant.scope_type, grant.scope_id) for grant, role in grants
        )
        if not eligibility.platform_admin:
            raise _invalid_admin_credentials()

        user.last_login_at = utc_now()
        result = await self.identity_service.issue_session(
            user,
            audience="admin",
            client_type="admin_password",
            device_name=request.client.device_name,
            auth_methods=["password"],
            assurance_level="password_admin",
            ip_address=ip_address,
            user_agent=user_agent,
            commit=False,
        )
        permissions, scopes = await self._authorization_projection(user.id)
        await self.session.commit()
        return (
            AdminBootstrap(
                session=result.payload,
                permission_codes=permissions,
                scopes=scopes,
            ),
            result.refresh_token,
        )

    async def login_merchant(
        self,
        request: AdminLoginRequest,
        ip_address: str,
        user_agent: str,
    ) -> tuple[AdminBootstrap, str]:
        await self._rate_limit(
            f"merchant-login-ip:{self.security.keyed_hash('merchant-login-ip', ip_address).hex()}",
            10,
            600,
        )
        user, credential = await self._resolve_identity(request.identifier)
        valid = self.security.verify_password(
            credential.secret_hash if credential is not None else None,
            request.password,
        )
        if (
            user is None
            or credential is None
            or not valid
            or user.user_status != "active"
            or credential.must_change_password
        ):
            raise _invalid_merchant_credentials()
        grants = await self.rbac.active_grants(user.id, utc_now())
        eligibility = classify_identity_grants(
            (role.role_code, grant.scope_type, grant.scope_id) for grant, role in grants
        )
        if not eligibility.merchant:
            raise _invalid_merchant_credentials()

        user.last_login_at = utc_now()
        result = await self.identity_service.issue_session(
            user,
            audience="admin",
            client_type="merchant",
            device_name=request.client.device_name,
            auth_methods=["password"],
            assurance_level="aal1",
            ip_address=ip_address,
            user_agent=user_agent,
            commit=False,
        )
        permissions, scopes = await self._merchant_authorization_projection(user.id)
        await self.session.commit()
        return (
            AdminBootstrap(
                session=result.payload,
                permission_codes=permissions,
                scopes=scopes,
            ),
            result.refresh_token,
        )

    async def verify_mfa(
        self,
        request: AdminMfaVerificationRequest,
        ip_address: str,
        user_agent: str,
        idempotency_key: str,
    ) -> tuple[AdminBootstrap, str]:
        key = self._challenge_key(request.challenge_id)
        challenge_fingerprint = self.security.keyed_hash(
            "admin-mfa-idem",
            request.challenge_id,
        ).hex()
        claim = await self.idempotency.begin(
            scope_key=f"admin:mfa:{challenge_fingerprint}",
            idempotency_key=idempotency_key,
            payload={
                "fingerprint": self.security.keyed_hash(
                    "idempotency:admin-mfa",
                    canonical_request_hash(request.model_dump(mode="json")),
                ).hex()
            },
            resource_type="admin_session",
            ttl_days=1,
        )
        if claim.replayed and claim.record.resource_no and claim.record.response_body:
            user_no = claim.record.response_body.get("user_no")
            if isinstance(user_no, str):
                user = await self.identity.user_by_no(user_no, for_update=True)
                previous = await self.session.scalar(
                    select(AuthSession).where(AuthSession.session_no == claim.record.resource_no)
                )
                if user is not None:
                    if previous is not None and previous.revoked_at is None:
                        previous.revoked_at = utc_now()
                        previous.revoke_reason = "idempotency_session_replaced"
                    replacement = await self.identity_service.issue_session(
                        user,
                        audience="admin",
                        client_type="admin",
                        device_name="Admin request recovery",
                        auth_methods=["password", request.method],
                        assurance_level="aal2",
                        ip_address=ip_address,
                        user_agent=user_agent,
                        commit=False,
                    )
                    claim.record.resource_no = replacement.payload.session.session_id
                    permissions, scopes = await self._authorization_projection(user.id)
                    await self.session.commit()
                    return (
                        AdminBootstrap(
                            session=replacement.payload,
                            permission_codes=permissions,
                            scopes=scopes,
                        ),
                        replacement.refresh_token,
                    )
        raw = await self.redis.get(key)
        if not isinstance(raw, str):
            raise _invalid_mfa_challenge()
        challenge = json.loads(raw)
        user = await self.identity.user_by_no(str(challenge["user_no"]), for_update=True)
        if user is None or user.user_status != "active":
            raise _invalid_mfa_challenge()
        authenticator = await self.rbac.active_admin_mfa(user.id, for_update=True)
        if authenticator is None or not await self._verify_mfa_code(
            user, authenticator, request.method, request.code
        ):
            await self._rate_limit(f"mfa-failure:{user.user_no}", 5, 600)
            raise ApplicationError(
                status=401,
                code="ADMIN_MFA_INVALID",
                title="Invalid MFA code",
                detail="安全验证码无效。",
            )
        consumed = await self.redis.getdel(key)
        if consumed != raw:
            raise _invalid_mfa_challenge()
        authenticator.last_used_at = utc_now()
        result = await self.identity_service.issue_session(
            user,
            audience="admin",
            client_type="admin",
            device_name=str(challenge.get("device_name", "Admin web")),
            auth_methods=["password", request.method],
            assurance_level="aal2",
            ip_address=ip_address,
            user_agent=user_agent,
            commit=False,
        )
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=result.payload.session.session_id,
            response_body={"user_no": user.user_no},
        )
        await self.session.commit()
        permissions, scopes = await self._authorization_projection(user.id)
        return (
            AdminBootstrap(
                session=result.payload,
                permission_codes=permissions,
                scopes=scopes,
            ),
            result.refresh_token,
        )

    async def reauthenticate(
        self,
        user: User,
        auth_session: AuthSession,
        request: AdminReauthenticationRequest,
    ) -> ReauthenticationResult:
        credential = await self.identity.password_credential(user.id, for_update=True)
        authenticator = await self.rbac.active_admin_mfa(user.id, for_update=True)
        if (
            credential is None
            or not self.security.verify_password(credential.secret_hash, request.password)
            or authenticator is None
            or not await self._verify_mfa_code(user, authenticator, request.method, request.code)
        ):
            raise _invalid_admin_credentials()
        now = utc_now()
        auth_session.authenticated_at = now
        auth_session.assurance_level = "aal2"
        auth_session.authentication_methods = ["password", request.method]
        authenticator.last_used_at = now
        await self.session.commit()
        return ReauthenticationResult(
            reauth_expires_at=now + timedelta(seconds=self.settings.admin_recent_auth_seconds),
            assurance_level="aal2",
        )

    async def reauthenticate_merchant(
        self,
        user: User,
        auth_session: AuthSession,
        request: MerchantReauthenticationRequest,
        ip_address: str,
    ) -> ReauthenticationResult:
        await self._rate_limit(
            f"merchant-reauth:{user.user_no}:"
            f"{self.security.keyed_hash('merchant-reauth-ip', ip_address).hex()}",
            10,
            600,
        )
        if auth_session.client_type != "merchant":
            raise _invalid_merchant_credentials()
        credential = await self.identity.password_credential(user.id, for_update=True)
        grants = await self.rbac.active_grants(user.id, utc_now())
        eligibility = classify_identity_grants(
            (role.role_code, grant.scope_type, grant.scope_id) for grant, role in grants
        )
        if (
            credential is None
            or not self.security.verify_password(credential.secret_hash, request.password)
            or not eligibility.merchant
        ):
            raise _invalid_merchant_credentials()
        now = utc_now()
        auth_session.authenticated_at = now
        auth_session.assurance_level = "aal1"
        auth_session.authentication_methods = ["password"]
        await self.session.commit()
        return ReauthenticationResult(
            reauth_expires_at=now + timedelta(seconds=self.settings.admin_recent_auth_seconds),
            assurance_level="aal1",
        )

    async def reauthenticate_platform_password(
        self,
        user: User,
        auth_session: AuthSession,
        request: AdminPasswordReauthenticationRequest,
        ip_address: str,
    ) -> ReauthenticationResult:
        await self._rate_limit(
            f"platform-reauth:{user.user_no}:"
            f"{self.security.keyed_hash('platform-reauth-ip', ip_address).hex()}",
            10,
            600,
        )
        if auth_session.client_type != "admin_password":
            raise _invalid_admin_credentials()
        credential = await self.identity.password_credential(user.id, for_update=True)
        grants = await self.rbac.active_grants(user.id, utc_now())
        eligibility = classify_identity_grants(
            (role.role_code, grant.scope_type, grant.scope_id) for grant, role in grants
        )
        if (
            credential is None
            or not self.security.verify_password(credential.secret_hash, request.password)
            or not eligibility.platform_admin
        ):
            raise _invalid_admin_credentials()
        now = utc_now()
        auth_session.authenticated_at = now
        auth_session.assurance_level = "password_admin"
        auth_session.authentication_methods = ["password"]
        await self.session.commit()
        return ReauthenticationResult(
            reauth_expires_at=now + timedelta(seconds=self.settings.admin_recent_auth_seconds),
            assurance_level="password_admin",
        )

    async def me(self, user: User, auth_session: AuthSession) -> AdminMe:
        permissions, scopes = await self._authorization_projection(user.id)
        return AdminMe(
            user_id=user.user_no,
            username=user.username,
            nickname=user.nickname,
            assurance_level=auth_session.assurance_level,
            authenticated_at=auth_session.authenticated_at,
            permission_version=user.permission_version,
            permission_codes=permissions,
            scopes=scopes,
        )

    async def navigation(self, user_id: int) -> AdminNavigation:
        permissions, scopes = await self._authorization_projection(user_id)
        permission_set = set(permissions)
        items = [
            NavigationItem(
                code=code,
                title=title,
                route=route,
                required_permission=required_permission,
            )
            for code, title, route, required_permission in ADMIN_NAVIGATION
            if required_permission in permission_set
        ]
        return AdminNavigation(items=items, scopes=scopes)

    async def _resolve_identity(self, raw: str) -> tuple[User | None, UserCredential | None]:
        if "@" in raw:
            try:
                identifier = normalize_target("email", raw)
            except ValueError:
                return None, None
            contact = await self.identity.credential_by_identifier(
                "email",
                self.security.keyed_hash("credential-identifier", identifier),
                for_update=True,
            )
            user = await self.session.get(User, contact.user_id) if contact else None
        else:
            user = await self.identity.user_by_username(normalize_username(raw), for_update=True)
        password = (
            await self.identity.password_credential(user.id, for_update=True) if user else None
        )
        return user, password

    async def _verify_mfa_code(
        self,
        user: User,
        authenticator: AdminMfaAuthenticator,
        method: str,
        code: str,
    ) -> bool:
        if method == "totp":
            if authenticator.secret_ciphertext is None:
                return False
            secret = self.security.decrypt("admin-mfa-totp", authenticator.secret_ciphertext)
            if not pyotp.TOTP(secret).verify(code, valid_window=1):
                return False
            counter = int(time.time() // 30)
            replay_key = f"ecom:admin:mfa:replay:{user.user_no}:{counter}:{code}"
            return bool(await self.redis.set(replay_key, "1", nx=True, ex=90))
        if method != "recovery_code" or not authenticator.recovery_codes_hashes:
            return False
        candidate = self.security.keyed_hash("admin-mfa-recovery", code).hex()
        updated: list[dict[str, object]] = []
        matched = False
        for item in authenticator.recovery_codes_hashes:
            current = dict(item)
            if (
                not matched
                and current.get("used") is False
                and isinstance(current.get("hash"), str)
                and hmac.compare_digest(str(current["hash"]), candidate)
            ):
                current["used"] = True
                matched = True
            updated.append(current)
        if matched:
            authenticator.recovery_codes_hashes = updated
        return matched

    async def _authorization_projection(
        self, user_id: int
    ) -> tuple[list[str], list[dict[str, str | int]]]:
        rows = await self.rbac.permissions_for_user(user_id, utc_now())
        permissions = sorted({permission.permission_code for permission, _, _ in rows})
        scope_pairs = sorted({(grant.scope_type, grant.scope_id) for _, grant, _ in rows})
        scopes: list[dict[str, str | int]] = [
            {"scope_type": scope_type, "scope_id": scope_id} for scope_type, scope_id in scope_pairs
        ]
        return permissions, scopes

    async def _merchant_authorization_projection(
        self, user_id: int
    ) -> tuple[list[str], list[dict[str, str | int]]]:
        rows = await self.rbac.permissions_for_user(user_id, utc_now())
        merchant_rows = [
            (permission, grant)
            for permission, grant, role in rows
            if role.role_code == "store_operator" and grant.scope_type == "store"
        ]
        permissions = sorted({permission.permission_code for permission, _ in merchant_rows})
        scopes: list[dict[str, str | int]] = [
            {"scope_type": "store", "scope_id": scope_id}
            for scope_id in sorted({grant.scope_id for _, grant in merchant_rows})
        ]
        return permissions, scopes

    async def _rate_limit(self, key: str, limit: int, window: int) -> None:
        redis_key = f"ecom:rl:admin-auth:{key}"
        current = await self.redis.incr(redis_key)
        if current == 1:
            await self.redis.expire(redis_key, window)
        if current > limit:
            raise ApplicationError(
                status=429,
                code="AUTH_RATE_LIMITED",
                title="Too many attempts",
                detail="操作过于频繁，请稍后重试。",
                retryable=True,
            )

    def _challenge_key(self, challenge: str) -> str:
        digest = self.security.keyed_hash("admin-mfa-challenge", challenge).hex()
        return f"ecom:admin:mfa:challenge:{digest}"


def _invalid_admin_credentials() -> ApplicationError:
    hashlib.sha256(b"constant-work-marker").digest()
    return ApplicationError(
        status=401,
        code="ADMIN_AUTH_INVALID_CREDENTIALS",
        title="Invalid credentials",
        detail="账号、安全凭证或验证流程无效。",
    )


def _invalid_merchant_credentials() -> ApplicationError:
    hashlib.sha256(b"constant-work-marker").digest()
    return ApplicationError(
        status=401,
        code="MERCHANT_AUTH_INVALID_CREDENTIALS",
        title="Invalid merchant credentials",
        detail="商家账号或密码错误，或者该账号没有绑定可管理的店铺。",
    )


def _invalid_mfa_challenge() -> ApplicationError:
    return ApplicationError(
        status=410,
        code="ADMIN_MFA_CHALLENGE_EXPIRED",
        title="MFA challenge expired",
        detail="安全验证已过期，请重新登录。",
    )
