from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.exceptions import ApplicationError
from app.integrations.object_storage import ObjectMetadata
from app.modules.finance.account_deletion import selected_ids, storage_objects
from app.workers.account_deletion_worker import _remove_objects


class FakeStorage:
    def __init__(self, missing: set[tuple[str, str]] | None = None) -> None:
        self.missing = missing or set()
        self.removed: list[tuple[str, str]] = []

    async def ensure_bucket(self, bucket: str) -> None:
        del bucket

    async def presign_put(self, bucket: str, object_key: str, expires: timedelta) -> str:
        del bucket, object_key, expires
        return "https://invalid.test/put"

    async def presign_get(self, bucket: str, object_key: str, expires: timedelta) -> str:
        del bucket, object_key, expires
        return "https://invalid.test/get"

    async def stat(self, bucket: str, object_key: str) -> ObjectMetadata:
        del bucket, object_key
        raise NotImplementedError

    async def read(self, bucket: str, object_key: str, max_bytes: int) -> bytes:
        del bucket, object_key, max_bytes
        raise NotImplementedError

    async def put(
        self, bucket: str, object_key: str, data: bytes, content_type: str
    ) -> ObjectMetadata:
        del bucket, object_key, data, content_type
        raise NotImplementedError

    async def remove(self, bucket: str, object_key: str) -> None:
        if (bucket, object_key) in self.missing:
            raise ApplicationError(
                status=404,
                code="OBJECT_STORAGE_OBJECT_NOT_FOUND",
                title="missing",
                detail="missing",
            )
        self.removed.append((bucket, object_key))


def test_account_deletion_inventory_rejects_unsafe_or_invalid_mysql_rows() -> None:
    with pytest.raises(ValueError):
        selected_ids({"mysql_ids": {"users`; DROP TABLE users": [1]}})
    with pytest.raises(ValueError):
        selected_ids({"mysql_ids": {"users": [True]}})


def test_account_deletion_inventory_parses_storage_locations() -> None:
    assert storage_objects(
        {"storage_objects": [{"bucket": "public-assets", "object_key": "v1/a.webp"}]}
    ) == [("public-assets", "v1/a.webp")]


@pytest.mark.asyncio
async def test_account_deletion_object_cleanup_is_idempotent_for_missing_objects() -> None:
    storage = FakeStorage(missing={("private", "already-gone")})
    await _remove_objects(
        storage,
        {
            "storage_objects": [
                {"bucket": "private", "object_key": "already-gone"},
                {"bucket": "public-assets", "object_key": "still-present"},
            ]
        },
    )
    assert storage.removed == [("public-assets", "still-present")]


@pytest.mark.asyncio
async def test_account_deletion_does_not_drop_inventory_when_storage_is_disabled() -> None:
    with pytest.raises(RuntimeError, match="object storage is disabled"):
        await _remove_objects(
            None,
            {"storage_objects": [{"bucket": "private", "object_key": "must-delete"}]},
        )
