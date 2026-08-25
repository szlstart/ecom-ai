from __future__ import annotations

import hashlib
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.modules.catalog.content_sanitizer import sanitize_content
from app.modules.content.models import PlatformContentEntry, PlatformContentVersion
from app.modules.content.schemas import (
    ContentCreate,
    ContentList,
    ContentUpdate,
    ContentVersionView,
    ContentView,
    PublishedContent,
    PublishedContentList,
)
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess


class ContentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def published(self, content_type: str) -> PublishedContentList:
        now = utc_now()
        rows = list(
            (
                await self.session.execute(
                    select(PlatformContentEntry, PlatformContentVersion)
                    .join(
                        PlatformContentVersion,
                        PlatformContentVersion.entry_id == PlatformContentEntry.id,
                    )
                    .where(
                        PlatformContentEntry.content_type == content_type,
                        PlatformContentEntry.content_status == "active",
                        PlatformContentVersion.publish_status == "published",
                        PlatformContentVersion.effective_at <= now,
                        (PlatformContentVersion.expires_at.is_(None))
                        | (PlatformContentVersion.expires_at > now),
                    )
                    .order_by(PlatformContentVersion.effective_at.desc())
                )
            ).all()
        )
        return PublishedContentList(
            items=[
                PublishedContent(
                    content_id=e.content_no,
                    content_key=e.content_key,
                    content_type=e.content_type,
                    title=e.title,
                    version=_version(v),
                )
                for e, v in rows
            ]
        )

    async def published_key(self, content_type: str, content_key: str) -> PublishedContent:
        entry = await self.session.scalar(
            select(PlatformContentEntry).where(
                PlatformContentEntry.content_type == content_type,
                PlatformContentEntry.content_key == content_key,
            )
        )
        if entry is None:
            raise _not_found()
        items = await self.published(content_type)
        result = next((item for item in items.items if item.content_key == content_key), None)
        if result is None:
            raise ApplicationError(
                status=410,
                code="CONTENT_GONE",
                title="Content unavailable",
                detail="该内容已撤回或不在有效期内。",
            )
        return result

    async def list(self) -> ContentList:
        entries = list(
            (
                await self.session.scalars(
                    select(PlatformContentEntry).order_by(PlatformContentEntry.content_key)
                )
            ).all()
        )
        if not entries:
            return ContentList(items=[])
        versions = list(
            (
                await self.session.scalars(
                    select(PlatformContentVersion)
                    .where(PlatformContentVersion.entry_id.in_([entry.id for entry in entries]))
                    .order_by(PlatformContentVersion.entry_id, PlatformContentVersion.id.desc())
                )
            ).all()
        )
        grouped: dict[int, list[PlatformContentVersion]] = defaultdict(list)
        for version in versions:
            grouped[version.entry_id].append(version)
        return ContentList(
            items=[_content_view(entry, grouped.get(entry.id, [])) for entry in entries]
        )

    async def get(self, content_no: str) -> ContentView:
        entry = await self.session.scalar(
            select(PlatformContentEntry).where(PlatformContentEntry.content_no == content_no)
        )
        if entry is None:
            raise _not_found()
        return await self._view(entry)

    async def create(self, access: AdminAccess, payload: ContentCreate) -> ContentView:
        access.require_scope("platform", 0)
        if await self.session.scalar(
            select(PlatformContentEntry.id).where(
                PlatformContentEntry.content_key == payload.content_key
            )
        ):
            raise _conflict("CONTENT_KEY_EXISTS")
        entry = PlatformContentEntry(
            content_no=new_prefixed_ulid("cnt_"),
            content_key=payload.content_key,
            content_type=payload.content_type,
            title=payload.title,
            content_status="draft",
        )
        self.session.add(entry)
        await self.session.flush()
        self.session.add(
            _new_version(
                entry.id,
                "v1",
                payload.locale,
                payload.region_code,
                payload.source_format,
                payload.source_content,
            )
        )
        record_admin_operation(
            self.session,
            access,
            action="content.create",
            target_type="platform_content",
            target_no=entry.content_no,
            after={"content_key": entry.content_key, "content_type": entry.content_type},
        )
        await self.session.commit()
        return await self._view(entry)

    async def update(
        self, access: AdminAccess, content_no: str, payload: ContentUpdate, expected: int
    ) -> ContentView:
        entry = await self.session.scalar(
            select(PlatformContentEntry)
            .where(PlatformContentEntry.content_no == content_no)
            .with_for_update()
        )
        if entry is None:
            raise _not_found()
        access.require_scope("platform", 0)
        if entry.version != expected:
            raise _conflict("RESOURCE_VERSION_CONFLICT", status=412)
        latest = await self.session.scalar(
            select(func.count(PlatformContentVersion.id)).where(
                PlatformContentVersion.entry_id == entry.id
            )
        )
        entry.title = payload.title
        entry.version += 1
        self.session.add(
            _new_version(
                entry.id,
                f"v{int(latest or 0) + 1}",
                payload.locale,
                payload.region_code,
                payload.source_format,
                payload.source_content,
            )
        )
        record_admin_operation(
            self.session,
            access,
            action="content.update",
            target_type="platform_content",
            target_no=entry.content_no,
            after={"version": entry.version},
        )
        await self.session.commit()
        return await self._view(entry)

    async def publish(self, access: AdminAccess, content_no: str, version_no: str) -> ContentView:
        entry = await self.session.scalar(
            select(PlatformContentEntry)
            .where(PlatformContentEntry.content_no == content_no)
            .with_for_update()
        )
        if entry is None:
            raise _not_found()
        access.require_scope("platform", 0)
        versions = list(
            (
                await self.session.scalars(
                    select(PlatformContentVersion)
                    .where(PlatformContentVersion.entry_id == entry.id)
                    .with_for_update()
                )
            ).all()
        )
        selected = next((item for item in versions if item.document_version == version_no), None)
        if selected is None:
            raise _not_found()
        for item in versions:
            if item.publish_status == "published":
                item.publish_status = "withdrawn"
        selected.publish_status = "published"
        selected.effective_at = utc_now()
        entry.content_status = "active"
        entry.version += 1
        record_admin_operation(
            self.session,
            access,
            action="content.publish",
            target_type="platform_content",
            target_no=entry.content_no,
            after={"published_version": version_no},
        )
        await self.session.commit()
        return await self._view(entry)

    async def withdraw(self, access: AdminAccess, content_no: str) -> ContentView:
        entry = await self.session.scalar(
            select(PlatformContentEntry)
            .where(PlatformContentEntry.content_no == content_no)
            .with_for_update()
        )
        if entry is None:
            raise _not_found()
        access.require_scope("platform", 0)
        for item in (
            await self.session.scalars(
                select(PlatformContentVersion)
                .where(
                    PlatformContentVersion.entry_id == entry.id,
                    PlatformContentVersion.publish_status == "published",
                )
                .with_for_update()
            )
        ).all():
            item.publish_status = "withdrawn"
        entry.content_status = "inactive"
        entry.version += 1
        record_admin_operation(
            self.session,
            access,
            action="content.withdraw",
            target_type="platform_content",
            target_no=entry.content_no,
        )
        await self.session.commit()
        return await self._view(entry)

    async def _view(self, entry: PlatformContentEntry) -> ContentView:
        versions = list(
            (
                await self.session.scalars(
                    select(PlatformContentVersion)
                    .where(PlatformContentVersion.entry_id == entry.id)
                    .order_by(PlatformContentVersion.id.desc())
                )
            ).all()
        )
        return _content_view(entry, versions)


