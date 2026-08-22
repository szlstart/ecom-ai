from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import SecurityService, canonical_request_hash, utc_now
from app.modules.identity.models import (
    AuthSession,
    User,
    UserStatusRecord,
)
from app.modules.identity.repository import IdentityRepository
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.models import (
    AdminApprovalDecision,
    AdminApprovalEvent,
    AdminApprovalRequest,
    AdminOperationLog,
    AdminSensitiveAccessGrant,
    Permission,
    Role,
    RolePermission,
    UserRole,
    UserRoleEvent,
)
from app.modules.rbac.repository import RbacRepository
from app.modules.rbac.schemas import (
    AdminDashboardSummary,
    AdminUserList,
    AdminUserSummary,
    ApprovalDecisionRequest,
    ApprovalView,
    AuditLogView,
    RoleCreateRequest,
    RoleGrantCreateRequest,
    RoleGrantEventView,
    RoleGrantView,
    RolePermissionsReplaceRequest,
    RoleSummary,
    RoleUpdateRequest,
    SensitiveFields,
    SensitiveGrantCreateRequest,
    SensitiveGrantResult,
    UserStatusChangeRequest,
    UserStatusEventView,
)
from app.modules.system.models import OutboxEvent


class RbacService:
    def __init__(self, session: AsyncSession, security: SecurityService) -> None:
        self.session = session
        self.security = security
        self.repository = RbacRepository(session)
        self.identity = IdentityRepository(session)
        self.idempotency = IdempotencyService(session)

    async def dashboard(self, access: AdminAccess) -> AdminDashboardSummary:
        active_user_count: int | None = None
        if ("platform", 0) in access.scopes:
            active_user_count = int(
                await self.session.scalar(
                    select(func.count(User.id)).where(
                        User.user_status == "active",
                        User.deleted_at.is_(None),
                    )
                )
                or 0
            )
        approval_statement = select(func.count(AdminApprovalRequest.id)).where(
            AdminApprovalRequest.request_status == "pending"
        )
        if ("platform", 0) not in access.scopes:
            approval_statement = approval_statement.where(
                or_(
                    *[
                        and_(
                            AdminApprovalRequest.scope_type == scope_type,
                            AdminApprovalRequest.scope_id == scope_id,
                        )
                        for scope_type, scope_id in access.scopes
                    ]
                )
            )
        pending_approval_count = int(await self.session.scalar(approval_statement) or 0)
        return AdminDashboardSummary(
            generated_at=utc_now(),
            scopes=[
                {"scope_type": scope_type, "scope_id": scope_id}
                for scope_type, scope_id in access.scopes
            ],
            active_user_count=active_user_count,
            pending_approval_count=pending_approval_count,
            unavailable_sections=[
                "commerce",
                "fulfillment",
                "after_sales",
                "support",
                "ai",
            ],
        )

    async def list_users(self, limit: int, cursor: str | None) -> AdminUserList:
        statement = select(User).where(User.deleted_at.is_(None))
        if cursor:
            statement = statement.where(User.user_no > cursor)
        users = list(
            (
                await self.session.scalars(statement.order_by(User.user_no.asc()).limit(limit + 1))
            ).all()
        )
        has_next = len(users) > limit
        items = users[:limit]
        return AdminUserList(
            items=[self._user_view(item) for item in items],
            next_cursor=items[-1].user_no if has_next and items else None,
        )

    async def get_user(self, user_no: str) -> AdminUserSummary:
        return self._user_view(await self._require_user(user_no))

    async def list_user_status_events(self, user_no: str, limit: int) -> list[UserStatusEventView]:
        target = await self._require_user(user_no)
        records = list(
            (
                await self.session.scalars(
                    select(UserStatusRecord)
                    .where(UserStatusRecord.user_id == target.id)
                    .order_by(UserStatusRecord.created_at.desc(), UserStatusRecord.id.desc())
                    .limit(limit)
                )
            ).all()
        )
        return [
            UserStatusEventView(
                status_event_id=item.status_record_no,
                from_status=item.from_status,
                to_status=item.to_status,
                reason_code=item.reason_code,
                reason=item.reason,
                effective_at=item.effective_at,
                expires_at=item.expires_at,
                actor_type=item.actor_type,
            )
            for item in records
        ]

    async def change_user_status(
        self,
        access: AdminAccess,
        user_no: str,
        request: UserStatusChangeRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminUserSummary:
        claim = await self.idempotency.begin(
            scope_key=f"admin:user-status:{user_no}",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("user-status", request.model_dump(mode="json")),
            resource_type="user_status",
        )
        target = await self._require_user(user_no, for_update=True)
        if claim.replayed:
            return self._user_view(target)
        self._check_version(target.version, expected_version)
        desired = "suspended" if request.action == "suspend" else "active"
        if target.user_status == desired:
            return self._user_view(target)
        if request.action == "suspend":
            if target.user_status != "active":
                raise _invalid_transition(target.user_status, desired)
            await self._protect_last_super_admin(target.id)
        elif target.user_status != "suspended":
            raise _invalid_transition(target.user_status, desired)

        now = utc_now()
        record = UserStatusRecord(
            status_record_no=new_prefixed_ulid("usrst_"),
            user_id=target.id,
            from_status=target.user_status,
            to_status=desired,
            reason_code=request.reason_code,
            reason=request.reason,
            effective_at=now,
            expires_at=request.expires_at,
            actor_type="admin",
            actor_user_id=access.context.user.id,
            scope_type="platform",
            scope_id=0,
            expected_user_version=target.version,
            result_user_version=target.version + 1,
            idempotency_key=idempotency_key,
            idempotency_scope_key=self.security.keyed_hash(
                "user-status-idempotency", f"admin:{target.id}:{idempotency_key}"
            ),
            request_id=_request_id(),
            trace_id=_request_id(),
        )
        self.session.add(record)
        await self.session.flush()
        target.user_status = desired
        target.status_reason_code = request.reason_code if desired == "suspended" else None
        target.status_expires_at = request.expires_at if desired == "suspended" else None
        target.current_status_record_id = record.id
        target.version += 1
        if desired == "suspended":
            await self.identity.revoke_user_sessions(target.id, now, "admin_suspended")
        self._add_audit(
            access,
            action="change_status",
            target_type="user",
            target_no=target.user_no,
            reason=request.reason,
            before={"status": record.from_status},
            after={"status": desired},
        )
        self.idempotency.complete(claim, response_status=200, resource_no=record.status_record_no)
        await self.session.commit()
        return self._user_view(target)

    async def revoke_user_sessions(
        self,
        access: AdminAccess,
        user_no: str,
        reason: str,
        idempotency_key: str,
    ) -> None:
        claim = await self.idempotency.begin(
            scope_key=f"admin:user-sessions-revoke:{user_no}",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("revoke-sessions", {"reason": reason}),
            resource_type="session_revocation",
        )
        if claim.replayed:
            return
        target = await self._require_user(user_no)
        await self.identity.revoke_user_sessions(target.id, utc_now(), "admin_forced_logout")
        self._add_audit(
            access,
            action="revoke_sessions",
            target_type="user",
            target_no=user_no,
            reason=reason,
        )
        self.idempotency.complete(claim, response_status=200, resource_no=user_no)
        await self.session.commit()

    async def require_password_reset(
        self,
        access: AdminAccess,
        user_no: str,
        reason: str,
        idempotency_key: str,
    ) -> None:
        claim = await self.idempotency.begin(
            scope_key=f"admin:user-password-reset-requirement:{user_no}",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("password-reset-requirement", {"reason": reason}),
            resource_type="password_reset_requirement",
        )
        if claim.replayed:
            return
        target = await self._require_user(user_no)
        credential = await self.identity.password_credential(target.id, for_update=True)
        if credential is None:
            raise ApplicationError(
                status=409,
                code="USER_PASSWORD_NOT_CONFIGURED",
                title="Password not configured",
                detail="该账号尚未设置密码。",
            )
        credential.must_change_password = True
        credential.credential_version += 1
        await self.identity.revoke_user_sessions(target.id, utc_now(), "password_reset_required")
        self._add_audit(
            access,
            action="require_password_reset",
            target_type="user",
            target_no=user_no,
            reason=reason,
        )
        self.idempotency.complete(claim, response_status=200, resource_no=user_no)
        await self.session.commit()

    async def list_roles(self) -> list[RoleSummary]:
        roles = list(
            (
                await self.session.scalars(
                    select(Role)
                    .where(Role.deleted_at.is_(None))
                    .order_by(Role.scope_type.asc(), Role.role_code.asc())
                )
            ).all()
        )
        return [self._role_view(role) for role in roles]

    async def get_role(self, role_no: str) -> RoleSummary:
        return self._role_view(await self._require_role(role_no))

    async def create_role(
        self,
        access: AdminAccess,
        request: RoleCreateRequest,
        idempotency_key: str,
    ) -> RoleSummary:
        access.require_scope("platform", 0)
        claim = await self.idempotency.begin(
            scope_key=f"admin:role-create:{request.scope_type}",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("role-create", request.model_dump(mode="json")),
            resource_type="role",
        )
        if claim.replayed and claim.record.resource_no:
            return self._role_view(await self._require_role(claim.record.resource_no))
        role = Role(
            role_no=new_prefixed_ulid("rol_"),
            role_code=request.role_code,
            role_name=request.role_name,
            scope_type=request.scope_type,
            role_type="custom",
            description=request.description,
            role_status="active",
        )
        self.session.add(role)
        self._add_audit(
            access,
            action="create_role",
            target_type="role",
            target_no=role.role_no,
            reason=request.description,
        )
        self.idempotency.complete(claim, response_status=201, resource_no=role.role_no)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ApplicationError(
                status=409,
                code="ROLE_CODE_CONFLICT",
                title="Role conflict",
                detail="相同范围下的角色编码已存在。",
            ) from exc
        return self._role_view(role)

    async def update_role(
        self,
        access: AdminAccess,
        role_no: str,
        request: RoleUpdateRequest,
        expected_version: int,
    ) -> RoleSummary:
        access.require_scope("platform", 0)
        role = await self._require_role(role_no, for_update=True)
        self._check_version(role.version, expected_version)
        if role.role_type == "system" and request.status == "disabled":
            raise ApplicationError(
                status=409,
                code="SYSTEM_ROLE_IMMUTABLE",
                title="System role is protected",
                detail="系统角色不能停用。",
            )
        before: dict[str, object] = {
            "name": role.role_name,
            "status": role.role_status,
        }
        if request.role_name is not None:
            role.role_name = request.role_name
        if request.description is not None:
            role.description = request.description
        if request.status is not None:
            role.role_status = request.status
        role.version += 1
        self._add_audit(
            access,
            action="update_role",
            target_type="role",
            target_no=role_no,
            before=before,
            after={"name": role.role_name, "status": role.role_status},
        )
        await self.session.commit()
        return self._role_view(role)

    async def replace_role_permissions(
        self,
        access: AdminAccess,
        role_no: str,
        request: RolePermissionsReplaceRequest,
        expected_version: int,
    ) -> RoleSummary:
        access.require_scope("platform", 0)
        role = await self._require_role(role_no, for_update=True)
        self._check_version(role.version, expected_version)
        if role.role_type != "custom":
            raise ApplicationError(
                status=409,
                code="SYSTEM_ROLE_PERMISSIONS_IMMUTABLE",
                title="System role permissions are protected",
                detail="系统角色权限由发布流程管理，不能在此修改。",
            )
        requested_codes = set(request.permission_codes)
        permissions = list(
            (
                await self.session.scalars(
                    select(Permission).where(Permission.permission_code.in_(requested_codes))
                )
            ).all()
        )
        if {item.permission_code for item in permissions} != requested_codes:
            raise ApplicationError(
                status=422,
                code="UNKNOWN_PERMISSION",
                title="Unknown permission",
                detail="目标权限集合包含未知权限码。",
            )
        if any(role.scope_type not in item.allowed_scope_types for item in permissions):
            raise ApplicationError(
                status=422,
                code="RBAC_PERMISSION_SCOPE_MISMATCH",
                title="Permission scope mismatch",
                detail="权限集合包含不支持目标角色范围的权限。",
            )
        operator_rows = await self.repository.permissions_for_user(
            access.context.user.id, utc_now()
        )
        operator_codes = {item.permission_code for item, _, _ in operator_rows}
        if not requested_codes <= operator_codes or any(
            item.delegation_policy == "non_delegable" for item in permissions
        ):
            raise ApplicationError(
                status=403,
                code="RBAC_PERMISSION_NOT_DELEGABLE",
                title="Permission is not delegable",
                detail="不能授予操作者未拥有或不可委派的权限。",
            )
        await self.session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        self.session.add_all(
            [
                RolePermission(
                    role_id=role.id,
                    permission_id=permission.id,
                    granted_by=access.context.user.id,
                )
                for permission in permissions
            ]
        )
        role.version += 1
        affected_ids = list(
            (
                await self.session.scalars(
                    select(UserRole.user_id).where(
                        UserRole.role_id == role.id,
                        UserRole.grant_status == "active",
                    )
                )
            ).all()
        )
        if affected_ids:
            await self.session.execute(
                update(User)
                .where(User.id.in_(affected_ids))
                .values(permission_version=User.permission_version + 1)
            )
            await self.session.execute(
                update(AuthSession)
                .where(AuthSession.user_id.in_(affected_ids), AuthSession.revoked_at.is_(None))
                .values(revoked_at=utc_now(), revoke_reason="role_permissions_changed")
            )
        self._add_audit(
            access,
            action="replace_role_permissions",
            target_type="role",
            target_no=role_no,
            reason=request.reason,
            after={"permission_codes": sorted(requested_codes)},
        )
        await self.session.commit()
        return self._role_view(role)

    async def list_role_grants(
        self, access: AdminAccess, user_no: str
    ) -> list[RoleGrantView]:
        target = await self._require_user(user_no)
        statement = (
            select(UserRole, Role)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == target.id)
            .order_by(UserRole.granted_at.desc(), UserRole.id.desc())
        )
        if ("platform", 0) not in access.scopes:
            statement = statement.where(
                or_(
                    *[
                        and_(UserRole.scope_type == scope_type, UserRole.scope_id == scope_id)
                        for scope_type, scope_id in access.scopes
                    ]
                )
            )
        rows = (await self.session.execute(statement)).all()
        if ("platform", 0) not in access.scopes and not rows:
            raise _not_found()
        return [self._grant_view(row[0], row[1]) for row in rows]

    async def list_role_grant_events(
        self, access: AdminAccess, user_no: str, limit: int
    ) -> list[RoleGrantEventView]:
        target = await self._require_user(user_no)
        statement = (
            select(UserRoleEvent, UserRole)
                .join(UserRole, UserRole.id == UserRoleEvent.grant_id)
                .where(UserRole.user_id == target.id)
                .order_by(UserRoleEvent.created_at.desc(), UserRoleEvent.id.desc())
                .limit(limit)
        )
        if ("platform", 0) not in access.scopes:
            statement = statement.where(
                or_(
                    *[
                        and_(UserRole.scope_type == scope_type, UserRole.scope_id == scope_id)
                        for scope_type, scope_id in access.scopes
                    ]
                )
            )
        rows = (await self.session.execute(statement)).all()
        if ("platform", 0) not in access.scopes and not rows:
            raise _not_found()
        actor_ids = {event.actor_user_id for event, _ in rows if event.actor_user_id is not None}
        actors = {
            user.id: user.user_no
            for user in (
                await self.session.scalars(select(User).where(User.id.in_(actor_ids)))
            ).all()
        }
        return [
            RoleGrantEventView(
                event_id=event.event_no,
                grant_id=grant.grant_no,
                event_type=event.event_type,
                actor_user_id=actors.get(event.actor_user_id),
                reason=event.reason,
                grant_snapshot=event.grant_snapshot,
                permission_version_after=event.permission_version_after,
                created_at=event.created_at,
            )
            for event, grant in rows
        ]

    async def create_role_grant(
        self,
        access: AdminAccess,
        user_no: str,
        request: RoleGrantCreateRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> RoleGrantView:
        claim = await self.idempotency.begin(
            scope_key=f"admin:role-grant:{user_no}",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("role-grant", request.model_dump(mode="json")),
            resource_type="user_role",
        )
        target = await self._require_user(user_no, for_update=True)
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.grant_by_no(target.id, claim.record.resource_no)
            if existing is not None:
                role = await self.session.get(Role, existing.role_id)
                if role is not None:
                    return self._grant_view(existing, role)
        self._check_version(target.version, expected_version)
        if target.id == access.context.user.id:
            raise ApplicationError(
                status=403,
                code="RBAC_SELF_ESCALATION_FORBIDDEN",
                title="Self escalation forbidden",
                detail="管理员不能为自己授予角色。",
            )
        role = await self._require_role(request.role_id)
        if role.role_status != "active" or role.scope_type != request.scope_type:
            raise ApplicationError(
                status=422,
                code="RBAC_SCOPE_MISMATCH",
                title="Role scope mismatch",
                detail="角色与目标数据范围不匹配。",
            )
        if request.scope_type == "platform" and request.scope_id != 0:
            raise ApplicationError(
                status=422,
                code="RBAC_SCOPE_MISMATCH",
                title="Role scope mismatch",
                detail="平台范围的 scope_id 必须为 0。",
            )
        access.require_scope(request.scope_type, request.scope_id)
        role_permissions = list(
            (
                await self.session.scalars(
                    select(Permission)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .where(RolePermission.role_id == role.id)
                )
            ).all()
        )
        operator_rows = await self.repository.permissions_for_user(
            access.context.user.id, utc_now()
        )
        operator_codes = {
            permission.permission_code
            for permission, grant, _ in operator_rows
            if (grant.scope_type, grant.scope_id) == ("platform", 0)
            or (grant.scope_type, grant.scope_id) == (request.scope_type, request.scope_id)
        }
        if any(item.delegation_policy == "non_delegable" for item in role_permissions) or not {
            item.permission_code for item in role_permissions
        } <= operator_codes:
            raise ApplicationError(
                status=403,
                code="RBAC_ROLE_NOT_DELEGABLE",
                title="Role is not delegable",
                detail="不能授予包含操作者未拥有或不可委派权限的角色。",
            )
        now = utc_now()
        if request.expires_at is not None and request.expires_at <= now:
            raise ApplicationError(
                status=422,
                code="RBAC_GRANT_EXPIRY_INVALID",
                title="Invalid grant expiry",
                detail="授权有效期必须晚于当前时间。",
            )
        grant = UserRole(
            user_id=target.id,
            role_id=role.id,
            grant_no=new_prefixed_ulid("grt_"),
            scope_type=request.scope_type,
            scope_id=request.scope_id,
            grant_status="active",
            active_grant_key=self.security.keyed_hash(
                "active-role-grant",
                f"{target.id}:{role.id}:{request.scope_type}:{request.scope_id}",
            ),
            granted_by=access.context.user.id,
            granted_at=now,
            expires_at=request.expires_at,
            grant_reason=request.reason,
        )
        self.session.add(grant)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ApplicationError(
                status=409,
                code="RBAC_ACTIVE_GRANT_EXISTS",
                title="Active grant already exists",
                detail="相同角色和范围已有有效授权。",
            ) from exc
        target.permission_version += 1
        target.version += 1
        self.session.add(
            UserRoleEvent(
                event_no=new_prefixed_ulid("gre_"),
                grant_id=grant.id,
                event_type="granted",
                actor_user_id=access.context.user.id,
                reason=request.reason,
                grant_snapshot=self._grant_snapshot(grant, role),
                permission_version_after=target.permission_version,
                request_id=_request_id(),
                trace_id=_request_id(),
            )
        )
        self._add_audit(
            access,
            action="grant_role",
            target_type="user_role",
            target_no=grant.grant_no,
            reason=request.reason,
        )
        self.idempotency.complete(claim, response_status=201, resource_no=grant.grant_no)
        await self.session.commit()
        return self._grant_view(grant, role)

    async def revoke_role_grant(
        self,
        access: AdminAccess,
        user_no: str,
        grant_no: str,
        reason: str,
        expected_version: int,
        idempotency_key: str,
    ) -> None:
        claim = await self.idempotency.begin(
            scope_key=f"admin:role-grant-revoke:{grant_no}",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("role-grant-revoke", {"reason": reason}),
            resource_type="user_role_revocation",
        )
        if claim.replayed:
            return
        target = await self._require_user(user_no, for_update=True)
        grant = await self.repository.grant_by_no(target.id, grant_no, for_update=True)
        if grant is None:
            raise _not_found()
        self._check_version(grant.version, expected_version)
        if grant.grant_status != "active":
            return
        role = await self.session.get(Role, grant.role_id)
        if role is None:
            raise _not_found()
        access.require_scope(grant.scope_type, grant.scope_id)
        if role.role_code == "platform_super_admin":
            await self._protect_last_super_admin(target.id)
        now = utc_now()
        grant.grant_status = "revoked"
        grant.active_grant_key = None
        grant.revoked_at = now
        grant.revoked_by = access.context.user.id
        grant.revoke_reason = reason
        grant.version += 1
        target.permission_version += 1
        target.version += 1
        await self.identity.revoke_user_sessions(target.id, now, "role_grant_revoked")
        self.session.add(
            UserRoleEvent(
                event_no=new_prefixed_ulid("gre_"),
                grant_id=grant.id,
                event_type="revoked",
                actor_user_id=access.context.user.id,
                reason=reason,
                grant_snapshot=self._grant_snapshot(grant, role),
                permission_version_after=target.permission_version,
                request_id=_request_id(),
                trace_id=_request_id(),
            )
        )
        self._add_audit(
            access,
            action="revoke_role",
            target_type="user_role",
            target_no=grant_no,
            reason=reason,
        )
        self.idempotency.complete(claim, response_status=200, resource_no=grant_no)
        await self.session.commit()

    async def create_sensitive_grant(
        self,
        access: AdminAccess,
        user_no: str,
        request: SensitiveGrantCreateRequest,
        idempotency_key: str,
    ) -> SensitiveGrantResult:
        claim = await self.idempotency.begin(
            scope_key=f"admin:sensitive-grant:{access.context.user.user_no}:{user_no}",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("sensitive-grant", request.model_dump(mode="json")),
            resource_type="sensitive_access_grant",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.session.scalar(
                select(AdminSensitiveAccessGrant).where(
                    AdminSensitiveAccessGrant.grant_no == claim.record.resource_no
                )
            )
            if existing is not None:
                return SensitiveGrantResult(
                    grant_id=existing.grant_no,
                    expires_at=existing.expires_at,
                    version=existing.version,
                )
        target = await self._require_user(user_no)
        now = utc_now()
        grant = AdminSensitiveAccessGrant(
            grant_no=new_prefixed_ulid("sag_"),
            admin_user_id=access.context.user.id,
            auth_session_id=access.context.session.id,
            target_type="user",
            target_no=target.user_no,
            field_set=sorted(set(request.fields)),
            purpose_code=request.purpose_code,
            reason=request.reason,
            grant_status="active",
            assurance_level=access.context.session.assurance_level,
            authenticated_at=access.context.session.authenticated_at,
            expires_at=now + timedelta(seconds=request.ttl_seconds),
            request_id=_request_id(),
            trace_id=_request_id(),
        )
        self.session.add(grant)
        self._add_audit(
            access,
            action="create_sensitive_grant",
            target_type="user",
            target_no=user_no,
            reason=request.reason,
            after={"fields": grant.field_set, "purpose_code": request.purpose_code},
        )
        self.idempotency.complete(claim, response_status=201, resource_no=grant.grant_no)
        await self.session.commit()
        return SensitiveGrantResult(
            grant_id=grant.grant_no,
            expires_at=grant.expires_at,
            version=grant.version,
        )

    async def revoke_sensitive_grant(
        self,
        access: AdminAccess,
        grant_no: str,
        reason: str,
        expected_version: int,
        idempotency_key: str,
    ) -> int:
        claim = await self.idempotency.begin(
            scope_key=f"admin:sensitive-grant-revoke:{grant_no}",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("sensitive-grant-revoke", {"reason": reason}),
            resource_type="sensitive_access_grant_revocation",
        )
        grant = await self.session.scalar(
            select(AdminSensitiveAccessGrant)
            .where(AdminSensitiveAccessGrant.grant_no == grant_no)
            .with_for_update()
        )
        if grant is None:
            raise _not_found()
        if claim.replayed:
            return grant.version
        self._check_version(grant.version, expected_version)
        access.require_scope("platform", 0)
        if grant.admin_user_id != access.context.user.id:
            rows = await self.repository.permissions_for_user(
                access.context.user.id, utc_now()
            )
            if "users:manage" not in {permission.permission_code for permission, _, _ in rows}:
                raise ApplicationError(
                    status=404,
                    code="RESOURCE_NOT_FOUND",
                    title="Resource not found",
                    detail="未找到该资源。",
                )
        if grant.grant_status == "active" and grant.revoked_at is None:
            grant.grant_status = "revoked"
            grant.revoked_at = utc_now()
            grant.revoked_by = access.context.user.id
            grant.revoke_reason = reason
            grant.version += 1
            self._add_audit(
                access,
                action="revoke_sensitive_grant",
                target_type="sensitive_access_grant",
                target_no=grant_no,
                reason=reason,
            )
        self.idempotency.complete(claim, response_status=200, resource_no=grant_no)
        await self.session.commit()
        return grant.version

    async def consume_sensitive_grant(
        self,
        access: AdminAccess,
        user_no: str,
        grant_no: str,
    ) -> SensitiveFields:
        target = await self._require_user(user_no)
        grant = await self.session.scalar(
            select(AdminSensitiveAccessGrant)
            .where(AdminSensitiveAccessGrant.grant_no == grant_no)
            .with_for_update()
        )
        now = utc_now()
        if (
            grant is None
            or grant.admin_user_id != access.context.user.id
            or grant.auth_session_id != access.context.session.id
            or grant.target_type != "user"
            or grant.target_no != user_no
            or grant.grant_status != "active"
            or grant.consumed_at is not None
            or grant.revoked_at is not None
            or grant.expires_at <= now
        ):
            raise ApplicationError(
                status=403,
                code="SENSITIVE_ACCESS_GRANT_INVALID",
                title="Sensitive access grant invalid",
                detail="敏感字段访问凭据无效或已过期。",
            )
        credentials = await self.identity.credentials_for_user(target.id)
        values: dict[str, str] = {}
        for credential in credentials:
            if (
                credential.credential_type in grant.field_set
                and credential.identifier_ciphertext is not None
            ):
                values[credential.credential_type] = self.security.decrypt(
                    f"user-credential:{credential.credential_type}",
                    credential.identifier_ciphertext,
                )
        grant.consumed_at = now
        grant.grant_status = "consumed"
        grant.version += 1
        watermark = f"{access.context.user.user_no} · {_request_id()}"
        self._add_audit(
            access,
            action="read_sensitive_fields",
            target_type="user",
            target_no=user_no,
            reason=grant.reason,
            after={"fields": sorted(values)},
        )
        await self.session.commit()
        return SensitiveFields(user_id=user_no, values=values, watermark=watermark)

    async def list_approvals(self, access: AdminAccess, limit: int = 50) -> list[ApprovalView]:
        statement = select(AdminApprovalRequest)
        if ("platform", 0) not in access.scopes:
            statement = statement.where(
                or_(
                    *[
                        and_(
                            AdminApprovalRequest.scope_type == scope_type,
                            AdminApprovalRequest.scope_id == scope_id,
                        )
                        for scope_type, scope_id in access.scopes
                    ]
                )
            )
        items = list(
            (
                await self.session.scalars(
                    statement.order_by(
                        AdminApprovalRequest.created_at.desc(),
                        AdminApprovalRequest.id.desc(),
                    )
                    .limit(limit)
                )
            ).all()
        )
        return [self._approval_view(item) for item in items]

    async def get_approval(self, access: AdminAccess, approval_no: str) -> ApprovalView:
        item = await self.repository.approval_by_no(approval_no)
        if item is None:
            raise _not_found()
        access.require_scope(item.scope_type, item.scope_id)
        return self._approval_view(item)

    async def decide_approval(
        self,
        access: AdminAccess,
        approval_no: str,
        request: ApprovalDecisionRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> ApprovalView:
        claim = await self.idempotency.begin(
            scope_key=f"admin:approval-decision:{approval_no}:{access.context.user.user_no}",
            idempotency_key=idempotency_key,
            payload=self._idempotency_payload("approval-decision", request.model_dump(mode="json")),
            resource_type="admin_approval_decision",
        )
        item = await self.repository.approval_by_no(approval_no, for_update=True)
        if item is None:
            raise _not_found()
        if claim.replayed:
            return self._approval_view(item)
        self._check_version(item.version, expected_version)
        now = utc_now()
        if item.request_status != "pending" or item.expires_at <= now:
            raise ApplicationError(
                status=409,
                code="APPROVAL_NOT_PENDING",
                title="Approval is not pending",
                detail="该审批申请当前不能再作出决定。",
            )
        if item.initiator_user_id == access.context.user.id:
            raise ApplicationError(
                status=403,
                code="APPROVAL_SELF_DECISION_FORBIDDEN",
                title="Self approval forbidden",
                detail="审批发起人不能审批自己的申请。",
            )
        access.require_scope(item.scope_type, item.scope_id)
        permissions = await self.repository.permissions_for_user(access.context.user.id, now)
        if item.required_permission_code not in {
            permission.permission_code for permission, _, _ in permissions
        }:
            raise ApplicationError(
                status=403,
                code="APPROVAL_DOMAIN_PERMISSION_DENIED",
                title="Domain permission denied",
                detail="当前审批人缺少原业务动作所需权限。",
            )
        existing = await self.repository.approval_decisions(item.id)
        if any(decision.approver_user_id == access.context.user.id for decision in existing):
            raise ApplicationError(
                status=409,
                code="APPROVAL_DUPLICATE_DECISION",
                title="Duplicate approval decision",
                detail="同一管理员不能重复占用审批席位。",
            )
        previous_status = item.request_status
        decision = AdminApprovalDecision(
            decision_no=new_prefixed_ulid("aad_"),
            approval_request_id=item.id,
            approver_user_id=access.context.user.id,
            decision=request.decision,
            reason_code=request.reason_code,
            reason=request.reason,
            permission_code_snapshot=item.required_permission_code,
            scope_snapshot={"scope_type": item.scope_type, "scope_id": item.scope_id},
            assurance_level=access.context.session.assurance_level,
            authenticated_at=access.context.session.authenticated_at,
            decision_hash=self.security.keyed_hash(
                "admin-approval-decision",
                f"{item.approval_request_no}:{access.context.user.user_no}:"
                f"{request.decision}:{request.reason_code}:{request.reason}",
            ),
            decided_at=now,
            request_id=_request_id(),
            trace_id=_request_id(),
        )
        self.session.add(decision)
        if request.decision == "reject":
            item.request_status = "rejected"
            item.completed_at = now
        else:
            item.approved_count += 1
            if item.approved_count >= item.required_approval_count:
                item.request_status = "approved"
                item.approved_at = now
                item.execution_no = new_prefixed_ulid("aex_")
                self.session.add(
                    OutboxEvent(
                        event_no=new_prefixed_ulid("evt_"),
                        event_type="rbac.admin_approval_ready.v1",
                        aggregate_type="admin_approval",
                        aggregate_no=item.approval_request_no,
                        aggregate_version=item.version + 1,
                        payload={
                            "approval_request_id": item.approval_request_no,
                            "execution_id": item.execution_no,
                        },
                        event_status="pending",
                        available_at=now,
                        trace_id=_request_id(),
                    )
                )
        item.version += 1
        self.session.add(
            AdminApprovalEvent(
                event_no=new_prefixed_ulid("aae_"),
                approval_request_id=item.id,
                event_type="decision_recorded",
                from_status=previous_status,
                to_status=item.request_status,
                actor_type="admin",
                actor_id=access.context.user.id,
                snapshot_redacted={
                    "decision": request.decision,
                    "approved_count": item.approved_count,
                },
                request_version=item.version,
                request_id=_request_id(),
                trace_id=_request_id(),
            )
        )
        self._add_audit(
            access,
            action="decide_approval",
            target_type="admin_approval",
            target_no=approval_no,
            reason=request.reason,
            after={"decision": request.decision, "status": item.request_status},
        )
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=decision.decision_no,
        )
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ApplicationError(
                status=409,
                code="APPROVAL_DUPLICATE_DECISION",
                title="Duplicate approval decision",
                detail="同一管理员不能重复占用审批席位。",
            ) from exc
        return self._approval_view(item)

    async def list_audit_logs(
        self, access: AdminAccess, limit: int = 50
    ) -> list[AuditLogView]:
        logs = await self.repository.admin_logs(access.scopes, limit)
        user_ids = {item.operator_user_id for item in logs}
        users = {
            user.id: user.user_no
            for user in (
                await self.session.scalars(select(User).where(User.id.in_(user_ids)))
            ).all()
        }
        result = [
            AuditLogView(
                operation_id=item.operation_no,
                operator_user_id=users.get(item.operator_user_id, "unknown"),
                permission_code=item.permission_code,
                action=item.action,
                target_type=item.target_type,
                target_id=item.target_no,
                result_status=item.result_status,
                reason=item.reason,
                created_at=item.created_at,
            )
            for item in logs
        ]
        self._add_audit(
            access,
            action="read_audit_logs",
            target_type="audit_log_collection",
            target_no="current_scope",
        )
        await self.session.commit()
        return result

    async def _require_user(self, user_no: str, *, for_update: bool = False) -> User:
        item = await self.repository.user_by_no(user_no, for_update=for_update)
        if item is None:
            raise _not_found()
        return item

    async def _require_role(self, role_no: str, *, for_update: bool = False) -> Role:
        role = await self.repository.role_by_no(role_no, for_update=for_update)
        if role is None:
            raise _not_found()
        return role

    async def _protect_last_super_admin(self, user_id: int) -> None:
        target_is_super = await self.session.scalar(
            select(func.count(UserRole.id))
            .join(Role, Role.id == UserRole.role_id)
            .where(
                UserRole.user_id == user_id,
                UserRole.grant_status == "active",
                Role.role_code == "platform_super_admin",
            )
        )
        if not target_is_super:
            return
        active_count = await self.session.scalar(
            select(func.count(func.distinct(UserRole.user_id)))
            .join(Role, Role.id == UserRole.role_id)
            .join(User, User.id == UserRole.user_id)
            .where(
                UserRole.grant_status == "active",
                Role.role_code == "platform_super_admin",
                User.user_status == "active",
            )
        )
        if int(active_count or 0) <= 1:
            raise ApplicationError(
                status=409,
                code="RBAC_LAST_SECURITY_ADMIN_PROTECTED",
                title="Last security administrator protected",
                detail="不能停用或撤销最后一名可恢复系统的安全管理员。",
            )

    def _add_audit(
        self,
        access: AdminAccess,
        *,
        action: str,
        target_type: str,
        target_no: str,
        reason: str | None = None,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        self.session.add(
            AdminOperationLog(
                operation_no=new_prefixed_ulid("aol_"),
                operator_user_id=access.context.user.id,
                scope_type="platform" if ("platform", 0) in access.scopes else access.scopes[0][0],
                scope_id=0 if ("platform", 0) in access.scopes else access.scopes[0][1],
                permission_code=access.permission.permission_code,
                action=action,
                target_type=target_type,
                target_no=target_no,
                before_snapshot=before,
                after_snapshot=after,
                result_status="succeeded",
                reason=reason,
                request_id=_request_id(),
                trace_id=_request_id(),
                ip_hash=None,
            )
        )

    def _idempotency_payload(self, purpose: str, value: object) -> dict[str, str]:
        return {
            "fingerprint": self.security.keyed_hash(
                f"idempotency:{purpose}",
                canonical_request_hash(value),
            ).hex()
        }

    @staticmethod
    def _user_view(user: User) -> AdminUserSummary:
        return AdminUserSummary(
            user_id=user.user_no,
            username=user.username,
            nickname=user.nickname,
            account_status=user.user_status,
            registered_at=user.registered_at,
            last_login_at=user.last_login_at,
            permission_version=user.permission_version,
            version=user.version,
        )

    @staticmethod
    def _role_view(role: Role) -> RoleSummary:
        return RoleSummary(
            role_id=role.role_no,
            role_code=role.role_code,
            role_name=role.role_name,
            scope_type=role.scope_type,
            role_type=role.role_type,
            description=role.description,
            status=role.role_status,
            version=role.version,
        )

    @staticmethod
    def _grant_view(grant: UserRole, role: Role) -> RoleGrantView:
        return RoleGrantView(
            grant_id=grant.grant_no,
            role_id=role.role_no,
            role_name=role.role_name,
            scope_type=grant.scope_type,
            scope_id=grant.scope_id,
            status=grant.grant_status,
            granted_at=grant.granted_at,
            expires_at=grant.expires_at,
            revoked_at=grant.revoked_at,
            reason=grant.grant_reason,
            version=grant.version,
        )

    @staticmethod
    def _grant_snapshot(grant: UserRole, role: Role) -> dict[str, object]:
        return {
            "grant_id": grant.grant_no,
            "role_id": role.role_no,
            "scope_type": grant.scope_type,
            "scope_id": grant.scope_id,
            "status": grant.grant_status,
        }

    @staticmethod
    def _approval_view(item: AdminApprovalRequest) -> ApprovalView:
        return ApprovalView(
            approval_request_id=item.approval_request_no,
            approval_type=item.approval_type,
            action_code=item.action_code,
            target_type=item.target_type,
            target_id=item.target_no,
            display_snapshot=item.display_snapshot,
            resource_versions=item.resource_versions,
            required_approval_count=item.required_approval_count,
            approved_count=item.approved_count,
            status=item.request_status,
            expires_at=item.expires_at,
            version=item.version,
        )

    @staticmethod
    def _check_version(actual: int, expected: int) -> None:
        if actual != expected:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_MISMATCH",
                title="Resource version mismatch",
                detail="资源已被其他操作修改，请刷新后重试。",
            )


def _request_id() -> str:
    return request_id_context.get() or "req_internal000000000000000000"


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="未找到该资源。",
    )


def _invalid_transition(current: str, desired: str) -> ApplicationError:
    return ApplicationError(
        status=409,
        code="USER_STATUS_TRANSITION_INVALID",
        title="Invalid user status transition",
        detail=f"账号不能从 {current} 转换为 {desired}。",
    )
