from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.integrations.object_storage import ObjectMetadata, ObjectStorage
from app.modules.files.models import FileObject
from app.modules.files.policies import upload_policy
from app.modules.files.repository import FileRepository
from app.modules.files.scanner import (
    MalwareScanner,
    ProcessedImage,
    detect_private_content_type,
    process_public_image,
)
from app.modules.system.models import OutboxEvent

PROCESSOR_VERSION = "image-v1"


@dataclass(frozen=True)
class FileProcessingSource:
    id: int
    file_no: str
    bucket: str
    object_key: str
    purpose: str
    declared_mime_type: str
    size_bytes: int
    sha256: bytes


class FileProcessor:
    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        scanner: MalwareScanner,
    ) -> None:
        self.session = session
        self.storage = storage
        self.scanner = scanner
        self.repository = FileRepository(session)

    async def process_batch(self, limit: int = 10) -> int:
        candidates = await self.repository.scanning_files(limit)
        identities = [(item.id, item.version) for item in candidates]
        await self.session.rollback()
        processed = 0
        for file_id, version in identities:
            if await self.repository.claim_scan(file_id, version):
                await self.session.commit()
                await self._process_claimed(file_id)
                processed += 1
            else:
                await self.session.rollback()
        return processed

    async def expire_uploads(self, limit: int = 20) -> int:
        sessions = await self.repository.expired_upload_sessions(limit)
        expired = 0
        for item in sessions:
            try:
                await self.storage.remove(item.reserved_bucket, item.reserved_object_key)
            except ApplicationError as exc:
                if exc.status != 404:
                    await self.session.rollback()
                    return expired
            item.upload_status = "expired"
            item.version += 1
            _upload_event(self.session, item.upload_no, item.version)
            expired += 1
        await self.session.commit()
        return expired

    async def _process_claimed(self, file_id: int) -> None:
        source = await self.repository.file_by_id(file_id)
        if source is None or source.scan_status != "processing":
            await self.session.rollback()
            return
        policy = upload_policy(source.purpose)
        candidate = FileProcessingSource(
            id=source.id,
            file_no=source.file_no,
            bucket=source.bucket,
            object_key=source.object_key,
            purpose=source.purpose,
            declared_mime_type=source.declared_mime_type,
            size_bytes=source.size_bytes,
            sha256=source.sha256,
        )
        await self.session.rollback()
        try:
            payload = await self.storage.read(
                candidate.bucket, candidate.object_key, candidate.size_bytes
            )
            if hashlib.sha256(payload).digest() != candidate.sha256:
                raise ApplicationError(
                    status=422,
                    code="FILE_CHECKSUM_MISMATCH",
                    title="File checksum mismatch",
                    detail="文件内容在扫描前发生变化。",
                )
            await self.scanner.scan(payload)
            if policy.processor == "public_image":
                await self._activate_public_image(candidate, payload, policy.max_pixels or 0)
            else:
                detected = detect_private_content_type(payload, candidate.declared_mime_type)
                await self._activate_private(candidate.id, detected)
        except ApplicationError as exc:
            if exc.status == 503 or exc.retryable:
                await self._retry_later(candidate.id)
            else:
                await self._reject(candidate.id, exc.code)
        except Exception:
            await self._retry_later(candidate.id)

    async def _activate_public_image(
        self, source: FileProcessingSource, payload: bytes, max_pixels: int
    ) -> None:
        widths = (
            (64, 128, 256, 512)
            if source.purpose in {"store_logo", "brand_logo", "category_icon"}
            else (320, 640, 960, 1280)
        )
        result = process_public_image(payload, max_pixels, widths)
        uploaded: list[tuple[ProcessedImage, str, ObjectMetadata]] = []
        now = utc_now()
        for variant in result.variants:
            object_key = f"v1/{source.purpose}/{now:%Y/%m}/{source.file_no}/{variant.variant}.webp"
            metadata = await self.storage.put(
                "public-assets", object_key, variant.payload, variant.content_type
            )
            uploaded.append((variant, object_key, metadata))

        locked = await self.repository.file_by_id(source.id, for_update=True)
        if locked is None or locked.scan_status != "processing":
            await self.session.rollback()
            return
        existing = await self.repository.variants(locked.id)
        if not existing:
            for variant, object_key, metadata in uploaded:
                self.session.add(
                    FileObject(
                        file_no=new_prefixed_ulid("file_"),
                        bucket="public-assets",
                        object_key=object_key,
                        purpose=locked.purpose,
                        owner_type=locked.owner_type,
                        owner_no=locked.owner_no,
                        upload_session_id=locked.upload_session_id,
                        parent_file_id=locked.id,
                        variant=variant.variant,
                        processor_version=PROCESSOR_VERSION,
                        declared_mime_type=locked.declared_mime_type,
                        detected_mime_type=variant.content_type,
                        size_bytes=len(variant.payload),
                        sha256=variant.sha256,
                        provider_checksum=metadata.etag,
                        width=variant.width,
                        height=variant.height,
                        visibility="public_derivative",
                        sensitivity_level="L0",
                        scan_status="safe",
                        file_status="active",
                        storage_version_id=metadata.version_id,
                        activated_at=utc_now(),
                    )
                )
        locked.detected_mime_type = result.detected_content_type
        locked.width = result.width
        locked.height = result.height
        locked.scan_status = "safe"
        locked.file_status = "active"
        locked.activated_at = utc_now()
        locked.version += 1
        _event(
            self.session,
            "file.activated.v1",
            locked,
            {"variant_count": len(result.variants)},
        )
        await self.session.commit()

    async def _activate_private(self, file_id: int, detected: str) -> None:
        source = await self.repository.file_by_id(file_id, for_update=True)
        if source is None or source.scan_status != "processing":
            await self.session.rollback()
            return
        source.detected_mime_type = detected
        source.scan_status = "safe"
        source.file_status = "active"
        source.activated_at = utc_now()
        source.version += 1
        _event(self.session, "file.activated.v1", source, {"variant_count": 0})
        await self.session.commit()

    async def _retry_later(self, file_id: int) -> None:
        source = await self.repository.file_by_id(file_id, for_update=True)
        if source is not None and source.scan_status == "processing":
            source.scan_status = "pending"
            source.version += 1
            await self.session.commit()
        else:
            await self.session.rollback()

    async def _reject(self, file_id: int, reason_code: str) -> None:
        source = await self.repository.file_by_id(file_id, for_update=True)
        if source is None or source.scan_status != "processing":
            await self.session.rollback()
            return
        source.scan_status = "rejected"
        source.file_status = "rejected"
        source.version += 1
        _event(
            self.session,
            "file.rejected.v1",
            source,
            {"reason_code": reason_code},
        )
        await self.session.commit()


def _event(
    session: AsyncSession,
    event_type: str,
    source: FileObject,
    payload: dict[str, object],
) -> None:
    request_id = request_id_context.get() or new_prefixed_ulid("req_")
    session.add(
        OutboxEvent(
            event_no=new_prefixed_ulid("evt_"),
            event_type=event_type,
            aggregate_type="file",
            aggregate_no=source.file_no,
            aggregate_version=source.version,
            payload={"file_id": source.file_no, **payload},
            event_status="pending",
            available_at=utc_now(),
            trace_id=request_id,
        )
    )


def _upload_event(session: AsyncSession, upload_no: str, aggregate_version: int) -> None:
    request_id = request_id_context.get() or new_prefixed_ulid("req_")
    session.add(
        OutboxEvent(
            event_no=new_prefixed_ulid("evt_"),
            event_type="file.upload_expired.v1",
            aggregate_type="file_upload",
            aggregate_no=upload_no,
            aggregate_version=aggregate_version,
            payload={"upload_id": upload_no},
            event_status="pending",
            available_at=utc_now(),
            trace_id=request_id,
        )
    )
