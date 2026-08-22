from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.files.models import FileObject
from app.modules.identity.models import User
from app.modules.stores.models import (
    Store,
    StoreCertification,
    StoreCertificationEvent,
    StoreServicePolicy,
)


class AdminStoreRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def stores(
        self,
        scopes: tuple[tuple[str, int], ...],
        *,
        status: str | None,
        q: str | None,
        cursor: str | None,
        limit: int,
    ) -> list[tuple[Store, User]]:
        statement = select(Store, User).join(User, User.id == Store.owner_user_id)
        if ("platform", 0) not in scopes:
            store_ids = [scope_id for scope_type, scope_id in scopes if scope_type == "store"]
            if not store_ids:
                return []
            statement = statement.where(Store.id.in_(store_ids))
        if status:
            statement = statement.where(Store.store_status == status)
        if q:
            term = f"%{_escape_like(q)}%"
            statement = statement.where(Store.store_name.like(term, escape="\\"))
        if cursor:
            statement = statement.where(Store.store_no > cursor)
        rows = (
            await self.session.execute(statement.order_by(Store.store_no).limit(limit + 1))
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def store_by_no(
        self, store_no: str, *, for_update: bool = False
    ) -> tuple[Store, User] | None:
        statement = (
            select(Store, User)
            .join(User, User.id == Store.owner_user_id)
            .where(Store.store_no == store_no)
        )
        if for_update:
            statement = statement.with_for_update(of=Store)
        row = (await self.session.execute(statement)).one_or_none()
        return None if row is None else (row[0], row[1])

    async def approved_certification_exists(self, store_id: int, now: datetime) -> bool:
        count = await self.session.scalar(
            select(func.count(StoreCertification.id)).where(
                StoreCertification.store_id == store_id,
                StoreCertification.review_status == "approved",
                or_(
                    StoreCertification.valid_from.is_(None),
                    StoreCertification.valid_from <= now.date(),
                ),
                or_(
                    StoreCertification.valid_until.is_(None),
                    StoreCertification.valid_until >= now.date(),
                ),
            )
        )
        return bool(count)

    async def certifications(
        self,
        scopes: tuple[tuple[str, int], ...],
        *,
        review_status: str | None,
        cursor: str | None,
        limit: int,
    ) -> list[tuple[StoreCertification, Store]]:
        statement = select(StoreCertification, Store).join(
            Store, Store.id == StoreCertification.store_id
        )
        if ("platform", 0) not in scopes:
            store_ids = [scope_id for scope_type, scope_id in scopes if scope_type == "store"]
            if not store_ids:
                return []
            statement = statement.where(Store.id.in_(store_ids))
        if review_status:
            statement = statement.where(StoreCertification.review_status == review_status)
        if cursor:
            statement = statement.where(StoreCertification.certification_no > cursor)
        rows = (
            await self.session.execute(
                statement.order_by(StoreCertification.certification_no).limit(limit + 1)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def certification_by_no(
        self, certification_no: str, *, for_update: bool = False
    ) -> tuple[StoreCertification, Store] | None:
        statement = (
            select(StoreCertification, Store)
            .join(Store, Store.id == StoreCertification.store_id)
            .where(StoreCertification.certification_no == certification_no)
        )
        if for_update:
            statement = statement.with_for_update(of=StoreCertification)
        row = (await self.session.execute(statement)).one_or_none()
        return None if row is None else (row[0], row[1])

    async def certification_events(
        self, certification_id: int, limit: int = 100
    ) -> list[StoreCertificationEvent]:
        return list(
            (
                await self.session.scalars(
                    select(StoreCertificationEvent)
                    .where(StoreCertificationEvent.certification_id == certification_id)
                    .order_by(
                        StoreCertificationEvent.created_at.desc(),
                        StoreCertificationEvent.id.desc(),
                    )
                    .limit(limit)
                )
            ).all()
        )

    async def files_by_nos(self, file_nos: list[str]) -> list[FileObject]:
        if not file_nos:
            return []
        return list(
            (
                await self.session.scalars(
                    select(FileObject).where(FileObject.file_no.in_(file_nos))
                )
            ).all()
        )

    async def file_by_object_key(self, object_key: str) -> FileObject | None:
        return cast(
            FileObject | None,
            await self.session.scalar(
                select(FileObject).where(FileObject.object_key == object_key)
            ),
        )

    async def policies(self, store_id: int) -> list[StoreServicePolicy]:
        return list(
            (
                await self.session.scalars(
                    select(StoreServicePolicy)
                    .where(StoreServicePolicy.store_id == store_id)
                    .order_by(
                        StoreServicePolicy.policy_type,
                        StoreServicePolicy.policy_version.desc(),
                    )
                )
            ).all()
        )

    async def policy_by_no(
        self, store_id: int, policy_no: str, *, for_update: bool = False
    ) -> StoreServicePolicy | None:
        statement = select(StoreServicePolicy).where(
            StoreServicePolicy.store_id == store_id,
            StoreServicePolicy.policy_no == policy_no,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(StoreServicePolicy | None, await self.session.scalar(statement))

    async def next_policy_version(self, store_id: int, policy_type: str) -> int:
        value = await self.session.scalar(
            select(func.max(StoreServicePolicy.policy_version)).where(
                StoreServicePolicy.store_id == store_id,
                StoreServicePolicy.policy_type == policy_type,
            )
        )
        return int(value or 0) + 1

    async def latest_policy(self, store_id: int, policy_type: str) -> StoreServicePolicy | None:
        return cast(
            StoreServicePolicy | None,
            await self.session.scalar(
                select(StoreServicePolicy)
                .where(
                    StoreServicePolicy.store_id == store_id,
                    StoreServicePolicy.policy_type == policy_type,
                )
                .order_by(StoreServicePolicy.policy_version.desc())
                .limit(1)
            ),
        )

    async def overlapping_policies(
        self,
        policy: StoreServicePolicy,
        effective_at: datetime,
        expires_at: datetime | None,
    ) -> list[StoreServicePolicy]:
        overlap_conditions = [
            or_(
                StoreServicePolicy.expires_at.is_(None),
                StoreServicePolicy.expires_at > effective_at,
            )
        ]
        if expires_at is not None:
            overlap_conditions.append(StoreServicePolicy.effective_at < expires_at)
        overlap = and_(
            *overlap_conditions,
        )
        return list(
            (
                await self.session.scalars(
                    select(StoreServicePolicy)
                    .where(
                        StoreServicePolicy.store_id == policy.store_id,
                        StoreServicePolicy.policy_type == policy.policy_type,
                        StoreServicePolicy.id != policy.id,
                        StoreServicePolicy.policy_status == "published",
                        StoreServicePolicy.effective_at.is_not(None),
                        overlap,
                    )
                    .with_for_update()
                )
            ).all()
        )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
