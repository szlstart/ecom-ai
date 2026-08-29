from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.ai_runtime import seed_ai_runtime
from app.bootstrap.merchant import STORE_OPERATOR_PERMISSIONS
from app.core.id_generator import new_prefixed_ulid
from app.generated.permission_catalog import PERMISSIONS
from app.modules.catalog.models import Category
from app.modules.content.models import PlatformContentEntry, PlatformContentVersion
from app.modules.identity.models import User
from app.modules.rbac.models import Permission, Role, RolePermission

LEGAL_DOCUMENTS = {
    "terms_of_service": {
        "title": "用户协议",
        "version": "terms_2026_08",
        "content": "欢迎使用 Ecom AI 在线商城。使用本平台前，请阅读并遵守本用户协议。",
    },
    "privacy_policy": {
        "title": "隐私政策",
        "version": "privacy_2026_08",
        "content": "我们仅在提供商城、交易与客服服务所必需的范围内处理个人信息。",
    },
}

BASE_CATEGORIES = (
    ("digital", "数码家电"),
    ("clothing", "服饰鞋包"),
    ("home", "家居生活"),
    ("beauty", "美妆个护"),
    ("food", "食品生鲜"),
    ("books", "图书文娱"),
    ("sports", "运动户外"),
    ("other", "其他商品"),
)


async def seed_reference_data(session: AsyncSession) -> None:
    await _seed_permissions(session)
    await _seed_roles(session)
    await _sync_store_operator_permissions(session)
    await _seed_categories(session)
    await _seed_legal_documents(session)
    await seed_ai_runtime(session)
    await session.commit()


async def _seed_permissions(session: AsyncSession) -> None:
    existing = set((await session.scalars(select(Permission.permission_code))).all())
    for raw in PERMISSIONS:
        code = str(raw["code"])
        if code in existing:
            continue
        session.add(
            Permission(
                permission_code=code,
                resource=str(raw["resource"]),
                action=str(raw["action"]),
                risk_level=str(raw["risk_level"]),
                allowed_scope_types=cast(list[str], raw["allowed_scope_types"]),
                delegation_policy=str(raw["delegation_policy"]),
                requires_mfa=bool(raw["requires_mfa"]),
                requires_recent_auth=bool(raw["requires_recent_auth"]),
                approval_policy=str(raw["approval_policy"]),
                owner=str(raw["owner"]),
                description=str(raw["description"]),
                permission_status=str(raw["status"]),
            )
        )


async def _seed_roles(session: AsyncSession) -> None:
    existing = set((await session.scalars(select(Role.role_code))).all())
    roles = (
        ("user", "普通用户", "platform", "商城消费者默认角色"),
        ("platform_super_admin", "平台超级管理员", "platform", "平台安全管理角色"),
        ("store_operator", "店铺运营", "store", "店铺范围运营角色"),
    )
    for code, name, scope, description in roles:
        if code in existing:
            continue
        session.add(
            Role(
                role_no=new_prefixed_ulid("rol_"),
                role_code=code,
                role_name=name,
                scope_type=scope,
                role_type="system",
                description=description,
                role_status="active",
            )
        )
    await session.flush()


async def _sync_store_operator_permissions(session: AsyncSession) -> None:
    role = await session.scalar(
        select(Role).where(Role.role_code == "store_operator", Role.scope_type == "store")
    )
    if role is None:
        raise RuntimeError("store_operator role was not seeded")
    permissions = list(
        (
            await session.scalars(
                select(Permission).where(
                    Permission.permission_code.in_(STORE_OPERATOR_PERMISSIONS),
                    Permission.permission_status == "active",
                )
            )
        ).all()
    )
    found = {item.permission_code for item in permissions}
    missing = set(STORE_OPERATOR_PERMISSIONS) - found
    if missing:
        raise RuntimeError(f"missing store operator permissions: {', '.join(sorted(missing))}")
    actor_id = await session.scalar(
        select(User.id).where(User.user_status == "active").order_by(User.id).limit(1)
    )
    # A completely fresh database has no security principal yet. The first
    # merchant/admin provisioning command grants the same exact permission set.
    if actor_id is None:
        return
    allowed_ids = {item.id for item in permissions}
    await session.execute(
        delete(RolePermission).where(
            RolePermission.role_id == role.id,
            ~RolePermission.permission_id.in_(allowed_ids),
        )
    )
    existing_ids = set(
        (
            await session.scalars(
                select(RolePermission.permission_id).where(RolePermission.role_id == role.id)
            )
        ).all()
    )
    session.add_all(
        RolePermission(role_id=role.id, permission_id=item.id, granted_by=actor_id)
        for item in permissions
        if item.id not in existing_ids
    )


async def _seed_categories(session: AsyncSession) -> None:
    existing = set((await session.scalars(select(Category.category_code))).all())
    for sort_order, (code, name) in enumerate(BASE_CATEGORIES, start=1):
        if code in existing:
            continue
        session.add(
            Category(
                category_no=new_prefixed_ulid("cat_"),
                category_name=name,
                category_code=code,
                path=f"/{code}",
                level=1,
                sort_order=sort_order,
                category_status="active",
            )
        )


async def _seed_legal_documents(session: AsyncSession) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    for document_type, item in LEGAL_DOCUMENTS.items():
        entry = await session.scalar(
            select(PlatformContentEntry).where(
                PlatformContentEntry.content_key == f"legal.{document_type}"
            )
        )
        if entry is None:
            entry = PlatformContentEntry(
                content_no=new_prefixed_ulid("cnt_"),
                content_key=f"legal.{document_type}",
                content_type="legal_document",
                title=item["title"],
                content_status="active",
            )
            session.add(entry)
            await session.flush()
        exists = await session.scalar(
            select(PlatformContentVersion.id).where(
                PlatformContentVersion.entry_id == entry.id,
                PlatformContentVersion.document_version == item["version"],
            )
        )
        if exists is None:
            content = item["content"]
            session.add(
                PlatformContentVersion(
                    content_version_no=new_prefixed_ulid("ctv_"),
                    entry_id=entry.id,
                    document_version=item["version"],
                    locale="zh-CN",
                    region_code="CN",
                    safe_content=content,
                    content_hash=hashlib.sha256(content.encode()).digest(),
                    metadata_json={"format": "plain_text_v1"},
                    publish_status="published",
                    effective_at=now,
                )
            )
