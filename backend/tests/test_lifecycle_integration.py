import hashlib
import os
import secrets
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import utc_now
from app.database.mysql import mysql_session
from app.integrations.object_storage import StoredObject
from app.modules.checkout.models import CheckoutSession, CheckoutSnapshot
from app.modules.files.models import FileObject
from app.modules.files.reconciliation import FileGarbageCollector
from app.modules.identity.models import AuthSession, User
from app.modules.system.lifecycle import LifecycleProcessor
from app.modules.system.models import IdempotencyRecord

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


class RecordingStorage:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.removed: list[tuple[str, str]] = []

    async def list_objects(self, bucket: str, *, limit: int) -> list[StoredObject]:
        del bucket, limit
        return []

    async def remove(self, bucket: str, object_key: str) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("temporary object storage failure")
        self.removed.append((bucket, object_key))


async def test_transient_data_and_file_lifecycle_is_idempotent(client: AsyncClient) -> None:
    del client
    suffix = secrets.token_hex(5)
    now = utc_now()
    old = now - timedelta(days=45)
    settings = get_settings().model_copy(
        update={
            "checkout_retention_days": 30,
            "file_unreferenced_grace_days": 7,
            "lifecycle_batch_size": 100,
        }
    )

    async for session in mysql_session():
        user = await session.scalar(select(User).order_by(User.id).limit(1))
        assert user is not None
        auth_session = AuthSession(
            session_no=new_prefixed_ulid("ses_"),
            user_id=user.id,
            refresh_token_hash=hashlib.sha256(f"refresh-{suffix}".encode()).digest(),
            token_family_no=new_prefixed_ulid("tfa_"),
            device_no=new_prefixed_ulid("dev_"),
            device_name="Lifecycle test",
            client_type="web",
            audience="user",
            csrf_token_hash=hashlib.sha256(f"csrf-{suffix}".encode()).digest(),
            authenticated_at=old,
            authentication_methods=["password"],
            assurance_level="aal1",
            issued_at=old,
            expires_at=old,
            last_seen_at=old,
        )
        active_checkout = _checkout(user.id, suffix + "-active", "active", old, old)
        purge_checkout = _checkout(user.id, suffix + "-purge", "expired", old, old)
        expired_key = IdempotencyRecord(
            scope_key=f"lifecycle:{suffix}",
            idempotency_key=f"expired-{suffix}",
            request_hash=hashlib.sha256(b"expired").digest(),
            response_status=200,
            resource_type="test",
            expires_at=old,
        )
        orphan = _file(user.user_no, suffix + "-orphan", old)
        retry_file = _file(user.user_no, suffix + "-retry", old)
        kept_file = _file(user.user_no, suffix + "-kept", old)
        user.avatar_object_key = kept_file.object_key
        session.add_all(
            [
                auth_session,
                active_checkout,
                purge_checkout,
                expired_key,
                orphan,
                retry_file,
                kept_file,
            ]
        )
        await session.flush()
        session.add(
            CheckoutSnapshot(
                checkout_session_id=purge_checkout.id,
                snapshot_version=1,
                schema_version=1,
                snapshot_payload={"test": True},
                snapshot_hash=hashlib.sha256(f"snapshot-{suffix}".encode()).digest(),
            )
        )
        await session.commit()

        result = await LifecycleProcessor(session, settings).process_batch()
        assert result.revoked_sessions >= 1
        assert result.expired_checkouts >= 1
        assert result.purged_checkouts >= 1
        assert result.purged_idempotency_records >= 1
        assert auth_session.revoked_at is not None and auth_session.revoke_reason == "expired"
        assert active_checkout.checkout_status == "expired"
        assert await session.get(CheckoutSession, purge_checkout.id) is None
        assert await session.get(IdempotencyRecord, expired_key.id) is None

        storage = RecordingStorage()
        collected = await FileGarbageCollector(session, storage, settings).collect(10)
        assert collected.deleted_files == 2
        assert collected.retained_referenced_files == 1
        assert collected.failed_deletions == 0
        assert (orphan.bucket, orphan.object_key) in storage.removed
        assert orphan.file_status == "deleted" and orphan.deleted_at is not None
        assert kept_file.file_status == "active" and kept_file.reference_count == 1
        second_gc = await FileGarbageCollector(session, storage, settings).collect(10)
        assert second_gc.deleted_files == 0

        retrying = _file(user.user_no, suffix + "-failure", old)
        session.add(retrying)
        await session.commit()
        flaky = RecordingStorage(fail_once=True)
        failed = await FileGarbageCollector(session, flaky, settings).collect(10)
        assert failed.failed_deletions == 1
        assert retrying.file_status == "deleting"
        recovered = await FileGarbageCollector(session, flaky, settings).collect(10)
        assert recovered.deleted_files == 1
        assert retrying.file_status == "deleted"

        stale = IdempotencyRecord(
            scope_key=f"reuse:{suffix}",
            idempotency_key=f"reuse-{suffix}",
            request_hash=hashlib.sha256(b"old-payload").digest(),
            response_status=200,
            resource_type="test",
            expires_at=old,
        )
        session.add(stale)
        await session.commit()
        claim = await IdempotencyService(session).begin(
            scope_key=stale.scope_key,
            idempotency_key=stale.idempotency_key,
            payload={"new": True},
            resource_type="test",
        )
        assert claim.replayed is False
        assert claim.record.id != stale.id
        await session.rollback()
        break


def _checkout(
    user_id: int,
    suffix: str,
    status: str,
    expires_at: datetime,
    updated_at: datetime,
) -> CheckoutSession:
    digest = hashlib.sha256(suffix.encode()).digest()
    return CheckoutSession(
        checkout_no=new_prefixed_ulid("chk_"),
        user_id=user_id,
        source_type="buy_now",
        checkout_status=status,
        goods_amount=100,
        freight_amount=0,
        payable_amount=100,
        currency="CNY",
        pricing_version="pricing_v1",
        snapshot_hash=digest,
        expires_at=expires_at,
        created_at=updated_at,
        updated_at=updated_at,
    )


def _file(owner_no: str, suffix: str, activated_at: datetime) -> FileObject:
    return FileObject(
        file_no=new_prefixed_ulid("file_"),
        bucket="private-image-sources",
        object_key=f"tests/lifecycle/{suffix}.webp",
        purpose="user_avatar",
        owner_type="user",
        owner_no=owner_no,
        variant="original",
        declared_mime_type="image/webp",
        detected_mime_type="image/webp",
        size_bytes=128,
        sha256=hashlib.sha256(suffix.encode()).digest(),
        width=10,
        height=10,
        visibility="private",
        sensitivity_level="L1",
        scan_status="safe",
        file_status="active",
        reference_count=0,
        activated_at=activated_at,
        created_at=activated_at,
        updated_at=activated_at,
    )
