from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.catalog.models import (
    Product,
    ProductAttribute,
    ProductContentVersion,
    ProductFaq,
    ProductFaqVersion,
    ProductSku,
)
from app.modules.files import models as file_models  # noqa: F401
from app.modules.identity import models as identity_models  # noqa: F401
from app.modules.knowledge.cleaning import clean_document_text
from app.modules.knowledge.indexing import create_index_job
from app.modules.knowledge.models import KnowledgeDocument
from app.modules.rbac.models import Role, UserRole
from app.modules.stores.models import Store, StoreServicePolicy
from app.modules.system.models import AdminBatchJob


@dataclass(frozen=True)
class KnowledgeSeedResult:
    documents_created: int = 0
    documents_updated: int = 0
    index_jobs_created: int = 0
    documents_withdrawn: int = 0


@dataclass(frozen=True)
class _SourceDocument:
    document_no: str
    scope_type: str
    scope_no: str
    title: str
    safe_text: str


async def seed_default_knowledge(
    mysql: AsyncSession, postgres: AsyncSession
) -> KnowledgeSeedResult:
    """Synchronize system-owned knowledge and enqueue missing vector generations.

    The job is intentionally idempotent. Business facts that change at transaction
    speed (price, stock, order, payment and logistics state) are not copied into RAG;
    Agents must obtain those facts from typed business tools at answer time.
    """

    await _reconcile_terminal_commands(mysql, postgres)
    sources = [*_platform_sources(), *(await _store_sources(mysql))]
    active_document_nos = {source.document_no for source in sources}
    created = 0
    updated = 0
    withdrawn = 0
    documents: list[KnowledgeDocument] = []
    for source in sources:
        item = await mysql.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.document_no == source.document_no
            )
        )
        content_version = _content_version(source.safe_text)
        if item is None:
            item = KnowledgeDocument(
                document_no=source.document_no,
                scope_type=source.scope_type,
                scope_no=source.scope_no,
                title=source.title,
                safe_text=source.safe_text,
                document_status="published",
                content_version=content_version,
            )
            mysql.add(item)
            created += 1
        elif (
            item.scope_type != source.scope_type
            or item.scope_no != source.scope_no
            or item.title != source.title
            or item.safe_text != source.safe_text
            or item.content_version != content_version
            or item.document_status != "published"
        ):
            item.scope_type = source.scope_type
            item.scope_no = source.scope_no
            item.title = source.title
            item.safe_text = source.safe_text
            item.content_version = content_version
            item.document_status = "published"
            item.version += 1
            updated += 1
        documents.append(item)

    stale = list(
        (
            await mysql.scalars(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.title.like("[系统]%"),
                    KnowledgeDocument.document_status == "published",
                    KnowledgeDocument.document_no.not_in(active_document_nos),
                )
            )
        ).all()
    )
    for item in stale:
        item.document_status = "withdrawn"
        item.version += 1
        withdrawn += 1
        await postgres.execute(
            text("DELETE FROM knowledge.document_chunks WHERE document_no=:document_no"),
            {"document_no": item.document_no},
        )
        await postgres.execute(
            text(
                """UPDATE knowledge.index_generations
                   SET generation_status='retired'
                   WHERE document_no=:document_no AND generation_status='active'"""
            ),
            {"document_no": item.document_no},
        )

    await mysql.flush()
    await mysql.commit()
    await postgres.commit()

    administrator_id = await _platform_administrator_id(mysql)
    if administrator_id is None:
        return KnowledgeSeedResult(created, updated, 0, withdrawn)

    settings = get_settings()
    jobs_created = 0
    for item in documents:
        if await _has_active_generation(
            postgres,
            document_no=item.document_no,
            content_version=item.content_version,
            embedding_model_code=settings.embedding_model,
        ):
            continue
        existing_jobs = list(
            (
                await mysql.scalars(
                    select(AdminBatchJob).where(
                        AdminBatchJob.execution_backend == "postgres_knowledge",
                        AdminBatchJob.job_type == "knowledge_index",
                    )
                )
            ).all()
        )
        matching_jobs = [
            job
            for job in existing_jobs
            if job.request_config.get("document_no") == item.document_no
            and job.request_config.get("content_version") == item.content_version
            and job.request_config.get("embedding_model_code") == settings.embedding_model
        ]
        if any(job.job_status in {"queued", "running"} for job in matching_jobs):
            continue
        retry_no = len(matching_jobs)
        request_key = (
            f"system-knowledge:{item.document_no}:{item.content_version}:"
            f"{settings.embedding_model}:retry-{retry_no}"
        )
        await create_index_job(
            mysql,
            postgres,
            item,
            requested_by=administrator_id,
            trace_id=f"knowledge-seed-{item.document_no}",
            scope_id=await _scope_id(mysql, item),
            request_key=request_key,
            embedding_model_code=settings.embedding_model,
        )
        jobs_created += 1
    return KnowledgeSeedResult(created, updated, jobs_created, withdrawn)


