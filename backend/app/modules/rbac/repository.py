from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from app.modules.rbac.models import (
    AdminApprovalDecision,
    AdminApprovalRequest,
    AdminMfaAuthenticator,
    AdminOperationLog,
    Permission,
    Role,
    RolePermission,
    UserRole,
)


class RbacRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def active_grants_statement(self, user_id: int, now: datetime) -> Select[tuple[UserRole, Role]]:
        return (
            select(UserRole, Role)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                UserRole.user_id == user_id,
                UserRole.grant_status == "active",
                or_(UserRole.expires_at.is_(None), UserRole.expires_at > now),
                Role.role_status == "active",
                Role.deleted_at.is_(None),
            )
        )

    async def active_grants(self, user_id: int, now: datetime) -> list[tuple[UserRole, Role]]:
        rows = await self.session.execute(self.active_grants_statement(user_id, now))
        return [(row[0], row[1]) for row in rows.all()]

    async def permissions_for_user(
        self, user_id: int, now: datetime
    ) -> list[tuple[Permission, UserRole, Role]]:
        rows = await self.session.execute(
            select(Permission, UserRole, Role)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(
                UserRole.user_id == user_id,
                UserRole.grant_status == "active",
                or_(UserRole.expires_at.is_(None), UserRole.expires_at > now),
                Role.role_status == "active",
                Role.deleted_at.is_(None),
                Permission.permission_status == "active",
            )
        )
        return [(row[0], row[1], row[2]) for row in rows.all()]

    async def active_admin_mfa(
        self, user_id: int, *, for_update: bool = False
    ) -> AdminMfaAuthenticator | None:
        statement = select(AdminMfaAuthenticator).where(
            AdminMfaAuthenticator.user_id == user_id,
            AdminMfaAuthenticator.authenticator_status == "active",
            AdminMfaAuthenticator.revoked_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(AdminMfaAuthenticator | None, await self.session.scalar(statement))

    async def role_by_no(self, role_no: str, *, for_update: bool = False) -> Role | None:
        statement = select(Role).where(Role.role_no == role_no, Role.deleted_at.is_(None))
        if for_update:
            statement = statement.with_for_update()
        return cast(Role | None, await self.session.scalar(statement))

    async def grant_by_no(
        self, user_id: int, grant_no: str, *, for_update: bool = False
    ) -> UserRole | None:
        statement = select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.grant_no == grant_no,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(UserRole | None, await self.session.scalar(statement))

    async def approval_by_no(
        self, approval_no: str, *, for_update: bool = False
    ) -> AdminApprovalRequest | None:
        statement = select(AdminApprovalRequest).where(
            AdminApprovalRequest.approval_request_no == approval_no
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(AdminApprovalRequest | None, await self.session.scalar(statement))

    async def approval_decisions(self, approval_request_id: int) -> list[AdminApprovalDecision]:
        return list(
            (
                await self.session.scalars(
                    select(AdminApprovalDecision)
                    .where(AdminApprovalDecision.approval_request_id == approval_request_id)
                    .order_by(AdminApprovalDecision.decided_at.asc())
                )
            ).all()
        )

    async def admin_logs(
        self, scopes: tuple[tuple[str, int], ...], limit: int = 50
    ) -> list[AdminOperationLog]:
        statement = select(AdminOperationLog)
        if ("platform", 0) not in scopes:
            statement = statement.where(
                or_(
                    *[
                        and_(
                            AdminOperationLog.scope_type == scope_type,
                            AdminOperationLog.scope_id == scope_id,
                        )
                        for scope_type, scope_id in scopes
                    ]
                )
            )
        return list(
            (
                await self.session.scalars(
                    statement.order_by(
                        AdminOperationLog.created_at.desc(), AdminOperationLog.id.desc()
                    ).limit(limit)
                )
            ).all()
        )

    async def user_by_no(self, user_no: str, *, for_update: bool = False) -> User | None:
        statement = select(User).where(User.user_no == user_no, User.deleted_at.is_(None))
        if for_update:
            statement = statement.with_for_update()
        return cast(User | None, await self.session.scalar(statement))
