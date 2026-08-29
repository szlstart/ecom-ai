from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import SecurityService, utc_now
from app.modules.agent_runtime.models import AiMemoryCleanupTask, UserAgentConsent
from app.modules.agent_runtime.privacy_schemas import (
    AiCleanupTaskView,
    AiMemoryActivationRequest,
    AiMemoryDeleteRequest,
    AiMemoryList,
    AiMemoryRevisionRequest,
    AiMemoryView,
    AiPersonalizationDisableRequest,
)
from app.modules.identity.models import User
from app.modules.system.models import OutboxEvent


class AiPrivacyService:
    def __init__(
        self, mysql: AsyncSession, postgres: AsyncSession, security: SecurityService
    ) -> None:
        self.mysql = mysql
        self.postgres = postgres
        self.security = security
        self.idempotency = IdempotencyService(mysql)

    async def list_memories(
        self,
        user: User,
        *,
        namespace: str | None,
        store_no: str | None,
        memory_type: str | None,
        status: str | None,
    ) -> AiMemoryList:
        params: dict[str, object] = {
            "user_no": user.user_no,
            "namespace": namespace,
            "namespace_filter": namespace is not None,
            "store_no": store_no,
            "store_no_filter": store_no is not None,
            "memory_type": memory_type,
            "memory_type_filter": memory_type is not None,
            "memory_status": status,
            "memory_status_filter": status is not None,
        }
        rows = (
            (
                await self.postgres.execute(
                    text(
                        """SELECT memory_no, namespace, store_no, memory_type, memory_key,
                        content_ciphertext, source_type, consent_no, memory_status, expires_at,
                        created_at, updated_at, version FROM memory.items
                        WHERE user_no = :user_no AND content_ciphertext IS NOT NULL
                        AND (:namespace_filter = false OR namespace = :namespace)
                        AND (:store_no_filter = false OR store_no = :store_no)
                        AND (:memory_type_filter = false OR memory_type = :memory_type)
                        AND (:memory_status_filter = false OR memory_status = :memory_status)
                        ORDER BY updated_at DESC, id DESC LIMIT 200"""
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        return AiMemoryList(items=[self._memory_view(dict(row)) for row in rows])

    async def revise_memory(
        self,
        user: User,
        memory_no: str,
        payload: AiMemoryRevisionRequest,
        expected_version: int,
    ) -> AiMemoryView:
        consent = await self._require_personalization(user)
        row = await self._memory_row(user, memory_no, for_update=True)
        if int(row["version"]) != expected_version:
            raise _version_conflict(int(row["version"]))
        if row["memory_status"] not in {"active", "candidate"}:
            raise _state_conflict()
        if row["consent_no"] != consent.consent_no:
            raise _consent_conflict()
        new_value = payload.new_value.strip()
        if _looks_sensitive(new_value):
            raise ApplicationError(
                status=422,
                code="AI_MEMORY_SENSITIVE_CONTENT",
                title="Sensitive memory rejected",
                detail="密码、证件、支付信息或完整地址不能写入长期记忆。",
            )
        new_no = new_prefixed_ulid("mem_")
        event_no = new_prefixed_ulid("mev_")
        ciphertext = self.security.encrypt("ai-memory-content", new_value)
        content_hash = self.security.keyed_hash("ai-memory-content-hash", new_value)
        await self.postgres.execute(
            text(
                """UPDATE memory.items SET memory_status='superseded', version=version+1,
                updated_at=now() WHERE id=:id AND version=:version"""
            ),
            {"id": row["id"], "version": expected_version},
        )
        await self.postgres.execute(
            text(
                """INSERT INTO memory.items
                (memory_no,user_no,namespace,store_no,memory_type,confidence,
                 memory_status,consent_no,expires_at,memory_key,content_ciphertext,content_hash,
                 dedupe_fingerprint,key_version,source_type,source_ref,source_conversation_no,
                 source_message_no,consent_policy_version,validation_snapshot,salience,
                 data_classification,memory_risk_level,valid_from,valid_until,
                 supersedes_memory_id,version)
                VALUES (:memory_no,:user_no,:namespace,:store_no,:memory_type,:confidence,
                 'active',:consent_no,:expires_at,:memory_key,:ciphertext,:content_hash,
                 :dedupe,1,'user_settings',:source_ref,NULL,NULL,:policy_version,
                 CAST(:validation AS JSONB),:salience,'L2','low',now(),:valid_until,:old_id,0)"""
            ),
            {
                "memory_no": new_no,
                "user_no": user.user_no,
                "namespace": row["namespace"],
                "store_no": row["store_no"],
                "memory_type": row["memory_type"],
                "confidence": row["confidence"],
                "consent_no": row["consent_no"],
                "expires_at": row["expires_at"],
                "memory_key": row["memory_key"],
                "ciphertext": ciphertext,
                "content_hash": content_hash,
                "dedupe": self.security.keyed_hash(
                    "ai-memory-dedupe", f"{user.user_no}:{row['memory_key']}:{new_value.casefold()}"
                ),
                "source_ref": memory_no,
                "policy_version": row["consent_policy_version"],
                "validation": '{"explicit_user_revision":true}',
                "salience": row["salience"],
                "valid_until": row["valid_until"],
                "old_id": row["id"],
            },
        )
        await self._memory_event(
            event_no,
            new_no,
            user.user_no,
            "corrected",
            "active",
            "active",
            payload.reason_code,
            row["content_hash"],
            content_hash,
        )
        await self.postgres.commit()
        return self._memory_view(await self._memory_row(user, new_no))

    async def activate_memory(
        self,
        user: User,
        memory_no: str,
        payload: AiMemoryActivationRequest,
        expected_version: int,
    ) -> AiMemoryView:
        del payload
        consent = await self._require_personalization(user)
        row = await self._memory_row(user, memory_no, for_update=True)
        if int(row["version"]) != expected_version:
            raise _version_conflict(int(row["version"]))
        if row["memory_status"] == "active":
            return self._memory_view(row)
        if row["memory_status"] != "candidate":
            raise _state_conflict()
        if row["consent_no"] != consent.consent_no:
            raise _consent_conflict()
        conflict_params = {
            "user_no": user.user_no,
            "namespace": row["namespace"],
            "store_no": row["store_no"],
            "memory_type": row["memory_type"],
            "memory_key": row["memory_key"],
            "id": row["id"],
        }
        conflicts = list(
            (
                await self.postgres.execute(
                    text(
                        """SELECT memory_no,content_hash FROM memory.items
                        WHERE user_no=:user_no AND namespace=:namespace
                          AND store_no IS NOT DISTINCT FROM :store_no
                          AND memory_type=:memory_type AND memory_key=:memory_key
                          AND memory_status='active' AND id<>:id FOR UPDATE"""
                    ),
                    conflict_params,
                )
            )
            .mappings()
            .all()
        )
        if conflicts:
            await self.postgres.execute(
                text(
                    """UPDATE memory.items SET memory_status='superseded',version=version+1,
                    updated_at=now() WHERE user_no=:user_no AND namespace=:namespace
                      AND store_no IS NOT DISTINCT FROM :store_no
                      AND memory_type=:memory_type AND memory_key=:memory_key
                      AND memory_status='active' AND id<>:id"""
                ),
                conflict_params,
            )
            for conflict in conflicts:
                await self._memory_event(
                    new_prefixed_ulid("mev_"),
                    str(conflict["memory_no"]),
                    user.user_no,
                    "superseded",
                    "active",
                    "superseded",
                    "USER_CONFIRMED_REPLACEMENT",
                    conflict["content_hash"],
                    None,
                )
        await self.postgres.execute(
            text(
                """UPDATE memory.items SET memory_status='active',version=version+1,
                updated_at=now() WHERE id=:id AND version=:version"""
            ),
            {"id": row["id"], "version": expected_version},
        )
        await self._memory_event(
            new_prefixed_ulid("mev_"),
            memory_no,
            user.user_no,
            "confirmed",
            "candidate",
            "active",
            "EXPLICIT_USER_CONFIRMATION",
            row["content_hash"],
            row["content_hash"],
        )
        await self.postgres.commit()
        return self._memory_view(await self._memory_row(user, memory_no))

    async def delete_memory(
        self,
        user: User,
        memory_no: str,
        payload: AiMemoryDeleteRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AiCleanupTaskView:
        row = await self._memory_row(user, memory_no, for_update=True)
        if row["memory_status"] == "deleted":
            existing = await self._cleanup_by_source(user.id, "memory", memory_no)
            if existing is not None:
                return _cleanup_view(existing)
        if int(row["version"]) != expected_version:
            raise _version_conflict(int(row["version"]))
        if row["memory_status"] not in {"active", "candidate", "superseded", "revoked"}:
            raise _state_conflict()
        await self.postgres.execute(
            text(
                """UPDATE memory.items SET memory_status='deleted',
                version=version+1, updated_at=now() WHERE id=:id AND version=:version"""
            ),
            {"id": row["id"], "version": expected_version},
        )
        await self._memory_event(
            new_prefixed_ulid("mev_"),
            memory_no,
            user.user_no,
            "deleted",
            str(row["memory_status"]),
            "deleted",
            payload.reason_code,
            row["content_hash"],
            None,
        )
        await self.postgres.commit()
        return await self._create_cleanup(
            user,
            "memory_delete",
            str(row["namespace"]),
            str(row["store_no"] or user.user_no),
            "memory",
            memory_no,
            1,
            idempotency_key,
        )

    async def disable_all(
        self,
        user: User,
        payload: AiPersonalizationDisableRequest,
        idempotency_key: str,
    ) -> AiCleanupTaskView:
        existing = await self._cleanup_by_key(user.id, idempotency_key)
        if existing is not None:
            return _cleanup_view(existing)
        consents = list(
            (
                await self.mysql.scalars(
                    select(UserAgentConsent)
                    .where(
                        UserAgentConsent.user_id == user.id,
                        UserAgentConsent.consent_status.in_(("active", "paused")),
                    )
                    .with_for_update()
                )
            ).all()
        )
        now = utc_now()
        for consent in consents:
            consent.consent_status = "revoked"
            consent.revoked_at = now
            consent.version += 1
        task = self._new_cleanup(
            user,
            "disable_all",
            "all",
            user.user_no,
            "user",
            user.user_no,
            len(consents),
            idempotency_key,
        )
        self.mysql.add(task)
        self.mysql.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="ai.personalization.disabled.v1",
                aggregate_type="user",
                aggregate_no=user.user_no,
                aggregate_version=max([item.version for item in consents], default=0),
                payload={
                    "schema_version": 1,
                    "user_id": user.user_no,
                    "cleanup_task_id": task.task_no,
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=request_id_context.get() or new_prefixed_ulid("req_"),
            )
        )
        await self.mysql.flush()
        await self.mysql.refresh(task)
        result = _cleanup_view(task)
        await self.mysql.commit()
        return result

    async def get_cleanup(self, user: User, task_no: str) -> AiCleanupTaskView:
        task = await self.mysql.scalar(
            select(AiMemoryCleanupTask).where(
                AiMemoryCleanupTask.user_id == user.id, AiMemoryCleanupTask.task_no == task_no
            )
        )
        if task is None:
            raise _not_found()
        return _cleanup_view(task)

    async def retry_cleanup(
        self, user: User, task_no: str, expected_version: int, idempotency_key: str
    ) -> AiCleanupTaskView:
        claim = await self.idempotency.begin(
            scope_key=f"ai-cleanup-retry:{user.id}:{task_no}",
            idempotency_key=idempotency_key,
            payload={"task_id": task_no, "expected_version": expected_version},
            resource_type="ai_memory_cleanup_task",
        )
        if claim.replayed and claim.record.response_body is not None:
            return AiCleanupTaskView.model_validate(claim.record.response_body)
        task = await self.mysql.scalar(
            select(AiMemoryCleanupTask)
            .where(AiMemoryCleanupTask.user_id == user.id, AiMemoryCleanupTask.task_no == task_no)
            .with_for_update()
        )
        if task is None:
            raise _not_found()
        if task.version != expected_version:
            raise _version_conflict(task.version)
        if (
            task.task_status not in {"failed", "partial_failed"}
            or task.retry_count >= task.max_retries
        ):
            raise _state_conflict()
        task.task_status = "queued"
        task.retry_count += 1
        task.error_code = None
        task.completed_at = None
        task.version += 1
        await self.mysql.flush()
        await self.mysql.refresh(task)
        result = _cleanup_view(task)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=task.task_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.mysql.commit()
        return result

    async def _memory_row(
        self, user: User, memory_no: str, *, for_update: bool = False
    ) -> dict[str, Any]:
        statement = (
            text(
                """SELECT id,memory_no,user_no,namespace,store_no,memory_type,memory_key,
                content_ciphertext,content_hash,confidence,salience,memory_status,consent_no,
                consent_policy_version,source_type,expires_at,valid_until,created_at,updated_at,
                version FROM memory.items WHERE memory_no=:memory_no AND user_no=:user_no
                AND content_ciphertext IS NOT NULL FOR UPDATE"""
            )
            if for_update
            else text(
                """SELECT id,memory_no,user_no,namespace,store_no,memory_type,memory_key,
                content_ciphertext,content_hash,confidence,salience,memory_status,consent_no,
                consent_policy_version,source_type,expires_at,valid_until,created_at,updated_at,
                version FROM memory.items WHERE memory_no=:memory_no AND user_no=:user_no
                AND content_ciphertext IS NOT NULL"""
            )
        )
        row = (
            (
                await self.postgres.execute(
                    statement,
                    {"memory_no": memory_no, "user_no": user.user_no},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise _not_found()
        return dict(row)

    async def _require_personalization(self, user: User) -> UserAgentConsent:
        now = utc_now()
        consent = await self.mysql.scalar(
            select(UserAgentConsent).where(
                UserAgentConsent.user_id == user.id,
                UserAgentConsent.consent_type == "personalization",
                UserAgentConsent.consent_status == "active",
                or_(
                    UserAgentConsent.expires_at.is_(None),
                    UserAgentConsent.expires_at > now,
                ),
            )
        )
        if consent is None:
            raise ApplicationError(
                status=403,
                code="AI_CONSENT_REQUIRED",
                title="AI consent required",
                detail="更正长期记忆前需要有效的个性化授权。",
            )
        return consent

    async def _memory_event(
        self,
        event_no: str,
        memory_no: str,
        user_no: str,
        event_type: str,
        from_status: str,
        to_status: str,
        reason_code: str,
        before: object,
        after: object,
    ) -> None:
        await self.postgres.execute(
            text(
                """INSERT INTO memory.events
                (event_no,memory_id,event_type,actor_type,reason_code,user_no,from_status,to_status,
                 actor_no,content_hash_before,content_hash_after,trace_id,metadata_redacted)
                VALUES (:event_no,(SELECT id FROM memory.items WHERE memory_no=:memory_no),
                 :event_type,'user',:reason_code,:user_no,:from_status,
                 :to_status,:user_no,:before,:after,:trace_id,'{}'::jsonb)"""
            ),
            {
                "event_no": event_no,
                "memory_no": memory_no,
                "event_type": event_type,
                "reason_code": reason_code,
                "user_no": user_no,
                "from_status": from_status,
                "to_status": to_status,
                "before": before,
                "after": after,
                "trace_id": request_id_context.get(),
            },
        )

    async def _create_cleanup(
        self,
        user: User,
        command: str,
        scope_type: str,
        scope_no: str,
        source_type: str,
        source_no: str,
        total: int,
        key: str,
    ) -> AiCleanupTaskView:
        existing = await self._cleanup_by_key(user.id, key)
        if existing is not None:
            return _cleanup_view(existing)
        task = self._new_cleanup(
            user, command, scope_type, scope_no, source_type, source_no, total, key
        )
        self.mysql.add(task)
        await self.mysql.flush()
        await self.mysql.refresh(task)
        result = _cleanup_view(task)
        await self.mysql.commit()
        return result

    def _new_cleanup(
        self,
        user: User,
        command: str,
        scope_type: str,
        scope_no: str,
        source_type: str,
        source_no: str,
        total: int,
        key: str,
    ) -> AiMemoryCleanupTask:
        return AiMemoryCleanupTask(
            task_no=new_prefixed_ulid("job_"),
            user_id=user.id,
            command_type=command,
            scope_type=scope_type,
            scope_no=scope_no,
            source_resource_type=source_type,
            source_resource_no=source_no,
            task_status="queued",
            total_count=total,
            processed_count=0,
            failed_count=0,
            retry_count=0,
            max_retries=3,
            idempotency_key_hash=self._cleanup_key(user.id, key),
        )

    async def _cleanup_by_key(self, user_id: int, key: str) -> AiMemoryCleanupTask | None:
        return cast(
            AiMemoryCleanupTask | None,
            await self.mysql.scalar(
                select(AiMemoryCleanupTask).where(
                    AiMemoryCleanupTask.user_id == user_id,
                    AiMemoryCleanupTask.idempotency_key_hash == self._cleanup_key(user_id, key),
                )
            ),
        )

    async def _cleanup_by_source(
        self, user_id: int, source_type: str, source_no: str
    ) -> AiMemoryCleanupTask | None:
        return cast(
            AiMemoryCleanupTask | None,
            await self.mysql.scalar(
                select(AiMemoryCleanupTask)
                .where(
                    AiMemoryCleanupTask.user_id == user_id,
                    AiMemoryCleanupTask.source_resource_type == source_type,
                    AiMemoryCleanupTask.source_resource_no == source_no,
                )
                .order_by(AiMemoryCleanupTask.id.desc())
            ),
        )

    def _cleanup_key(self, user_id: int, key: str) -> bytes:
        return self.security.keyed_hash("ai-cleanup-idempotency", f"{user_id}:{key}")

    def _memory_view(self, row: Mapping[str, Any]) -> AiMemoryView:
        values = row
        ciphertext = values["content_ciphertext"]
        if not isinstance(ciphertext, bytes):
            raise _not_found()
        return AiMemoryView(
            memory_id=str(values["memory_no"]),
            namespace=cast(Literal["exclusive", "store"], values["namespace"]),
            store_id=cast(str | None, values["store_no"]),
            memory_type=str(values["memory_type"]),
            memory_key=str(values["memory_key"]),
            value=self.security.decrypt("ai-memory-content", ciphertext),
            source_type=str(values["source_type"]),
            consent_id=cast(str | None, values["consent_no"]),
            status=cast(
                Literal["candidate", "active", "superseded", "revoked", "expired", "deleted"],
                values["memory_status"],
            ),
            expires_at=values["expires_at"],
            created_at=values["created_at"],
            updated_at=values["updated_at"],
            version=int(values["version"]),
        )


def _cleanup_view(task: AiMemoryCleanupTask) -> AiCleanupTaskView:
    return AiCleanupTaskView(
        cleanup_task_id=task.task_no,
        command_type=task.command_type,
        scope_type=task.scope_type,
        scope_id=task.scope_no,
        source_resource_type=task.source_resource_type,
        source_resource_id=task.source_resource_no,
        status=cast(
            Literal["queued", "running", "succeeded", "partial_failed", "failed"],
            task.task_status,
        ),
        total_count=task.total_count,
        processed_count=task.processed_count,
        failed_count=task.failed_count,
        retry_count=task.retry_count,
        max_retries=task.max_retries,
        error_code=task.error_code,
        can_retry=task.task_status in {"failed", "partial_failed"}
        and task.retry_count < task.max_retries,
        created_at=task.created_at,
        updated_at=task.updated_at,
        version=task.version,
    )


def _looks_sensitive(value: str) -> bool:
    lowered = value.casefold()
    return any(token in lowered for token in ("password", "密码", "身份证", "银行卡", "cvv"))


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="AI_PRIVACY_RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="该 AI 隐私资源不存在。",
    )


def _version_conflict(current: int) -> ApplicationError:
    return ApplicationError(
        status=412,
        code="VERSION_PRECONDITION_FAILED",
        title="Version conflict",
        detail=f"资源已变更(当前版本 {current})，请刷新后重试。",
    )


def _state_conflict() -> ApplicationError:
    return ApplicationError(
        status=409,
        code="AI_PRIVACY_STATE_CONFLICT",
        title="State conflict",
        detail="当前状态不允许该操作。",
    )


def _consent_conflict() -> ApplicationError:
    return ApplicationError(
        status=409,
        code="AI_MEMORY_CONSENT_CHANGED",
        title="Consent changed",
        detail="创建候选记忆时的授权已变化，请删除该候选后重新明确表达偏好。",
    )