async def _reconcile_terminal_commands(
    mysql: AsyncSession, postgres: AsyncSession
) -> None:
    commands = list(
        (
            await mysql.scalars(
                select(AdminBatchJob).where(
                    AdminBatchJob.execution_backend == "postgres_knowledge",
                    AdminBatchJob.job_type == "knowledge_index",
                    AdminBatchJob.job_status.in_(("failed", "cancelled")),
                )
            )
        ).all()
    )
    for command in commands:
        status = "cancelled" if command.job_status == "cancelled" else "failed"
        await postgres.execute(
            text(
                """UPDATE knowledge.indexing_jobs
                   SET job_status=:status, progress=100,
                       error_code=COALESCE(error_code, :error_code),
                       error_owner=COALESCE(error_owner, 'command'),
                       status_version=status_version+1, updated_at=now()
                   WHERE command_job_no=:command_job_no
                     AND job_status IN ('queued','running')"""
            ),
            {
                "status": status,
                "error_code": command.error_code or "PARENT_COMMAND_FAILED",
                "command_job_no": command.job_no,
            },
        )
    await postgres.commit()


def _platform_sources() -> list[_SourceDocument]:
    directory = Path(__file__).parent / "knowledge_sources" / "platform"
    sources: list[_SourceDocument] = []
    for path in sorted(directory.glob("*.md")):
        safe_text = clean_document_text(path.read_text(encoding="utf-8"))
        first_line = next(
            (line.removeprefix("# ").strip() for line in safe_text.splitlines() if line.strip()),
            path.stem,
        )
        sources.append(
            _SourceDocument(
                document_no=_document_no(f"platform:{path.name}"),
                scope_type="platform",
                scope_no="platform",
                title=f"[系统] {first_line}",
                safe_text=safe_text,
            )
        )
    return sources


async def _store_sources(mysql: AsyncSession) -> list[_SourceDocument]:
    stores = list(
        (
            await mysql.scalars(
                select(Store).where(Store.store_status == "active").order_by(Store.id)
            )
        ).all()
    )
    return [await _store_source(mysql, store) for store in stores]


