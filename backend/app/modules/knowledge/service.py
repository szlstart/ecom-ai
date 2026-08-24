from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import request_id_context
from app.core.id_generator import new_prefixed_ulid
from app.modules.knowledge.embedding import embedding_provider
from app.modules.knowledge.models import KnowledgeDocument
from app.modules.knowledge.retrieval import hybrid_search
from app.modules.knowledge.schemas import (
    KnowledgeCitation,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
)
from app.modules.rbac.dependencies import AdminAccess
from app.modules.stores.models import Store


class KnowledgeService:
    def __init__(self, mysql: AsyncSession, postgres: AsyncSession) -> None:
        self.mysql = mysql
        self.postgres = postgres

    async def search(
        self, access: AdminAccess, payload: KnowledgeSearchRequest
    ) -> KnowledgeSearchResult:
        if payload.scope_type == "store":
            store = await self.mysql.scalar(select(Store).where(Store.store_no == payload.scope_id))
            if store is None:
                return KnowledgeSearchResult(items=[])
            access.require_scope("store", store.id)
        elif ("platform", 0) not in access.scopes:
            return KnowledgeSearchResult(items=[])
        retrieval = await hybrid_search(
            self.postgres,
            embedding_provider(get_settings()),
            query=payload.query,
            scope_type=payload.scope_type,
            scope_no=payload.scope_id,
            limit=payload.limit,
            trace_id=request_id_context.get() or new_prefixed_ulid("req_"),
        )
        document_nos = {item.document_no for item in retrieval.chunks}
        rows = list(
            (
                await self.mysql.scalars(
                    select(KnowledgeDocument)
                    .where(
                        KnowledgeDocument.document_no.in_(document_nos),
                        KnowledgeDocument.document_status == "published",
                    )
                )
            ).all()
        ) if document_nos else []
        by_no = {
            item.document_no: item
            for item in rows
            if item.scope_type == payload.scope_type and item.scope_no == payload.scope_id
        }
        return KnowledgeSearchResult(
            items=[
                KnowledgeCitation(
                    document_id=item.document_no,
                    content_version=item.content_version,
                    title=by_no[item.document_no].title,
                    excerpt=item.text[:500],
                    score=item.score,
                )
                for item in retrieval.chunks
                if item.document_no in by_no
                and by_no[item.document_no].content_version == item.content_version
            ],
            degraded=retrieval.degraded,
        )
