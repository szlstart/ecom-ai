from __future__ import annotations

from typing import cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utc_now
from app.modules.files.models import FileObject, FileUploadSession
from app.modules.identity.models import User
from app.modules.rbac.repository import RbacRepository
from app.modules.stores.models import Store


class FileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def store(self, store_no: str) -> Store | None:
        return cast(
            Store | None, await self.session.scalar(select(Store).where(Store.store_no == store_no))
        )

    async def user(self, user_no: str) -> User | None:
        return cast(
            User | None, await self.session.scalar(select(User).where(User.user_no == user_no))
        )

    async def actor_store_permissions(
        self, user_id: int, store_id: int, codes: tuple[str, ...]
    ) -> set[str]:
        return await self.actor_scope_permissions(user_id, "store", store_id, codes)

    async def actor_scope_permissions(
        self,
        user_id: int,
        scope_type: str,
        scope_id: int,
        codes: tuple[str, ...],
    ) -> set[str]:
        rows = await RbacRepository(self.session).permissions_for_user(user_id, utc_now())
        return {
            permission.permission_code
            for permission, grant, _role in rows
            if permission.permission_code in codes
            and ((grant.scope_type, grant.scope_id) in {("platform", 0), (scope_type, scope_id)})
        }

    async def upload_session(
        self, upload_no: str, *, for_update: bool = False
    ) -> FileUploadSession | None:
        statement = select(FileUploadSession).where(FileUploadSession.upload_no == upload_no)
        if for_update:
            statement = statement.with_for_update()
        return cast(FileUploadSession | None, await self.session.scalar(statement))

    async def upload_session_by_id(self, upload_id: int) -> FileUploadSession | None:
        return cast(
            FileUploadSession | None,
            await self.session.scalar(
                select(FileUploadSession).where(FileUploadSession.id == upload_id)
            ),
        )

    async def source_file(self, upload_session_id: int) -> FileObject | None:
        return cast(
            FileObject | None,
            await self.session.scalar(
                select(FileObject).where(
                    FileObject.upload_session_id == upload_session_id,
                    FileObject.parent_file_id.is_(None),
                )
            ),
        )

    async def file(self, file_no: str) -> FileObject | None:
        return cast(
            FileObject | None,
            await self.session.scalar(select(FileObject).where(FileObject.file_no == file_no)),
        )

    async def variants(self, parent_file_id: int) -> list[FileObject]:
        return list(
            (
                await self.session.scalars(
                    select(FileObject)
                    .where(FileObject.parent_file_id == parent_file_id)
                    .order_by(FileObject.width.desc(), FileObject.id)
                )
            ).all()
        )

    async def scanning_files(self, limit: int) -> list[FileObject]:
        return list(
            (
                await self.session.scalars(
                    select(FileObject)
                    .where(
                        FileObject.parent_file_id.is_(None),
                        FileObject.file_status == "scanning",
                        FileObject.scan_status == "pending",
                    )
                    .order_by(FileObject.created_at, FileObject.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )

    async def expired_upload_sessions(self, limit: int) -> list[FileUploadSession]:
        return list(
            (
                await self.session.scalars(
                    select(FileUploadSession)
                    .where(
                        FileUploadSession.upload_status.in_(("created", "uploading", "uploaded")),
                        FileUploadSession.expires_at <= utc_now(),
                    )
                    .order_by(FileUploadSession.expires_at, FileUploadSession.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )

    async def claim_scan(self, file_id: int, version: int) -> bool:
        result = cast(
            CursorResult[tuple[()]],
            await self.session.execute(
                update(FileObject)
                .where(
                    FileObject.id == file_id,
                    FileObject.version == version,
                    FileObject.file_status == "scanning",
                    FileObject.scan_status == "pending",
                )
                .values(scan_status="processing", version=FileObject.version + 1)
            ),
        )
        return result.rowcount == 1

    async def file_by_id(self, file_id: int, *, for_update: bool = False) -> FileObject | None:
        statement = select(FileObject).where(FileObject.id == file_id)
        if for_update:
            statement = statement.with_for_update()
        return cast(FileObject | None, await self.session.scalar(statement))
