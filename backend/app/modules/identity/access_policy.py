from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utc_now
from app.modules.rbac.models import Role, UserRole

IdentityGrant = tuple[str, str, int]


@dataclass(frozen=True)
class IdentityEligibility:
    consumer: bool
    merchant: bool
    platform_admin: bool

    def allows_session(self, audience: Literal["user", "admin"], client_type: str) -> bool:
        if audience == "user":
            return client_type == "web" and self.consumer
        if client_type == "merchant":
            return self.merchant
        if client_type in {"admin", "admin_password"}:
            return self.platform_admin
        return False


def classify_identity_grants(grants: Iterable[IdentityGrant]) -> IdentityEligibility:
    normalized = tuple(grants)
    has_any_consumer_role = any(role_code == "user" for role_code, _, _ in normalized)
    has_consumer_role = any(
        role_code == "user" and scope_type == "platform" and scope_id == 0
        for role_code, scope_type, scope_id in normalized
    )
    has_store_identity = any(
        role_code != "user" and scope_type == "store"
        for role_code, scope_type, _scope_id in normalized
    )
    has_store_operator = any(
        role_code == "store_operator" and scope_type == "store" and scope_id > 0
        for role_code, scope_type, scope_id in normalized
    )
    has_platform_admin = any(
        role_code != "user" and scope_type == "platform" and scope_id == 0
        for role_code, scope_type, scope_id in normalized
    )
    has_platform_identity = any(
        role_code != "user" and scope_type == "platform"
        for role_code, scope_type, _scope_id in normalized
    )
    has_non_consumer_role = any(role_code != "user" for role_code, _, _ in normalized)
    has_malformed_identity_grant = any(
        (role_code == "user" and (scope_type != "platform" or scope_id != 0))
        or (role_code == "store_operator" and (scope_type != "store" or scope_id <= 0))
        or (role_code != "user" and scope_type == "platform" and scope_id != 0)
        for role_code, scope_type, scope_id in normalized
    )

    return IdentityEligibility(
        consumer=(
            has_consumer_role
            and not has_non_consumer_role
            and not has_malformed_identity_grant
        ),
        merchant=(
            has_store_operator
            and not has_any_consumer_role
            and not has_platform_identity
            and not has_malformed_identity_grant
        ),
        platform_admin=(
            has_platform_admin
            and not has_any_consumer_role
            and not has_store_identity
            and not has_malformed_identity_grant
        ),
    )


async def load_identity_eligibility(
    session: AsyncSession,
    user_id: int,
    now: datetime | None = None,
) -> IdentityEligibility:
    effective_at = now or utc_now()
    rows = (
        await session.execute(
            select(Role.role_code, UserRole.scope_type, UserRole.scope_id)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(
                UserRole.user_id == user_id,
                UserRole.grant_status == "active",
                or_(UserRole.expires_at.is_(None), UserRole.expires_at > effective_at),
                Role.role_status == "active",
                Role.deleted_at.is_(None),
            )
        )
    ).all()
    return classify_identity_grants(
        (str(role_code), str(scope_type), int(scope_id))
        for role_code, scope_type, scope_id in rows
    )
