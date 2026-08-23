from __future__ import annotations

from typing import cast

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utc_now
from app.modules.files.models import FileObject
from app.modules.identity.models import User
from app.modules.rbac.repository import RbacRepository
from app.modules.stores.models import Store
from app.modules.system.models import AdminBatchJob, AdminBatchJobItem


class BatchJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def job_by_no(self, job_no: str, *, for_update: bool = False) -> AdminBatchJob | None:
        statement = select(AdminBatchJob).where(AdminBatchJob.job_no == job_no)
        if for_update:
            statement = statement.with_for_update()
        return cast(AdminBatchJob | None, await self.session.scalar(statement))

    async def jobs(
        self,
        scopes: tuple[tuple[str, int], ...],
        *,
        job_type: str | None,
        job_status: str | None,
        cursor_no: str | None,
        limit: int,
    ) -> list[AdminBatchJob]:
        statement = select(AdminBatchJob)
        if ("platform", 0) not in scopes:
            scope_filters = [
                and_(AdminBatchJob.scope_type == scope_type, AdminBatchJob.scope_id == scope_id)
                for scope_type, scope_id in scopes
            ]
            if not scope_filters:
                return []
            statement = statement.where(or_(*scope_filters))
        if job_type:
            statement = statement.where(AdminBatchJob.job_type == job_type)
        if job_status:
            statement = statement.where(AdminBatchJob.job_status == job_status)
        if cursor_no:
            statement = statement.where(AdminBatchJob.job_no < cursor_no)
        return list(
            (
                await self.session.scalars(
                    statement.order_by(AdminBatchJob.job_no.desc()).limit(limit + 1)
                )
            ).all()
        )

    async def items(
        self,
        job_id: int,
        *,
        item_status: str | None,
        cursor_id: int | None,
        limit: int,
    ) -> list[AdminBatchJobItem]:
        statement = select(AdminBatchJobItem).where(AdminBatchJobItem.job_id == job_id)
        if item_status:
            statement = statement.where(AdminBatchJobItem.item_status == item_status)
        if cursor_id is not None:
            statement = statement.where(AdminBatchJobItem.id > cursor_id)
        return list(
            (
                await self.session.scalars(
                    statement.order_by(AdminBatchJobItem.id).limit(limit + 1)
                )
            ).all()
        )

    async def store_by_no(self, store_no: str) -> Store | None:
        return cast(
            Store | None, await self.session.scalar(select(Store).where(Store.store_no == store_no))
        )

    async def store_by_id(self, store_id: int) -> Store | None:
        return cast(Store | None, await self.session.get(Store, store_id))

    async def file_by_no(self, file_no: str) -> FileObject | None:
        return cast(
            FileObject | None,
            await self.session.scalar(select(FileObject).where(FileObject.file_no == file_no)),
        )

    async def file_by_id(self, file_id: int | None) -> FileObject | None:
        if file_id is None:
            return None
        return cast(FileObject | None, await self.session.get(FileObject, file_id))

    async def requester_is_authorized(self, job: AdminBatchJob) -> bool:
        user = cast(User | None, await self.session.get(User, job.requested_by))
        if user is None or user.deleted_at is not None or user.user_status != "active":
            return False
        rows = await RbacRepository(self.session).permissions_for_user(user.id, utc_now())
        return any(
            permission.permission_code == job.permission_code
            and (
                (grant.scope_type, grant.scope_id) == ("platform", 0)
                or (grant.scope_type, grant.scope_id) == (job.scope_type, job.scope_id)
            )
            for permission, grant, _role in rows
        )

    async def actor_has_job_permission(self, user_id: int, job: AdminBatchJob) -> bool:
        rows = await RbacRepository(self.session).permissions_for_user(user_id, utc_now())
        return any(
            permission.permission_code == job.permission_code
            and (
                (grant.scope_type, grant.scope_id) == ("platform", 0)
                or (grant.scope_type, grant.scope_id) == (job.scope_type, job.scope_id)
            )
            for permission, grant, _role in rows
        )

    async def next_job(self, statuses: tuple[str, ...]) -> AdminBatchJob | None:
        return cast(
            AdminBatchJob | None,
            await self.session.scalar(
                select(AdminBatchJob)
                .where(AdminBatchJob.job_status.in_(statuses))
                .order_by(AdminBatchJob.created_at, AdminBatchJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ),
        )

    async def item_by_key(self, job_id: int, item_key: str) -> AdminBatchJobItem | None:
        return cast(
            AdminBatchJobItem | None,
            await self.session.scalar(
                select(AdminBatchJobItem).where(
                    AdminBatchJobItem.job_id == job_id,
                    AdminBatchJobItem.item_key == item_key,
                )
            ),
        )
