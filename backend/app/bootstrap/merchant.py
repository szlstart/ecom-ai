from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from decimal import Decimal

import pyotp
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, normalize_username, utc_now
from app.modules.identity.models import User, UserCredential
from app.modules.rbac.models import (
    AdminMfaAuthenticator,
    Permission,
    Role,
    RolePermission,
    UserRole,
)
from app.modules.stores.models import Store

STORE_OPERATOR_PERMISSIONS = (
    "stores:read",
    "stores:manage",
    "store_policies:read",
    "store_policies:create",
    "store_policies:update",
    "store_policies:publish",
    "products:read",
    "products:create",
    "products:update",
    "products:publish",
    "inventories:read",
    "inventories:adjust",
    "reviews:read",
    "reviews:reply",
    "support:queue_read",
    "support:claim",
    "support:reply",
    "support:wait",
    "support:resume",
    "support:resolve",
)


@dataclass(frozen=True)
class MerchantProvisioningResult:
    user_no: str
    username: str
    store_no: str
    store_name: str
    totp_secret: str
    provisioning_uri: str
    recovery_codes: list[str]


async def provision_store_operator(
    session: AsyncSession,
    security: SecurityService,
    *,
    username: str,
    password: str,
    store_no: str | None = None,
    store_name: str | None = None,
) -> MerchantProvisioningResult:
    """Create one store-scoped operator for bootstrap and local development.

    Existing stores can be selected by public ID. When no public ID is supplied,
    a new active store is created and owned by the new operator. Production store
    onboarding remains subject to the certification workflow.
    """

    normalized_username = normalize_username(username)
    existing = await session.scalar(
        select(User.id).where(User.username_normalized == normalized_username)
    )
    if existing is not None:
        raise ValueError("username is already in use")
    if store_no is None and not store_name:
        raise ValueError("store_no or store_name is required")

    role = await session.scalar(
        select(Role).where(
            Role.role_code == "store_operator",
            Role.scope_type == "store",
            Role.role_status == "active",
        )
    )
    if role is None:
        raise RuntimeError("reference roles are not seeded")

    now = utc_now()
    user = User(
        user_no=new_prefixed_ulid("usr_"),
        username=username,
        username_normalized=normalized_username,
        nickname=username,
        user_status="active",
        locale="zh-CN",
        timezone="Asia/Shanghai",
        registered_at=now,
    )
    session.add(user)
    await session.flush()
    session.add(
        UserCredential(
            user_id=user.id,
            credential_type="password",
            secret_hash=security.hash_password(password),
            algorithm="argon2id",
            is_primary=True,
            is_verified=True,
            verified_at=now,
            password_changed_at=now,
            credential_status="active",
        )
    )

    if store_no is not None:
        store = await session.scalar(select(Store).where(Store.store_no == store_no))
        if store is None:
            raise ValueError("store does not exist")
    else:
        assert store_name is not None
        normalized_store_name = " ".join(store_name.casefold().split())
        duplicate_store = await session.scalar(
            select(Store.id).where(Store.store_name_normalized == normalized_store_name)
        )
        if duplicate_store is not None:
            raise ValueError("store name is already in use")
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=user.id,
            store_name=store_name,
            store_name_normalized=normalized_store_name,
            description="欢迎来到我们的店铺。",
            store_status="active",
            rating_score=Decimal("0.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=now,
        )
        session.add(store)
        await session.flush()

    grant_key = security.keyed_hash("active-role-grant", f"{user.id}:{role.id}:store:{store.id}")
    session.add(
        UserRole(
            user_id=user.id,
            role_id=role.id,
            grant_no=new_prefixed_ulid("grt_"),
            scope_type="store",
            scope_id=store.id,
            grant_status="active",
            active_grant_key=grant_key,
            granted_by=user.id,
            granted_at=now,
            grant_reason="store_operator_provisioning",
        )
    )

    permissions = list(
        (
            await session.scalars(
                select(Permission).where(
                    Permission.permission_code.in_(STORE_OPERATOR_PERMISSIONS),
                    Permission.permission_status == "active",
                )
            )
        ).all()
    )
    found_codes = {permission.permission_code for permission in permissions}
    missing_codes = set(STORE_OPERATOR_PERMISSIONS) - found_codes
    if missing_codes:
        raise RuntimeError(f"missing permissions: {', '.join(sorted(missing_codes))}")
    allowed_permission_ids = {permission.id for permission in permissions}
    await session.execute(
        delete(RolePermission).where(
            RolePermission.role_id == role.id,
            ~RolePermission.permission_id.in_(allowed_permission_ids),
        )
    )
    existing_permissions = set(
        (
            await session.scalars(
                select(RolePermission.permission_id).where(RolePermission.role_id == role.id)
            )
        ).all()
    )
    for permission in permissions:
        if permission.id not in existing_permissions:
            session.add(
                RolePermission(
                    role_id=role.id,
                    permission_id=permission.id,
                    granted_by=user.id,
                )
            )

    totp_secret = pyotp.random_base32()
    recovery_codes = [secrets.token_urlsafe(10) for _ in range(8)]
    session.add(
        AdminMfaAuthenticator(
            authenticator_no=new_prefixed_ulid("mfa_"),
            user_id=user.id,
            authenticator_type="totp",
            display_name="Store management authenticator",
            secret_ciphertext=security.encrypt("admin-mfa-totp", totp_secret),
            recovery_codes_hashes=[
                {
                    "hash": security.keyed_hash("admin-mfa-recovery", code).hex(),
                    "used": False,
                }
                for code in recovery_codes
            ],
            key_version=1,
            authenticator_status="active",
        )
    )
    await session.commit()

    provisioning_uri = pyotp.TOTP(totp_secret).provisioning_uri(
        name=username,
        issuer_name="Ecom AI Merchant",
    )
    return MerchantProvisioningResult(
        user_no=user.user_no,
        username=user.username,
        store_no=store.store_no,
        store_name=store.store_name,
        totp_secret=totp_secret,
        provisioning_uri=provisioning_uri,
        recovery_codes=recovery_codes,
    )


def provisioning_result_json(result: MerchantProvisioningResult) -> str:
    return json.dumps(
        {
            "user_id": result.user_no,
            "username": result.username,
            "store_id": result.store_no,
            "store_name": result.store_name,
            "totp_secret": result.totp_secret,
            "provisioning_uri": result.provisioning_uri,
            "recovery_codes": result.recovery_codes,
            "warning": "Store these values now. They cannot be retrieved later.",
        },
        ensure_ascii=False,
        indent=2,
    )