async def _store_source(mysql: AsyncSession, store: Store) -> _SourceDocument:
    lines = [
        f"# {store.store_name}公开商品与服务资料",
        "",
        "以下内容仅用于商品介绍与店铺公开服务咨询。价格、库存、订单、支付、物流和售后状态必须通过实时业务工具查询。",
    ]
    if store.description:
        lines.extend(("", "## 店铺介绍", store.description))
    policies = list(
        (
            await mysql.scalars(
                select(StoreServicePolicy)
                .where(
                    StoreServicePolicy.store_id == store.id,
                    StoreServicePolicy.policy_status == "published",
                )
                .order_by(StoreServicePolicy.policy_type, StoreServicePolicy.policy_version.desc())
            )
        ).all()
    )
    if policies:
        lines.extend(("", "## 店铺服务政策"))
        for policy in policies:
            lines.extend((f"### {policy.title}", policy.content))
    products = list(
        (
            await mysql.scalars(
                select(Product)
                .where(
                    Product.store_id == store.id,
                    Product.product_status == "on_sale",
                    Product.deleted_at.is_(None),
                )
                .order_by(Product.id)
            )
        ).all()
    )
    for product in products:
        lines.extend(("", f"## 商品: {product.product_name}"))
        if product.description:
            lines.append(product.description)
        content = (
            await mysql.scalar(
                select(ProductContentVersion).where(
                    ProductContentVersion.id == product.published_detail_content_version_id,
                    ProductContentVersion.version_status == "published",
                )
            )
            if product.published_detail_content_version_id
            else None
        )
        if content and content.safe_text:
            lines.extend(("### 商品详情", content.safe_text))
        attributes = list(
            (
                await mysql.scalars(
                    select(ProductAttribute)
                    .where(ProductAttribute.product_id == product.id)
                    .order_by(ProductAttribute.sort_order, ProductAttribute.id)
                )
            ).all()
        )
        if attributes:
            lines.append("### 规格参数")
            lines.extend(
                f"- {attribute.attribute_name}: {attribute.value_text}{attribute.unit or ''}"
                for attribute in attributes
            )
        skus = list(
            (
                await mysql.scalars(
                    select(ProductSku)
                    .where(
                        ProductSku.product_id == product.id,
                        ProductSku.sku_status == "active",
                    )
                    .order_by(ProductSku.id)
                )
            ).all()
        )
        if skus:
            lines.append("### 可选款式")
            lines.extend(f"- {sku.sku_name}" for sku in skus)
        faqs = (
            await mysql.execute(
                select(ProductFaq, ProductFaqVersion)
                .join(
                    ProductFaqVersion,
                    ProductFaqVersion.id == ProductFaq.published_content_version_id,
                )
                .where(
                    ProductFaq.product_id == product.id,
                    ProductFaq.faq_status == "published",
                    ProductFaqVersion.version_status == "published",
                )
                .order_by(ProductFaq.sort_order, ProductFaq.id)
            )
        ).all()
        if faqs:
            lines.append("### 常见问题")
            for faq, answer in faqs:
                lines.extend((f"- 问: {faq.question}", f"  答: {answer.safe_text}"))
    safe_text = clean_document_text("\n".join(lines))
    return _SourceDocument(
        document_no=_document_no(f"store:{store.store_no}"),
        scope_type="store",
        scope_no=store.store_no,
        title=f"[系统] {store.store_name}公开商品与服务资料",
        safe_text=safe_text,
    )


async def _platform_administrator_id(mysql: AsyncSession) -> int | None:
    return cast(
        int | None,
        await mysql.scalar(
            select(UserRole.user_id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                Role.role_code == "platform_super_admin",
                Role.scope_type == "platform",
                Role.role_status == "active",
                UserRole.scope_type == "platform",
                UserRole.scope_id == 0,
                UserRole.grant_status == "active",
            )
            .order_by(UserRole.id)
            .limit(1)
        )
    )


async def _has_active_generation(
    postgres: AsyncSession,
    *,
    document_no: str,
    content_version: str,
    embedding_model_code: str,
) -> bool:
    count = await postgres.scalar(
        text(
            """SELECT count(*)
               FROM knowledge.index_generations generation
               JOIN knowledge.document_chunks chunk
                 ON chunk.generation_no=generation.generation_no
               WHERE generation.document_no=:document_no
                 AND generation.generation_status='active'
                 AND chunk.content_version=:content_version
                 AND chunk.embedding_model_code=:embedding_model_code
                 AND chunk.embedding IS NOT NULL"""
        ),
        {
            "document_no": document_no,
            "content_version": content_version,
            "embedding_model_code": embedding_model_code,
        },
    )
    return bool(count)


async def _scope_id(mysql: AsyncSession, item: KnowledgeDocument) -> int:
    if item.scope_type == "platform":
        return 0
    return (
        await mysql.scalar(select(Store.id).where(Store.store_no == item.scope_no))
    ) or 0


def _document_no(identity: str) -> str:
    return "kdoc_" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _content_version(safe_text: str) -> str:
    return "kver_" + hashlib.sha256(safe_text.encode()).hexdigest()[:32]
