from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import PurePath

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import utc_now
from app.integrations.object_storage import ObjectStorage
from app.modules.files.auth import FileActor
from app.modules.files.models import FileObject, FileUploadSession
from app.modules.files.policies import UploadPolicy, upload_policy
from app.modules.files.repository import FileRepository
from app.modules.files.schemas import (
    FileMetadataView,
    FileUploadCompleteRequest,
    FileUploadInstruction,
    FileUploadPolicyView,
    FileUploadSessionCreateRequest,
    FileUploadSessionView,
    FileVariantView,
)
from app.modules.system.models import OutboxEvent


class FileService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        storage: ObjectStorage,
    ) -> None:
        self.session = session
        self.settings = settings
        self.storage = storage
        self.repository = FileRepository(session)
        self.idempotency = IdempotencyService(session)

    def policy(self, purpose: str) -> FileUploadPolicyView:
        policy = upload_policy(purpose)
        return FileUploadPolicyView(
            purpose=policy.purpose,
            policy_version=policy.version,
            allowed_mime_types=list(policy.allowed_mime_types),
            allowed_extensions=list(policy.allowed_extensions),
            max_size_bytes=policy.max_size_bytes,
            max_count=policy.max_count,
            max_pixels=policy.max_pixels,
        )

    async def create_upload(
        self,
        actor: FileActor,
        payload: FileUploadSessionCreateRequest,
        key: str,
    ) -> FileUploadSessionView:
        policy = upload_policy(payload.purpose)
        extension = _validate_declaration(policy, payload)
        owner_no = await self._authorize_owner(actor, policy, payload.business_context_id)
        claim = await self.idempotency.begin(
            scope_key=f"file-upload-create:{actor.context.user.user_no}:{policy.purpose}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="file_upload_session",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.upload_session(claim.record.resource_no)
            if existing is not None:
                return await self._session_view(existing, include_upload=True)
        now = utc_now()
        upload_no = new_prefixed_ulid("upl_")
        object_key = f"v1/{policy.purpose}/{now:%Y/%m}/{upload_no}/original.{extension}"
        item = FileUploadSession(
            upload_no=upload_no,
            uploader_user_id=actor.context.user.id,
            purpose=policy.purpose,
            target_owner_type=policy.owner_type,
            target_owner_no=owner_no,
            provider="minio",
            reserved_bucket="temporary-uploads",
            reserved_object_key=object_key,
            upload_type="single",
            expected_mime_types=[payload.content_type],
            max_size_bytes=payload.size_bytes,
            expected_sha256=bytes.fromhex(payload.sha256),
            upload_status="created",
            expires_at=now + timedelta(seconds=self.settings.object_storage_presign_seconds),
        )
        self.session.add(item)
        await self.session.flush()
        self.idempotency.complete(claim, response_status=201, resource_no=item.upload_no)
        await self.session.commit()
        return await self._session_view(item, include_upload=True)

    async def upload_status(self, actor: FileActor, upload_no: str) -> FileUploadSessionView:
        item = await self.repository.upload_session(upload_no)
        if item is None or item.uploader_user_id != actor.context.user.id:
            raise _not_found()
        return await self._session_view(item, include_upload=item.upload_status == "created")

    async def complete_upload(
        self,
        actor: FileActor,
        upload_no: str,
        payload: FileUploadCompleteRequest,
        key: str,
    ) -> FileUploadSessionView:
        initial = await self.repository.upload_session(upload_no)
        if initial is None or initial.uploader_user_id != actor.context.user.id:
            raise _not_found()
        if initial.upload_status == "completed":
            claim = await self.idempotency.begin(
                scope_key=f"file-upload-complete:{upload_no}",
                idempotency_key=key,
                payload=payload.model_dump(mode="json"),
                resource_type="file",
            )
            if not claim.replayed:
                raise _conflict("FILE_UPLOAD_ALREADY_COMPLETED", "上传会话已经完成。")
            return await self._session_view(initial)
        _upload_can_complete(initial)
        bucket = initial.reserved_bucket
        object_key = initial.reserved_object_key
        upload_no_value = initial.upload_no
        purpose = initial.purpose
        declared_size = initial.max_size_bytes
        expected_types = tuple(initial.expected_mime_types)
        expected_sha256 = initial.expected_sha256
        actor_user_id = actor.context.user.id
        await self.session.rollback()

        metadata = await self.storage.stat(bucket, object_key)
        if metadata.size != declared_size:
            raise _unprocessable("FILE_SIZE_MISMATCH", "对象大小与创建上传会话时的声明不一致。")
        normalized_type = metadata.content_type.split(";", 1)[0].strip().lower()
        if normalized_type not in expected_types:
            raise _unprocessable("FILE_CONTENT_TYPE_MISMATCH", "对象 Content-Type 与声明不一致。")
        if (
            payload.provider_checksum
            and metadata.etag
            and payload.provider_checksum.strip('"') != metadata.etag.strip('"')
        ):
            raise _unprocessable(
                "FILE_PROVIDER_CHECKSUM_MISMATCH",
                "对象存储校验值与客户端提交值不一致。",
            )
        content = await self.storage.read(
            bucket,
            object_key,
            declared_size,
        )
        digest = hashlib.sha256(content).digest()
        if digest.hex() != payload.sha256.lower() or (
            expected_sha256 is not None and digest != expected_sha256
        ):
            raise _unprocessable("FILE_CHECKSUM_MISMATCH", "对象 SHA-256 与声明不一致。")
        extension = PurePath(object_key).suffix.lstrip(".")
        immutable_key = f"v1/{purpose}/{utc_now():%Y/%m}/{upload_no_value}/original.{extension}"
        immutable_metadata = await self.storage.put(
            _source_bucket(purpose), immutable_key, content, normalized_type
        )

        claim = await self.idempotency.begin(
            scope_key=f"file-upload-complete:{upload_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="file",
        )
        item = await self.repository.upload_session(upload_no, for_update=True)
        if item is None or item.uploader_user_id != actor_user_id:
            raise _not_found()
        if claim.replayed:
            return await self._session_view(item)
        _upload_can_complete(item)
        source = FileObject(
            file_no=new_prefixed_ulid("file_"),
            bucket=_source_bucket(item.purpose),
            object_key=immutable_key,
            purpose=item.purpose,
            owner_type=item.target_owner_type,
            owner_no=item.target_owner_no,
            upload_session_id=item.id,
            variant="original",
            declared_mime_type=normalized_type,
            detected_mime_type="application/octet-stream",
            size_bytes=metadata.size,
            sha256=digest,
            provider_checksum=immutable_metadata.etag,
            visibility="private",
            sensitivity_level="L2" if item.purpose == "store_certification" else "L1",
            scan_status="pending",
            file_status="scanning",
            storage_version_id=immutable_metadata.version_id,
        )
        self.session.add(source)
        # A direct PUT has no callback. Completion first records the externally
        # verified upload and then closes the session in the same transaction.
        item.upload_status = "uploaded"
        item.version += 1
        _outbox(
            self.session,
            "file.uploaded.v1",
            item.upload_no,
            item.version,
            {"purpose": item.purpose},
            aggregate_type="file_upload",
        )
        item.upload_status = "completed"
        item.completed_at = utc_now()
        item.version += 1
        await self.session.flush()
        _outbox(
            self.session,
            "file.scan_requested.v1",
            source.file_no,
            source.version,
            {"upload_id": item.upload_no, "purpose": item.purpose},
        )
        _outbox(
            self.session,
            "file.upload_completed.v1",
            item.upload_no,
            item.version,
            {"file_id": source.file_no, "purpose": item.purpose},
            aggregate_type="file_upload",
        )
        self.idempotency.complete(claim, response_status=200, resource_no=source.file_no)
        await self.session.commit()
        try:
            await self.storage.remove(bucket, object_key)
        except ApplicationError:
            # Bucket lifecycle removes any temporary object left behind. The
            # completed file always points at the immutable server-owned copy.
            pass
        return await self._session_view(item)

    async def abort_upload(self, actor: FileActor, upload_no: str) -> None:
        item = await self.repository.upload_session(upload_no, for_update=True)
        if item is None or item.uploader_user_id != actor.context.user.id:
            raise _not_found()
        if item.upload_status in {"aborted", "expired"}:
            return
        if item.upload_status == "completed":
            raise _conflict("FILE_UPLOAD_ALREADY_COMPLETED", "已完成的上传会话不能中止。")
        bucket, object_key = item.reserved_bucket, item.reserved_object_key
        try:
            await self.storage.remove(bucket, object_key)
        except ApplicationError as exc:
            if exc.status != 404:
                raise
        item.upload_status = "aborted"
        item.aborted_at = utc_now()
        item.version += 1
        await self.session.commit()

    async def file_metadata(self, actor: FileActor | None, file_no: str) -> FileMetadataView:
        file = await self.repository.file(file_no)
        if file is None or not await self._can_read(actor, file):
            raise _not_found()
        return _metadata_view(file)

    async def file_url(self, actor: FileActor | None, file_no: str) -> str:
        file = await self.repository.file(file_no)
        if file is None or not await self._can_read(actor, file):
            raise _not_found()
        if file.file_status != "active" or file.scan_status != "safe":
            raise _conflict("FILE_NOT_READY", "文件尚未完成安全处理。")
        return await self.storage.presign_get(
            file.bucket,
            file.object_key,
            timedelta(minutes=5 if file.visibility != "public_derivative" else 30),
        )

    async def _can_read(self, actor: FileActor | None, file: FileObject) -> bool:
        if (
            file.visibility == "public_derivative"
            and file.file_status == "active"
            and file.scan_status == "safe"
        ):
            return True
        if actor is None:
            return False
        if file.owner_type == "user" and file.owner_no == actor.context.user.user_no:
            return True
        if file.upload_session_id is not None:
            upload = await self.repository.upload_session_by_id(file.upload_session_id)
            if upload is not None and upload.uploader_user_id == actor.context.user.id:
                return True
        if actor.audience == "admin" and file.owner_type == "store":
            store = await self.repository.store(file.owner_no)
            if store is None:
                return False
            policy = upload_policy(file.purpose)
            permissions = await self.repository.actor_store_permissions(
                actor.context.user.id, store.id, policy.permissions
            )
            return bool(permissions)
        if actor.audience == "admin" and file.owner_type == "platform":
            policy = upload_policy(file.purpose)
            permissions = await self.repository.actor_scope_permissions(
                actor.context.user.id, "platform", 0, policy.permissions
            )
            return bool(permissions)
        return False

    async def _authorize_owner(
        self,
        actor: FileActor,
        policy: UploadPolicy,
        business_context_id: str | None,
    ) -> str:
        if policy.owner_type == "user":
            if actor.audience != "user":
                raise _not_found()
            return actor.context.user.user_no
        if policy.owner_type == "platform":
            if actor.audience != "admin":
                raise _not_found()
            permissions = await self.repository.actor_scope_permissions(
                actor.context.user.id, "platform", 0, policy.permissions
            )
            if not permissions:
                raise _not_found()
            return "platform"
        if actor.audience != "admin" or not business_context_id:
            raise _not_found()
        store = await self.repository.store(business_context_id)
        if store is None:
            raise _not_found()
        permissions = await self.repository.actor_store_permissions(
            actor.context.user.id, store.id, policy.permissions
        )
        if not permissions:
            raise _not_found()
        return store.store_no

    async def _session_view(
        self, item: FileUploadSession, *, include_upload: bool = False
    ) -> FileUploadSessionView:
        source = await self.repository.source_file(item.id)
        variants = await self.repository.variants(source.id) if source else []
        preferred = next((entry for entry in variants if entry.variant in {"w960", "w512"}), None)
        bindable = preferred
        if (
            bindable is None
            and source is not None
            and source.purpose in {"admin_import", "store_certification"}
            and source.file_status == "active"
            and source.scan_status == "safe"
        ):
            bindable = source
        instruction = None
        now = utc_now()
        if include_upload and item.upload_status == "created" and item.expires_at > now:
            await self.storage.ensure_bucket(item.reserved_bucket)
            instruction = FileUploadInstruction(
                method="PUT",
                url=await self.storage.presign_put(
                    item.reserved_bucket,
                    item.reserved_object_key,
                    max(item.expires_at - now, timedelta(seconds=1)),
                ),
                headers={"Content-Type": item.expected_mime_types[0]},
                expires_at=item.expires_at,
            )
        return FileUploadSessionView(
            upload_id=item.upload_no,
            purpose=item.purpose,
            owner_type=item.target_owner_type,
            owner_id=item.target_owner_no,
            upload_status=item.upload_status,
            expires_at=item.expires_at,
            upload=instruction,
            source_file=_variant_view(source) if source else None,
            bindable_file=_variant_view(bindable) if bindable else None,
            variants=[_variant_view(entry) for entry in variants],
        )


def _validate_declaration(policy: UploadPolicy, payload: FileUploadSessionCreateRequest) -> str:
    content_type = payload.content_type.split(";", 1)[0].strip().lower()
    extension = PurePath(payload.filename).suffix.lstrip(".").casefold()
    if content_type not in policy.allowed_mime_types or extension not in policy.allowed_extensions:
        raise _unprocessable("FILE_TYPE_NOT_ALLOWED", "文件类型不符合当前用途的上传策略。")
    if payload.size_bytes > policy.max_size_bytes:
        raise _unprocessable("FILE_SIZE_LIMIT_EXCEEDED", "文件大小超过当前用途的上限。")
    payload.content_type = content_type
    return extension


def _upload_can_complete(item: FileUploadSession) -> None:
    if item.upload_status not in {"created", "uploading", "uploaded"}:
        raise _conflict("FILE_UPLOAD_STATE_CONFLICT", "当前上传状态不能执行完成操作。")
    if item.expires_at <= utc_now():
        raise ApplicationError(
            status=410,
            code="FILE_UPLOAD_EXPIRED",
            title="Upload session expired",
            detail="上传会话已经过期，请重新创建。",
        )


def _source_bucket(purpose: str) -> str:
    if purpose == "store_certification":
        return "private-certifications"
    if purpose == "admin_import":
        return "admin-job-artifacts"
    return "private-image-sources"


def _variant_view(file: FileObject) -> FileVariantView:
    return FileVariantView(
        file_id=file.file_no,
        variant=file.variant,
        status=file.file_status,
        scan_status=file.scan_status,
        content_type=file.detected_mime_type,
        size_bytes=file.size_bytes,
        width=file.width,
        height=file.height,
        url=f"/api/v1/files/{file.file_no}"
        if file.visibility == "public_derivative" and file.file_status == "active"
        else None,
    )


def _metadata_view(file: FileObject) -> FileMetadataView:
    return FileMetadataView(
        **_variant_view(file).model_dump(),
        purpose=file.purpose,
        owner_type=file.owner_type,
        owner_id=file.owner_no,
        visibility=file.visibility,
    )


def _outbox(
    session: AsyncSession,
    event_type: str,
    aggregate_no: str,
    aggregate_version: int,
    payload: dict[str, object],
    *,
    aggregate_type: str = "file",
) -> None:
    request_id = request_id_context.get() or new_prefixed_ulid("req_")
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
            trace_id=request_id,
        )
    )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="未找到该资源。",
    )


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Resource conflict", detail=detail)


def _unprocessable(code: str, detail: str) -> ApplicationError:
    return ApplicationError(
        status=422, code=code, title="Request cannot be processed", detail=detail
    )
