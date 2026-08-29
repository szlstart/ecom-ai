from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.integrations.object_storage import StoredObject
from app.modules.files.models import (
    FileObject,
    FileReconciliationFinding,
    FileUploadSession,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_MANAGED_BUCKETS = {
    "temporary-uploads",
    "private-image-sources",
    "public-assets",
    "private-certifications",
    "admin-job-artifacts",
}
_OBJECT_KEY_REFERENCES = (
    ("users", "avatar_object_key"),
    ("stores", "logo_object_key"),
    ("store_certifications", "evidence_object_key"),
    ("categories", "icon_object_key"),
    ("brands", "logo_object_key"),
    ("order_items", "image_object_key"),
    ("review_images", "object_key"),
    ("review_append_images", "object_key"),
)


class ObjectInventoryStorage(Protocol):
    async def list_objects(self, bucket: str, *, limit: int) -> list[StoredObject]: ...

    async def remove(self, bucket: str, object_key: str) -> None: ...


@dataclass(frozen=True)
class ReconciliationResult:
    metadata_files: int
    storage_objects: int
    missing_objects: int
    orphan_objects: int
    reference_mismatches: int
    deleted_expired_orphans: int


@dataclass(frozen=True)
class FileGarbageCollectionResult:
    deleted_files: int
    retained_referenced_files: int
    failed_deletions: int


class FileReconciler:
    """Bidirectionally reconciles MySQL file metadata and object storage.

    Orphans are only deleted after a durable quarantine period. Metadata rows
    whose object is missing are retained and marked missing for restoration and
    audit; they are never silently dropped.
    """

    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectInventoryStorage,
        settings: Settings,
    ) -> None:
        self.session = session
        self.storage = storage
        self.settings = settings

    async def reconcile(self) -> ReconciliationResult:
        now = utc_now()
        files = list(
            (
                await self.session.scalars(
                    select(FileObject).where(FileObject.file_status != "deleted")
                )
            ).all()
        )
        active_uploads = list(
            (
                await self.session.scalars(
                    select(FileUploadSession).where(
                        FileUploadSession.upload_status.in_(("created", "uploading", "uploaded"))
                    )
                )
            ).all()
        )
        buckets = _MANAGED_BUCKETS | {item.bucket for item in files}
        stored: dict[tuple[str, str], StoredObject] = {}
        remaining = self.settings.file_reconciliation_max_objects
        for bucket in sorted(buckets):
            if remaining <= 0:
                raise RuntimeError("object storage reconciliation limit exceeded")
            objects = await self.storage.list_objects(bucket, limit=remaining)
            remaining -= len(objects)
            stored.update({(item.bucket, item.object_key): item for item in objects})

        known = {(item.bucket, item.object_key) for item in files}
        known.update((item.reserved_bucket, item.reserved_object_key) for item in active_uploads)
        references = await self.reference_counts()
        encountered: set[bytes] = set()
        missing_count = 0
        mismatch_count = 0

        for item in files:
            storage_key = (item.bucket, item.object_key)
            actual_count = references.get(item.id, 0)
            if item.reference_count != actual_count:
                mismatch_count += 1
                finding = await self._record(
                    "reference_count_mismatch",
                    item.bucket,
                    item.object_key,
                    item.file_no,
                    expected=item.reference_count,
                    actual=actual_count,
                    status="open",
                    now=now,
                )
                encountered.add(finding.finding_key)
                item.reference_count = actual_count
                item.version += 1
            if item.file_status == "deleting":
                continue
            if storage_key not in stored:
                missing_count += 1
                finding = await self._record(
                    "metadata_without_object",
                    item.bucket,
                    item.object_key,
                    item.file_no,
                    expected=None,
                    actual=None,
                    status="open",
                    now=now,
                )
                encountered.add(finding.finding_key)
                if item.file_status != "missing":
                    item.file_status = "missing"
                    item.version += 1
            elif item.file_status == "missing":
                item.file_status = "active" if item.scan_status == "safe" else "scanning"
                item.version += 1

        orphan_count = 0
        deleted_orphans = 0
        for bucket, object_key in sorted(set(stored) - known):
            orphan_count += 1
            finding = await self._record(
                "object_without_metadata",
                bucket,
                object_key,
                None,
                expected=None,
                actual=None,
                status="quarantined",
                now=now,
            )
            encountered.add(finding.finding_key)
            if finding.quarantine_until is not None and finding.quarantine_until <= now:
                await self.storage.remove(bucket, object_key)
                finding.finding_status = "resolved"
                finding.resolved_at = now
                finding.resolution_code = "orphan_deleted_after_grace"
                finding.version += 1
                encountered.discard(finding.finding_key)
                deleted_orphans += 1

        await self._resolve_absent(encountered, now)
        await self.session.commit()
        return ReconciliationResult(
            metadata_files=len(files),
            storage_objects=len(stored),
            missing_objects=missing_count,
            orphan_objects=orphan_count,
            reference_mismatches=mismatch_count,
            deleted_expired_orphans=deleted_orphans,
        )

    async def reference_counts(self) -> dict[int, int]:
        counts: defaultdict[int, int] = defaultdict(int)
        relations = (
            (
                await self.session.execute(
                    text(
                        """SELECT TABLE_NAME, COLUMN_NAME
                        FROM information_schema.KEY_COLUMN_USAGE
                        WHERE TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_SCHEMA=DATABASE()
                        AND REFERENCED_TABLE_NAME='file_objects'
                        AND REFERENCED_COLUMN_NAME='id'"""
                    )
                )
            )
            .tuples()
            .all()
        )
        for table_name, column_name in relations:
            table, column = str(table_name), str(column_name)
            if not _IDENTIFIER.fullmatch(table) or not _IDENTIFIER.fullmatch(column):
                raise RuntimeError("unsafe file reference identifier")
            rows = (
                await self.session.execute(
                    text(
                        # Identifiers are information_schema values validated by _IDENTIFIER.
                        f"SELECT `{column}`, COUNT(*) FROM `{table}` "  # nosec B608
                        f"WHERE `{column}` IS NOT NULL GROUP BY `{column}`"
                    )
                )
            ).tuples()
            for file_id, count in rows:
                counts[int(file_id)] += int(count)

        key_rows = await self.session.execute(
            select(FileObject.object_key, FileObject.id).where(FileObject.file_status != "deleted")
        )
        key_to_id: dict[str, int] = {
            str(object_key): int(file_id) for object_key, file_id in key_rows.tuples()
        }
        for table, column in _OBJECT_KEY_REFERENCES:
            rows = (
                await self.session.execute(
                    text(
                        # Identifiers come from the immutable module allowlist.
                        f"SELECT `{column}`, COUNT(*) FROM `{table}` "  # nosec B608
                        f"WHERE `{column}` IS NOT NULL GROUP BY `{column}`"
                    )
                )
            ).tuples()
            for object_key, count in rows:
                file_id = key_to_id.get(str(object_key))
                if file_id is not None:
                    counts[int(file_id)] += int(count)
        return dict(counts)

    async def _record(
        self,
        finding_type: str,
        bucket: str,
        object_key: str,
        file_no: str | None,
        *,
        expected: int | None,
        actual: int | None,
        status: str,
        now: datetime,
    ) -> FileReconciliationFinding:
        finding_key = hashlib.sha256(f"{finding_type}\0{bucket}\0{object_key}".encode()).digest()
        finding = await self.session.scalar(
            select(FileReconciliationFinding)
            .where(FileReconciliationFinding.finding_key == finding_key)
            .with_for_update()
        )
        if finding is None:
            finding = FileReconciliationFinding(
                finding_no=new_prefixed_ulid("frf_"),
                finding_key=finding_key,
                finding_type=finding_type,
                bucket=bucket,
                object_key=object_key,
                file_no=file_no,
                expected_reference_count=expected,
                actual_reference_count=actual,
                finding_status=status,
                first_seen_at=now,
                last_seen_at=now,
                quarantine_until=(
                    now + timedelta(days=self.settings.file_orphan_grace_days)
                    if status == "quarantined"
                    else None
                ),
            )
            self.session.add(finding)
            await self.session.flush()
            return finding
        finding.file_no = file_no
        finding.expected_reference_count = expected
        finding.actual_reference_count = actual
        finding.finding_status = status
        finding.last_seen_at = now
        finding.resolved_at = None
        finding.resolution_code = None
        finding.version += 1
        return finding

    async def _resolve_absent(self, encountered: set[bytes], now: datetime) -> None:
        findings = list(
            (
                await self.session.scalars(
                    select(FileReconciliationFinding)
                    .where(FileReconciliationFinding.finding_status.in_(("open", "quarantined")))
                    .with_for_update()
                )
            ).all()
        )
        for finding in findings:
            if finding.finding_key in encountered:
                continue
            finding.finding_status = "resolved"
            finding.resolved_at = now
            finding.resolution_code = "reconciled"
            finding.version += 1


