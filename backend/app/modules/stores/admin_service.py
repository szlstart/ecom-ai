from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.pagination import CursorCodec
from app.core.security import utc_now
from app.modules.files.models import FileObject
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.stores.admin_repository import AdminStoreRepository
from app.modules.stores.admin_schemas import (
    AdminCertificationDecisionRequest,
    AdminCertificationDetail,
    AdminCertificationEventView,
    AdminCertificationList,
    AdminCertificationMaterialRequest,
    AdminCertificationSummary,
    AdminPolicyCommandRequest,
    AdminStoreList,
    AdminStorePolicyCreateRequest,
    AdminStorePolicyUpdateRequest,
    AdminStorePolicyView,
    AdminStoreStatusChangeRequest,
    AdminStoreUpdateRequest,
    AdminStoreView,
)
from app.modules.stores.models import (
    Store,
    StoreCertification,
    StoreCertificationEvent,
    StoreServicePolicy,
)
from app.modules.system.models import OutboxEvent


class AdminStoreService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.repository = AdminStoreRepository(session)
        self.idempotency = IdempotencyService(session)
        self.cursor = CursorCodec(settings.security_hmac_secret.get_secret_value())

    async def list_stores(
        self,
        access: AdminAccess,
        *,
        status: str | None,
        q: str | None,
        cursor: str | None,
        limit: int,
    ) -> AdminStoreList:
        filter_key = _cursor_filter("admin-stores", status=status, q=q)
        cursor_value = _cursor_value(self.cursor, cursor, filter_key)
        rows = await self.repository.stores(
            access.scopes,
            status=status,
            q=q.strip() if q else None,
            cursor=cursor_value,
            limit=limit,
        )
        has_more = len(rows) > limit
        visible = rows[:limit]
        logos = await self.repository.files_by_object_keys(
            [store.logo_object_key for store, _ in visible if store.logo_object_key]
        )
        return AdminStoreList(
            items=[
                _store_view(store, owner.user_no, logos.get(store.logo_object_key or ""))
                for store, owner in visible
            ],
            next_cursor=(
                self.cursor.encode(
                    filter_key=filter_key,
                    values=(visible[-1][0].store_no,),
                )
                if visible and has_more
                else None
            ),
        )

    async def get_store(self, access: AdminAccess, store_no: str) -> AdminStoreView:
        row = await self.repository.store_by_no(store_no)
        if row is None:
            raise _not_found()
        store, owner = row
        access.require_scope("store", store.id)
        logo = (
            await self.repository.file_by_object_key(store.logo_object_key)
            if store.logo_object_key
            else None
        )
        return _store_view(store, owner.user_no, logo)

    async def update_store(
        self,
        access: AdminAccess,
        store_no: str,
        payload: AdminStoreUpdateRequest,
        expected_version: int,
    ) -> AdminStoreView:
        row = await self.repository.store_by_no(store_no, for_update=True)
        if row is None:
            raise _not_found()
        store, owner = row
        access.require_scope("store", store.id)
        _check_version(store.version, expected_version)
        before: dict[str, object] = {
            "store_name": store.store_name,
            "store_name_changed_at": (
                store.store_name_changed_at.isoformat()
                if store.store_name_changed_at is not None
                else None
            ),
            "description": store.description,
            "logo_object_key": store.logo_object_key,
            "version": store.version,
        }
        logo: FileObject | None = None
        if payload.store_name is not None and payload.store_name != store.store_name:
            normalized_name = _normalize_name(payload.store_name)
            if await self.repository.store_name_exists(
                normalized_name, exclude_store_id=store.id
            ):
                raise _conflict("STORE_NAME_ALREADY_EXISTS", "该店铺名称已被其他店铺使用。")
            now = utc_now()
            available_at = (
                store.store_name_changed_at + timedelta(days=7)
                if store.store_name_changed_at is not None
                else None
            )
            if (
                ("platform", 0) not in access.scopes
                and available_at is not None
                and now < available_at
            ):
                raise ApplicationError(
                    status=409,
                    code="STORE_NAME_CHANGE_COOLDOWN",
                    title="Store name change is temporarily unavailable",
                    detail=(
                        "店铺名称每 7 天只能修改一次，下次可修改时间为 "
                        f"{available_at.isoformat(timespec='seconds')}。"
                    ),
                    errors=[
                        {
                            "pointer": "/store_name",
                            "code": "STORE_NAME_CHANGE_COOLDOWN",
                            "message": "店铺名称尚在 7 天修改冷却期内。",
                        }
                    ],
                )
            store.store_name = payload.store_name
            store.store_name_normalized = normalized_name
            store.store_name_changed_at = now
        if "description" in payload.model_fields_set:
            store.description = payload.description
        if "logo_file_id" in payload.model_fields_set:
            if payload.logo_file_id is None:
                store.logo_object_key = None
            else:
                logo = await self.repository.file_by_no(payload.logo_file_id)
                if (
                    logo is None
                    or logo.purpose != "store_logo"
                    or logo.owner_type != "store"
                    or logo.owner_no != store.store_no
                    or logo.file_status != "active"
                    or logo.scan_status != "safe"
                    or logo.visibility != "public_derivative"
                ):
                    raise ApplicationError(
                        status=422,
                        code="FILE_NOT_BINDABLE",
                        title="File cannot be bound",
                        detail="店铺 Logo 文件不存在、尚未完成安全处理或不属于当前店铺。",
                    )
                store.logo_object_key = logo.object_key
        store.version += 1
        _add_outbox(
            self.session,
            event_type="store.profile_updated.v1",
            aggregate_type="store",
            aggregate_no=store.store_no,
            aggregate_version=store.version,
            payload={"store_id": store.store_no},
        )
        record_admin_operation(
            self.session,
            access,
            action="update_store",
            target_type="store",
            target_no=store.store_no,
            before=before,
            after={
                "store_name": store.store_name,
                "store_name_changed_at": (
                    store.store_name_changed_at.isoformat()
                    if store.store_name_changed_at is not None
                    else None
                ),
                "description": store.description,
                "logo_object_key": store.logo_object_key,
                "version": store.version,
            },
            scope_type="store",
            scope_id=store.id,
        )
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _conflict("STORE_NAME_ALREADY_EXISTS", "店铺名称已被使用。") from exc
        if logo is None and store.logo_object_key:
            logo = await self.repository.file_by_object_key(store.logo_object_key)
        return _store_view(store, owner.user_no, logo)

    async def change_store_status(
        self,
        access: AdminAccess,
        store_no: str,
        payload: AdminStoreStatusChangeRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminStoreView:
        claim = await self.idempotency.begin(
            scope_key=f"admin:store-status:{store_no}:{access.context.user.user_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="store",
        )
        row = await self.repository.store_by_no(store_no, for_update=True)
        if row is None:
            raise _not_found()
        store, owner = row
        access.require_scope("store", store.id)
        if claim.replayed:
            logo = (
                await self.repository.file_by_object_key(store.logo_object_key)
                if store.logo_object_key
                else None
            )
            return _store_view(store, owner.user_no, logo)
        _check_version(store.version, expected_version)
        transitions = {
            "activate": ({"pending"}, "active", "store.activated.v1"),
            "suspend": ({"active"}, "suspended", "store.suspended.v1"),
            "resume": ({"suspended"}, "active", "store.resumed.v1"),
            "close": ({"pending", "active", "suspended"}, "closed", "store.closed.v1"),
        }
        allowed_from, next_status, event_type = transitions[payload.action]
        if store.store_status not in allowed_from:
            raise _invalid_transition(store.store_status, payload.action)
        now = utc_now()
        if payload.action == "activate" and not await self.repository.approved_certification_exists(
            store.id, now
        ):
            raise ApplicationError(
                status=409,
                code="STORE_CERTIFICATION_REQUIRED",
                title="Store certification required",
                detail="店铺至少需要一项当前有效的已通过认证才能启用。",
            )
        before_status = store.store_status
        store.store_status = next_status
        if payload.action == "activate" and store.opened_at is None:
            store.opened_at = now
        store.suspended_at = now if payload.action == "suspend" else None
        store.closed_at = now if payload.action == "close" else store.closed_at
        store.version += 1
        _add_outbox(
            self.session,
            event_type=event_type,
            aggregate_type="store",
            aggregate_no=store.store_no,
            aggregate_version=store.version,
            payload={
                "store_id": store.store_no,
                "from_status": before_status,
                "to_status": next_status,
                "reason_code": payload.reason_code,
            },
        )
        record_admin_operation(
            self.session,
            access,
            action=f"{payload.action}_store",
            target_type="store",
            target_no=store.store_no,
            reason=payload.reason,
            before={"status": before_status, "version": store.version - 1},
            after={"status": next_status, "version": store.version},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(claim, response_status=200, resource_no=store.store_no)
        logo = (
            await self.repository.file_by_object_key(store.logo_object_key)
            if store.logo_object_key
            else None
        )
        result = _store_view(store, owner.user_no, logo)
        await self.session.commit()
        return result

    async def list_certifications(
        self,
        access: AdminAccess,
        *,
        review_status: str | None,
        cursor: str | None,
        limit: int,
    ) -> AdminCertificationList:
        filter_key = _cursor_filter(
            "admin-store-certifications",
            review_status=review_status,
        )
        cursor_value = _cursor_value(self.cursor, cursor, filter_key)
        rows = await self.repository.certifications(
            access.scopes,
            review_status=review_status,
            cursor=cursor_value,
            limit=limit,
        )
        has_more = len(rows) > limit
        visible = rows[:limit]
        return AdminCertificationList(
            items=[
                _certification_summary(certification, store) for certification, store in visible
            ],
            next_cursor=(
                self.cursor.encode(
                    filter_key=filter_key,
                    values=(visible[-1][0].certification_no,),
                )
                if visible and has_more
                else None
            ),
        )

    async def get_certification(
        self, access: AdminAccess, certification_no: str
    ) -> AdminCertificationDetail:
        row = await self.repository.certification_by_no(certification_no)
        if row is None:
            raise _not_found()
        certification, store = row
        access.require_scope("store", store.id)
        detail = await self._certification_detail(certification, store)
        record_admin_operation(
            self.session,
            access,
            action="read_store_certification",
            target_type="store_certification",
            target_no=certification.certification_no,
            after={"material_version": certification.current_material_version},
            scope_type="store",
            scope_id=store.id,
        )
        await self.session.commit()
        return detail

    async def certification_events(
        self, access: AdminAccess, certification_no: str
    ) -> list[AdminCertificationEventView]:
        row = await self.repository.certification_by_no(certification_no)
        if row is None:
            raise _not_found()
        certification, store = row
        access.require_scope("store", store.id)
        return [
            _certification_event_view(event)
            for event in await self.repository.certification_events(certification.id)
        ]

    async def decide_certification(
        self,
        access: AdminAccess,
        certification_no: str,
        payload: AdminCertificationDecisionRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminCertificationDetail:
        claim = await self.idempotency.begin(
            scope_key=f"admin:certification-decision:{certification_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="store_certification",
        )
        row = await self.repository.certification_by_no(certification_no, for_update=True)
        if row is None:
            raise _not_found()
        certification, store = row
        access.require_scope("store", store.id)
        if claim.replayed:
            return await self._certification_detail(certification, store)
        _check_version(certification.version, expected_version)
        if certification.review_status != "pending":
            raise _invalid_transition(certification.review_status, payload.decision)
        next_status = {
            "approve": "approved",
            "reject": "rejected",
            "request_more_info": "more_info_required",
        }[payload.decision]
        now = utc_now()
        if (
            payload.decision == "approve"
            and payload.valid_until is not None
            and payload.valid_until < now.date()
        ):
            raise ApplicationError(
                status=422,
                code="CERTIFICATION_VALIDITY_EXPIRED",
                title="Certification validity expired",
                detail="认证有效期截止日期不能早于当前日期。",
            )
        certification.review_status = next_status
        certification.reviewed_by = access.context.user.id
        certification.reviewed_at = now
        certification.decision_reason_code = payload.reason_code
        certification.decision_reason = payload.reason
        certification.valid_from = payload.valid_from if payload.decision == "approve" else None
        certification.valid_until = payload.valid_until if payload.decision == "approve" else None
        certification.version += 1
        event = StoreCertificationEvent(
            event_no=new_prefixed_ulid("evt_"),
            certification_id=certification.id,
            event_type={
                "approve": "approved",
                "reject": "rejected",
                "request_more_info": "more_info_requested",
            }[payload.decision],
            material_version=certification.current_material_version,
            evidence_file_ids=await self._current_evidence_file_ids(certification),
            reason_code=payload.reason_code,
            reason=payload.reason,
            required_materials=(
                [item.model_dump(mode="json") for item in payload.required_materials]
                if payload.required_materials
                else None
            ),
            actor_type="admin",
            actor_user_id=access.context.user.id,
            certification_version=certification.version,
            request_id=_request_id(),
            trace_id=_request_id(),
        )
        self.session.add(event)
        _add_outbox(
            self.session,
            event_type=f"store.certification_{next_status}.v1",
            aggregate_type="store_certification",
            aggregate_no=certification.certification_no,
            aggregate_version=certification.version,
            payload={
                "certification_id": certification.certification_no,
                "store_id": store.store_no,
                "status": next_status,
                "material_version": certification.current_material_version,
            },
        )
        record_admin_operation(
            self.session,
            access,
            action=f"decide_certification_{payload.decision}",
            target_type="store_certification",
            target_no=certification.certification_no,
            reason=payload.reason,
            before={"status": "pending", "version": certification.version - 1},
            after={"status": next_status, "version": certification.version},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(
            claim, response_status=200, resource_no=certification.certification_no
        )
        await self.session.flush()
        detail = await self._certification_detail(certification, store)
        await self.session.commit()
        return detail

    async def add_material_version(
        self,
        access: AdminAccess,
        certification_no: str,
        payload: AdminCertificationMaterialRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminCertificationDetail:
        claim = await self.idempotency.begin(
            scope_key=f"admin:certification-material:{certification_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="store_certification",
        )
        row = await self.repository.certification_by_no(certification_no, for_update=True)
        if row is None:
            raise _not_found()
        certification, store = row
        access.require_scope("store", store.id)
        if claim.replayed:
            return await self._certification_detail(certification, store)
        _check_version(certification.version, expected_version)
        if certification.review_status != "more_info_required":
            raise _invalid_transition(certification.review_status, "add_material_version")
        files = await self.repository.files_by_nos(payload.evidence_file_ids)
        unique_file_nos = set(payload.evidence_file_ids)
        if (
            len(payload.evidence_file_ids) != len(unique_file_nos)
            or len(files) != len(unique_file_nos)
            or any(
                file.file_status != "active"
                or file.scan_status != "safe"
                or file.visibility != "private"
                or file.purpose != "store_certification"
                or file.owner_type != "store"
                or file.owner_no != store.store_no
                for file in files
            )
        ):
            raise ApplicationError(
                status=422,
                code="CERTIFICATION_FILE_NOT_BINDABLE",
                title="Certification file cannot be bound",
                detail="认证材料必须全部属于当前店铺、用途匹配并通过安全扫描。",
            )
        next_material_version = certification.current_material_version + 1
        certification.current_material_version = next_material_version
        certification.evidence_object_key = files[0].object_key
        certification.review_status = "pending"
        certification.resubmitted_at = utc_now()
        certification.reviewed_by = None
        certification.reviewed_at = None
        certification.decision_reason_code = None
        certification.decision_reason = None
        certification.version += 1
        self.session.add(
            StoreCertificationEvent(
                event_no=new_prefixed_ulid("evt_"),
                certification_id=certification.id,
                event_type="materials_resubmitted",
                material_version=next_material_version,
                evidence_file_ids=payload.evidence_file_ids,
                reason=payload.reason,
                actor_type="admin",
                actor_user_id=access.context.user.id,
                certification_version=certification.version,
                request_id=_request_id(),
                trace_id=_request_id(),
            )
        )
        _add_outbox(
            self.session,
            event_type="store.certification_materials_resubmitted.v1",
            aggregate_type="store_certification",
            aggregate_no=certification.certification_no,
            aggregate_version=certification.version,
            payload={
                "certification_id": certification.certification_no,
                "store_id": store.store_no,
                "material_version": next_material_version,
            },
        )
        record_admin_operation(
            self.session,
            access,
            action="add_certification_material_version",
            target_type="store_certification",
            target_no=certification.certification_no,
            reason=payload.reason,
            before={"material_version": next_material_version - 1},
            after={"material_version": next_material_version},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(
            claim, response_status=200, resource_no=certification.certification_no
        )
        await self.session.flush()
        detail = await self._certification_detail(certification, store)
        await self.session.commit()
        return detail

    async def list_policies(self, access: AdminAccess, store_no: str) -> list[AdminStorePolicyView]:
        store = await self._store_for_scope(access, store_no)
        policies = await self.repository.policies(store.id)
        return [_policy_view(item, store.store_no) for item in policies]

    async def get_policy(
        self, access: AdminAccess, store_no: str, policy_no: str
    ) -> AdminStorePolicyView:
        store = await self._store_for_scope(access, store_no)
        policy = await self.repository.policy_by_no(store.id, policy_no)
        if policy is None:
            raise _not_found()
        return _policy_view(policy, store.store_no)

    async def create_policy(
        self,
        access: AdminAccess,
        store_no: str,
        payload: AdminStorePolicyCreateRequest,
        idempotency_key: str,
    ) -> AdminStorePolicyView:
        store = await self._store_for_scope(access, store_no, for_update=True)
        claim = await self.idempotency.begin(
            scope_key=f"admin:store-policy-create:{store_no}:{payload.policy_type}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="store_service_policy",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.policy_by_no(store.id, claim.record.resource_no)
            if existing is not None:
                return _policy_view(existing, store.store_no)
        latest = await self.repository.latest_policy(store.id, payload.policy_type)
        policy = StoreServicePolicy(
            policy_no=new_prefixed_ulid("pol_"),
            store_id=store.id,
            policy_type=payload.policy_type,
            title=payload.title,
            content=payload.content,
            content_hash=hashlib.sha256(payload.content.encode()).digest(),
            policy_version=await self.repository.next_policy_version(store.id, payload.policy_type),
            policy_status="draft",
            effective_at=payload.effective_at,
            expires_at=payload.expires_at,
            supersedes_policy_id=latest.id if latest else None,
            created_by=access.context.user.id,
        )
        self.session.add(policy)
        await self.session.flush()
        record_admin_operation(
            self.session,
            access,
            action="create_store_policy",
            target_type="store_service_policy",
            target_no=policy.policy_no,
            after={"policy_type": policy.policy_type, "policy_version": policy.policy_version},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(claim, response_status=201, resource_no=policy.policy_no)
        result = _policy_view(policy, store.store_no)
        await self.session.commit()
        return result

    async def update_policy(
        self,
        access: AdminAccess,
        store_no: str,
        policy_no: str,
        payload: AdminStorePolicyUpdateRequest,
        expected_version: int,
    ) -> AdminStorePolicyView:
        store = await self._store_for_scope(access, store_no)
        policy = await self.repository.policy_by_no(store.id, policy_no, for_update=True)
        if policy is None:
            raise _not_found()
        _check_version(policy.version, expected_version)
        if policy.policy_status != "draft":
            raise ApplicationError(
                status=409,
                code="PUBLISHED_POLICY_IMMUTABLE",
                title="Published policy is immutable",
                detail="只有草稿政策可以修改。",
            )
        before = _policy_snapshot(policy)
        fields = payload.model_fields_set
        if payload.title is not None:
            policy.title = payload.title
        if payload.content is not None:
            policy.content = payload.content
            policy.content_hash = hashlib.sha256(payload.content.encode()).digest()
        if "effective_at" in fields:
            policy.effective_at = payload.effective_at
        if "expires_at" in fields:
            policy.expires_at = payload.expires_at
        if (
            policy.effective_at is not None
            and policy.expires_at is not None
            and policy.expires_at <= policy.effective_at
        ):
            raise ApplicationError(
                status=422,
                code="POLICY_TIME_WINDOW_INVALID",
                title="Invalid policy time window",
                detail="政策失效时间必须晚于生效时间。",
            )
        policy.version += 1
        record_admin_operation(
            self.session,
            access,
            action="update_store_policy",
            target_type="store_service_policy",
            target_no=policy.policy_no,
            before=before,
            after=_policy_snapshot(policy),
            scope_type="store",
            scope_id=store.id,
        )
        result = _policy_view(policy, store.store_no)
        await self.session.commit()
        return result

    async def publish_policy(
        self,
        access: AdminAccess,
        store_no: str,
        policy_no: str,
        payload: AdminPolicyCommandRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminStorePolicyView:
        return await self._policy_command(
            access,
            store_no,
            policy_no,
            payload,
            expected_version,
            idempotency_key,
            command="publish",
        )

    async def withdraw_policy(
        self,
        access: AdminAccess,
        store_no: str,
        policy_no: str,
        payload: AdminPolicyCommandRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AdminStorePolicyView:
        return await self._policy_command(
            access,
            store_no,
            policy_no,
            payload,
            expected_version,
            idempotency_key,
            command="withdraw",
        )

    async def _policy_command(
        self,
        access: AdminAccess,
        store_no: str,
        policy_no: str,
        payload: AdminPolicyCommandRequest,
        expected_version: int,
        idempotency_key: str,
        *,
        command: str,
    ) -> AdminStorePolicyView:
        store = await self._store_for_scope(access, store_no)
        claim = await self.idempotency.begin(
            scope_key=f"admin:store-policy-{command}:{policy_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="store_service_policy",
        )
        policy = await self.repository.policy_by_no(store.id, policy_no, for_update=True)
        if policy is None:
            raise _not_found()
        if claim.replayed:
            return _policy_view(policy, store.store_no)
        _check_version(policy.version, expected_version)
        now = utc_now()
        previous_status = policy.policy_status
        if command == "publish":
            if policy.policy_status != "draft":
                raise _invalid_transition(policy.policy_status, command)
            if policy.effective_at is None:
                raise ApplicationError(
                    status=409,
                    code="POLICY_EFFECTIVE_AT_REQUIRED",
                    title="Policy effective time required",
                    detail="发布前必须设置政策生效时间。",
                )
            if policy.expires_at is not None and policy.expires_at <= now:
                raise ApplicationError(
                    status=409,
                    code="POLICY_TIME_WINDOW_EXPIRED",
                    title="Policy time window expired",
                    detail="不能发布有效期已经结束的政策。",
                )
            if await self.repository.overlapping_policies(
                policy, policy.effective_at, policy.expires_at
            ):
                raise ApplicationError(
                    status=409,
                    code="POLICY_EFFECTIVE_WINDOW_OVERLAP",
                    title="Policy time window overlaps",
                    detail="同一类型已有时间窗重叠的已发布政策。",
                )
            policy.policy_status = "published"
            policy.published_at = now
            policy.published_by = access.context.user.id
            event_type = "store.policy_published.v1"
        else:
            if policy.policy_status != "published":
                raise _invalid_transition(policy.policy_status, command)
            if policy.expires_at is not None and policy.expires_at <= now:
                raise ApplicationError(
                    status=409,
                    code="POLICY_ALREADY_EXPIRED",
                    title="Policy already expired",
                    detail="已过期政策不能执行撤回，应由过期任务转换状态。",
                )
            policy.policy_status = "withdrawn"
            policy.withdrawn_at = now
            policy.withdrawn_by = access.context.user.id
            event_type = "store.policy_withdrawn.v1"
        policy.version += 1
        _add_outbox(
            self.session,
            event_type=event_type,
            aggregate_type="store_service_policy",
            aggregate_no=policy.policy_no,
            aggregate_version=policy.version,
            payload={
                "policy_id": policy.policy_no,
                "store_id": store.store_no,
                "policy_type": policy.policy_type,
                "policy_version": policy.policy_version,
                "status": policy.policy_status,
            },
        )
        record_admin_operation(
            self.session,
            access,
            action=f"{command}_store_policy",
            target_type="store_service_policy",
            target_no=policy.policy_no,
            reason=payload.reason,
            before={"status": previous_status, "version": policy.version - 1},
            after={"status": policy.policy_status, "version": policy.version},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(claim, response_status=200, resource_no=policy.policy_no)
        result = _policy_view(policy, store.store_no)
        await self.session.commit()
        return result

    async def _store_for_scope(
        self,
        access: AdminAccess,
        store_no: str,
        *,
        for_update: bool = False,
    ) -> Store:
        row = await self.repository.store_by_no(store_no, for_update=for_update)
        if row is None:
            raise _not_found()
        access.require_scope("store", row[0].id)
        return row[0]

    async def _certification_detail(
        self, certification: StoreCertification, store: Store
    ) -> AdminCertificationDetail:
        summary = _certification_summary(certification, store)
        return AdminCertificationDetail(
            **summary.model_dump(),
            evidence_file_ids=await self._current_evidence_file_ids(certification),
            decision_reason_code=certification.decision_reason_code,
            decision_reason=certification.decision_reason,
        )

    async def _current_evidence_file_ids(self, certification: StoreCertification) -> list[str]:
        events = await self.repository.certification_events(certification.id)
        matching = next(
            (
                event
                for event in events
                if event.material_version == certification.current_material_version
                and event.evidence_file_ids
            ),
            None,
        )
        if matching and matching.evidence_file_ids:
            return list(matching.evidence_file_ids)
        file_object = await self.repository.file_by_object_key(certification.evidence_object_key)
        return [file_object.file_no] if file_object else []


def _store_view(store: Store, owner_user_no: str, logo: FileObject | None) -> AdminStoreView:
    return AdminStoreView(
        store_id=store.store_no,
        owner_user_id=owner_user_no,
        store_name=store.store_name,
        description=store.description,
        logo_file_id=logo.file_no if logo else None,
        logo_url=f"/api/v1/files/{logo.file_no}" if logo else None,
        status=store.store_status,
        rating_score=format(store.rating_score, "f"),
        rating_count=store.rating_count,
        follower_count=store.follower_count,
        sales_count=store.sales_count,
        store_name_changed_at=store.store_name_changed_at,
        store_name_change_available_at=(
            store.store_name_changed_at + timedelta(days=7)
            if store.store_name_changed_at is not None
            else None
        ),
        opened_at=store.opened_at,
        suspended_at=store.suspended_at,
        closed_at=store.closed_at,
        version=store.version,
    )


def _normalize_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _certification_summary(
    certification: StoreCertification, store: Store
) -> AdminCertificationSummary:
    return AdminCertificationSummary(
        certification_id=certification.certification_no,
        store_id=store.store_no,
        store_name=store.store_name,
        certification_type=certification.certification_type,
        review_status=certification.review_status,
        material_version=certification.current_material_version,
        valid_from=certification.valid_from,
        valid_until=certification.valid_until,
        reviewed_at=certification.reviewed_at,
        version=certification.version,
    )


def _certification_event_view(event: StoreCertificationEvent) -> AdminCertificationEventView:
    return AdminCertificationEventView(
        event_id=event.event_no,
        event_type=event.event_type,
        material_version=event.material_version,
        evidence_file_ids=event.evidence_file_ids or [],
        reason_code=event.reason_code,
        reason=event.reason,
        required_materials=event.required_materials,
        actor_type=event.actor_type,
        certification_version=event.certification_version,
        created_at=event.created_at,
    )


def _policy_view(policy: StoreServicePolicy, store_no: str) -> AdminStorePolicyView:
    return AdminStorePolicyView(
        policy_id=policy.policy_no,
        store_id=store_no,
        policy_type=policy.policy_type,
        title=policy.title,
        content=policy.content,
        policy_version=policy.policy_version,
        status=policy.policy_status,
        effective_at=policy.effective_at,
        expires_at=policy.expires_at,
        published_at=policy.published_at,
        withdrawn_at=policy.withdrawn_at,
        version=policy.version,
    )


def _policy_snapshot(policy: StoreServicePolicy) -> dict[str, object]:
    return {
        "title": policy.title,
        "content_hash": policy.content_hash.hex(),
        "effective_at": policy.effective_at.isoformat() if policy.effective_at else None,
        "expires_at": policy.expires_at.isoformat() if policy.expires_at else None,
        "status": policy.policy_status,
        "version": policy.version,
    }


def _add_outbox(
    session: AsyncSession,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_no: str,
    aggregate_version: int,
    payload: dict[str, object],
) -> None:
    session.add(
        OutboxEvent(
            event_no=new_prefixed_ulid("evt_"),
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_no=aggregate_no,
            aggregate_version=aggregate_version,
            payload=payload,
            event_status="pending",
            available_at=utc_now(),
            trace_id=_request_id(),
        )
    )


def _check_version(actual: int, expected: int) -> None:
    if actual != expected:
        raise ApplicationError(
            status=412,
            code="RESOURCE_VERSION_CONFLICT",
            title="Resource version conflict",
            detail="资源已被其他操作更新，请刷新后重试。",
        )


def _cursor_filter(kind: str, **filters: str | None) -> str:
    return json.dumps(
        {"kind": kind, **filters},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _cursor_value(codec: CursorCodec, token: str | None, filter_key: str) -> str | None:
    position = codec.decode(token, filter_key=filter_key)
    if position is None:
        return None
    if position.direction != "next" or len(position.values) != 1:
        raise ApplicationError(
            status=400,
            code="PAGINATION_CURSOR_INVALID",
            title="Invalid pagination cursor",
            detail="分页位置无效，请重新加载列表。",
        )
    return position.values[0]


def _invalid_transition(current: str, command: str) -> ApplicationError:
    return ApplicationError(
        status=409,
        code="ILLEGAL_STATE_TRANSITION",
        title="Illegal state transition",
        detail=f"当前状态 {current} 不允许执行命令 {command}。",
    )


def _request_id() -> str:
    return request_id_context.get() or new_prefixed_ulid("req_")


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="未找到该资源。",
    )


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(
        status=409,
        code=code,
        title="Resource conflict",
        detail=detail,
    )