def _content_view(
    entry: PlatformContentEntry, versions: list[PlatformContentVersion]
) -> ContentView:
    return ContentView(
        content_id=entry.content_no,
        content_key=entry.content_key,
        content_type=entry.content_type,
        title=entry.title,
        status=entry.content_status,
        version=entry.version,
        versions=[_version(item) for item in versions],
    )


def _new_version(
    entry_id: int, version: str, locale: str, region: str, source_format: str, source: str
) -> PlatformContentVersion:
    safe = sanitize_content(source_format, source)
    metadata: dict[str, object] = {
        "format": safe.public_content_format,
        "blocks": safe.safe_blocks,
        "html": safe.safe_html,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
    }
    return PlatformContentVersion(
        content_version_no=new_prefixed_ulid("cver_"),
        entry_id=entry_id,
        document_version=version,
        locale=locale,
        region_code=region,
        safe_content=safe.safe_text,
        content_hash=hashlib.sha256(safe.safe_text.encode()).digest(),
        metadata_json=metadata,
        publish_status="draft",
        effective_at=utc_now(),
    )


def _version(item: PlatformContentVersion) -> ContentVersionView:
    metadata = item.metadata_json or {}
    blocks = metadata.get("blocks")
    html = metadata.get("html")
    return ContentVersionView(
        content_version_id=item.content_version_no,
        version=item.document_version,
        locale=item.locale,
        region_code=item.region_code,
        format=str(metadata.get("format") or "structured_v1"),
        blocks=blocks if isinstance(blocks, list) else None,
        html=html if isinstance(html, str) else None,
        text=item.safe_content,
        status=item.publish_status,
        effective_at=item.effective_at,
        expires_at=item.expires_at,
    )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404, code="CONTENT_NOT_FOUND", title="Content not found", detail="未找到该平台内容。"
    )


def _conflict(code: str, status: int = 409) -> ApplicationError:
    return ApplicationError(
        status=status, code=code, title="Content conflict", detail="平台内容状态或版本冲突。"
    )
