from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Protocol
from urllib.parse import urlparse

from anyio import to_thread
from minio import Minio
from minio.error import S3Error

from app.core.config import Settings, get_settings
from app.core.exceptions import ApplicationError


@dataclass(frozen=True)
class ObjectMetadata:
    size: int
    content_type: str
    etag: str | None
    version_id: str | None
    metadata: dict[str, str]


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    object_key: str
    size: int
    etag: str | None
    last_modified: datetime | None


class ObjectStorage(Protocol):
    async def probe(self) -> None: ...

    async def ensure_bucket(self, bucket: str) -> None: ...

    async def presign_put(self, bucket: str, object_key: str, expires: timedelta) -> str: ...

    async def presign_get(self, bucket: str, object_key: str, expires: timedelta) -> str: ...

    async def stat(self, bucket: str, object_key: str) -> ObjectMetadata: ...

    async def read(self, bucket: str, object_key: str, max_bytes: int) -> bytes: ...

    async def put(
        self, bucket: str, object_key: str, data: bytes, content_type: str
    ) -> ObjectMetadata: ...

    async def remove(self, bucket: str, object_key: str) -> None: ...


class MinioObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.internal = _client(settings.object_storage_endpoint, settings)
        self.public = _client(settings.object_storage_public_endpoint, settings)

    async def probe(self) -> None:
        bucket = self._physical_bucket("temporary-uploads")
        await self._call(lambda: self.internal.bucket_exists(bucket))

    async def ensure_bucket(self, bucket: str) -> None:
        physical_bucket = self._physical_bucket(bucket)

        def operation() -> None:
            if not self.internal.bucket_exists(physical_bucket):
                self.internal.make_bucket(
                    physical_bucket, location=self.settings.object_storage_region
                )

        await self._call(operation)

    async def presign_put(self, bucket: str, object_key: str, expires: timedelta) -> str:
        physical_bucket = self._physical_bucket(bucket)
        return await self._call(
            lambda: self.public.presigned_put_object(
                physical_bucket, object_key, expires=expires
            )
        )

    async def presign_get(self, bucket: str, object_key: str, expires: timedelta) -> str:
        physical_bucket = self._physical_bucket(bucket)
        return await self._call(
            lambda: self.public.presigned_get_object(
                physical_bucket, object_key, expires=expires
            )
        )

    async def stat(self, bucket: str, object_key: str) -> ObjectMetadata:
        physical_bucket = self._physical_bucket(bucket)
        item = await self._call(
            lambda: self.internal.stat_object(physical_bucket, object_key)
        )
        if item.size is None:
            raise _unavailable()
        return ObjectMetadata(
            size=int(item.size),
            content_type=str(item.content_type or "application/octet-stream"),
            etag=str(item.etag) if item.etag else None,
            version_id=str(item.version_id) if item.version_id else None,
            metadata={str(key).lower(): str(value) for key, value in (item.metadata or {}).items()},
        )

    async def read(self, bucket: str, object_key: str, max_bytes: int) -> bytes:
        physical_bucket = self._physical_bucket(bucket)

        def operation() -> bytes:
            response = self.internal.get_object(physical_bucket, object_key)
            try:
                payload = response.read(max_bytes + 1)
            finally:
                response.close()
                response.release_conn()
            if len(payload) > max_bytes:
                raise ApplicationError(
                    status=422,
                    code="FILE_SIZE_LIMIT_EXCEEDED",
                    title="File size limit exceeded",
                    detail="上传文件超过允许的大小。",
                )
            return bytes(payload)

        return await self._call(operation)

    async def put(
        self, bucket: str, object_key: str, data: bytes, content_type: str
    ) -> ObjectMetadata:
        await self.ensure_bucket(bucket)
        physical_bucket = self._physical_bucket(bucket)

        def operation() -> ObjectMetadata:
            result = self.internal.put_object(
                physical_bucket,
                object_key,
                io.BytesIO(data),
                len(data),
                content_type=content_type,
            )
            return ObjectMetadata(
                size=len(data),
                content_type=content_type,
                etag=str(result.etag) if result.etag else None,
                version_id=str(result.version_id) if result.version_id else None,
                metadata={},
            )

        return await self._call(operation)

    async def remove(self, bucket: str, object_key: str) -> None:
        physical_bucket = self._physical_bucket(bucket)
        await self._call(
            lambda: self.internal.remove_object(physical_bucket, object_key)
        )

    async def list_objects(self, bucket: str, *, limit: int) -> list[StoredObject]:
        physical_bucket = self._physical_bucket(bucket)

        def operation() -> list[StoredObject]:
            if not self.internal.bucket_exists(physical_bucket):
                return []
            result: list[StoredObject] = []
            for item in self.internal.list_objects(physical_bucket, recursive=True):
                if len(result) >= limit:
                    raise RuntimeError("object storage inventory exceeds reconciliation limit")
                result.append(
                    StoredObject(
                        bucket=bucket,
                        object_key=str(item.object_name),
                        size=int(item.size or 0),
                        etag=str(item.etag) if item.etag else None,
                        last_modified=item.last_modified,
                    )
                )
            return result

        return await self._call(operation)

    def _physical_bucket(self, bucket: str) -> str:
        return f"{self.settings.object_storage_bucket_prefix}{bucket}"

    async def _call[ResultT](self, operation: Callable[[], ResultT]) -> ResultT:
        try:
            return await to_thread.run_sync(operation)
        except ApplicationError:
            raise
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                raise ApplicationError(
                    status=404,
                    code="OBJECT_STORAGE_OBJECT_NOT_FOUND",
                    title="Stored object not found",
                    detail="对象存储中未找到对应文件。",
                ) from exc
            raise _unavailable() from exc
        except Exception as exc:
            raise _unavailable() from exc


def _client(endpoint: str, settings: Settings) -> Minio:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ValueError("object storage endpoint must be an http(s) origin without a path")
    return Minio(
        parsed.netloc,
        access_key=settings.object_storage_access_key,
        secret_key=settings.object_storage_secret_key,
        secure=parsed.scheme == "https",
        region=settings.object_storage_region,
    )


def _unavailable() -> ApplicationError:
    return ApplicationError(
        status=503,
        code="OBJECT_STORAGE_UNAVAILABLE",
        title="Object storage unavailable",
        detail="文件服务暂时不可用，请稍后重试。",
        retryable=True,
    )


@lru_cache
def get_object_storage() -> ObjectStorage:
    settings = get_settings()
    if not settings.object_storage_enabled:
        raise _unavailable()
    if not settings.object_storage_access_key or not settings.object_storage_secret_key:
        raise RuntimeError("object storage credentials are required when storage is enabled")
    return MinioObjectStorage(settings)
