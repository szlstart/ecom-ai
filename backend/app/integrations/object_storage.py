from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


class ObjectStorage(Protocol):
    async def put(self, *, key: str, content: AsyncIterator[bytes], content_type: str) -> None: ...

    async def delete(self, *, key: str) -> None: ...

    async def create_download_url(self, *, key: str, expires_seconds: int) -> str: ...


@dataclass(frozen=True, slots=True)
class ObjectStorageDisabledError(RuntimeError):
    message: str = "object storage is not configured"

    def __str__(self) -> str:
        return self.message


class DisabledObjectStorage:
    async def put(self, *, key: str, content: AsyncIterator[bytes], content_type: str) -> None:
        del key, content, content_type
        raise ObjectStorageDisabledError

    async def delete(self, *, key: str) -> None:
        del key
        raise ObjectStorageDisabledError

    async def create_download_url(self, *, key: str, expires_seconds: int) -> str:
        del key, expires_seconds
        raise ObjectStorageDisabledError
