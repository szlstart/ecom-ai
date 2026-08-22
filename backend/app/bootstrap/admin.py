from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

import pyotp
from sqlalchemy import select
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


@dataclass(frozen=True)
class AdminProvisioningResult:
    user_no: str
    username: str
    totp_secret: str
    provisioning_uri: str
    recovery_codes: list[str]


async def provision_platform_super_admin(
    session: AsyncSession,
    security: SecurityService,
    *,
    username: str,
    password: str,
) -> AdminProvisioningResult:
    normalized_username = normalize_username(username)
    existing = await session.scalar(
        select(User.id).where(User.username_normalized == normalized_username)
    )
    if existing is not None:
        raise ValueError("username is already in use")
    role = await session.scalar(
        select(Role).where(
            Role.role_code == "platform_super_admin",
            Role.scope_type == "platform",
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
    grant_key = security.keyed_hash("active-role-grant", f"{user.id}:{role.id}:platform:0")
    session.add(
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
            grant_reason="initial_platform_super_admin_provisioning",
        )
    )

    existing_permissions = set(
        (
            await session.scalars(
                select(RolePermission.permission_id).where(RolePermission.role_id == role.id)
            )
        ).all()
    )
    permissions = list(
        (
            await session.scalars(
                select(Permission).where(Permission.permission_status == "active")
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
            display_name="Primary authenticator",
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
        issuer_name="Ecom AI Admin",
    )
    return AdminProvisioningResult(
        user_no=user.user_no,
        username=user.username,
        totp_secret=totp_secret,
        provisioning_uri=provisioning_uri,
        recovery_codes=recovery_codes,
    )


def provisioning_result_json(result: AdminProvisioningResult) -> str:
    return json.dumps(
        {
            "user_id": result.user_no,
            "username": result.username,
            "totp_secret": result.totp_secret,
            "provisioning_uri": result.provisioning_uri,
            "recovery_codes": result.recovery_codes,
            "warning": "Store these values now. They cannot be retrieved later.",
        },
        ensure_ascii=False,
        indent=2,
    )