class FileGarbageCollector:
    """Deletes unreferenced objects through a durable, retryable tombstone state."""

    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectInventoryStorage,
        settings: Settings,
    ) -> None:
        self.session = session
        self.storage = storage
        self.settings = settings

    async def collect(self, limit: int = 100) -> FileGarbageCollectionResult:
        now = utc_now()
        cutoff = now - timedelta(days=self.settings.file_unreferenced_grace_days)
        candidates = list(
            (
                await self.session.scalars(
                    select(FileObject)
                    .where(
                        or_(
                            FileObject.file_status == "deleting",
                            (
                                (FileObject.file_status == "active")
                                & (FileObject.reference_count == 0)
                                & (
                                    func.coalesce(FileObject.activated_at, FileObject.created_at)
                                    <= cutoff
                                )
                            ),
                        )
                    )
                    .order_by(FileObject.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        if not candidates:
            return FileGarbageCollectionResult(0, 0, 0)

        references = await FileReconciler(
            self.session, self.storage, self.settings
        ).reference_counts()
        retained = 0
        pending: list[tuple[int, str, str]] = []
        for item in candidates:
            actual_count = references.get(item.id, 0)
            if actual_count > 0:
                item.reference_count = actual_count
                if item.file_status == "deleting":
                    item.file_status = "active"
                item.version += 1
                retained += 1
                continue
            item.reference_count = 0
            item.file_status = "deleting"
            item.version += 1
            pending.append((item.id, item.bucket, item.object_key))
        await self.session.commit()

        deleted = 0
        failed = 0
        for file_id, bucket, object_key in pending:
            try:
                await self.storage.remove(bucket, object_key)
            except Exception as exc:
                if getattr(exc, "code", None) != "OBJECT_STORAGE_OBJECT_NOT_FOUND":
                    failed += 1
                    continue
            locked = await self.session.scalar(
                select(FileObject).where(FileObject.id == file_id).with_for_update()
            )
            if locked is None or locked.file_status == "deleted":
                continue
            if locked.file_status != "deleting" or locked.reference_count != 0:
                retained += 1
                await self.session.commit()
                continue
            locked.file_status = "deleted"
            locked.deleted_at = now
            locked.expires_at = None
            locked.version += 1
            await self.session.commit()
            deleted += 1
        return FileGarbageCollectionResult(deleted, retained, failed)
