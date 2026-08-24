from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.modules.knowledge.cleaning import clean_document_text
from app.modules.knowledge.indexing import create_index_job
from app.modules.knowledge.models import KnowledgeDocument
from app.modules.knowledge.schemas import KnowledgeDocumentCreate, KnowledgeDocumentView
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.stores.models import Store


class KnowledgeDocumentService:
    def __init__(self, mysql: AsyncSession, postgres: AsyncSession) -> None:
        self.mysql = mysql
        self.postgres = postgres

    async def create(
        self, access: AdminAccess, payload: KnowledgeDocumentCreate
    ) -> KnowledgeDocumentView:
        scope_no = payload.scope_id
        scope_id = 0
        if payload.scope_type == "store":
            store = await self.mysql.scalar(select(Store).where(Store.store_no == payload.scope_id))
            if store is None:
                raise _not_found()
            access.require_scope("store", store.id)
            scope_id = store.id
        else:
            access.require_scope("platform", 0)
        try:
            safe_text = clean_document_text(payload.safe_text)
        except ValueError as exc:
            raise ApplicationError(
                status=422,
                code="KNOWLEDGE_TEXT_EMPTY",
                title="Knowledge text invalid",
                detail="文档清洗后没有可索引正文。",
            ) from exc
        item = KnowledgeDocument(
            document_no=new_prefixed_ulid("kdoc_"),
            scope_type=payload.scope_type,
            scope_no=scope_no,
            title=payload.title,
            safe_text=safe_text,
            document_status="draft",
            content_version=new_prefixed_ulid("kver_"),
        )
        self.mysql.add(item)
        await self.mysql.flush()
        record_admin_operation(
            self.mysql,
            access,
            action="knowledge.document.create",
            target_type="knowledge_document",
            target_no=item.document_no,
            scope_type=payload.scope_type,
            scope_id=scope_id,
        )
        await self.mysql.commit()
        return _view(item)

    async def list(self, access: AdminAccess, limit: int = 100) -> list[KnowledgeDocumentView]:
        statement = select(KnowledgeDocument).order_by(
            KnowledgeDocument.created_at.desc(), KnowledgeDocument.id.desc()
        )
        if ("platform", 0) not in access.scopes:
            store_ids = [
                scope_id for scope_type, scope_id in access.scopes if scope_type == "store"
            ]
            store_nos = list(
                (
                    await self.mysql.scalars(select(Store.store_no).where(Store.id.in_(store_ids)))
                ).all()
            )
            statement = statement.where(
                KnowledgeDocument.scope_type == "store",
                KnowledgeDocument.scope_no.in_(store_nos),
            )
        rows = list((await self.mysql.scalars(statement.limit(limit))).all())
        return [_view(item) for item in rows]

    async def publish(
        self, access: AdminAccess, document_no: str, idempotency_key: str
    ) -> KnowledgeDocumentView:
        item = await self._document(access, document_no, lock=True)
        if item.document_status not in {"draft", "published"}:
            raise ApplicationError(
                status=409,
                code="DOCUMENT_NOT_PUBLISHABLE",
                title="Document conflict",
                detail="知识文档当前不可发布。",
            )
        if item.document_status == "draft":
            item.document_status = "published"
            item.version += 1
        scope_id = 0
        if item.scope_type == "store":
            store = await self.mysql.scalar(select(Store).where(Store.store_no == item.scope_no))
            if store is None:
                raise _not_found()
            scope_id = store.id
        record_admin_operation(
            self.mysql,
            access,
            action="knowledge.document.publish",
            target_type="knowledge_document",
            target_no=item.document_no,
        )
        state = await create_index_job(
            self.mysql,
            self.postgres,
            item,
            requested_by=access.context.user.id,
            trace_id=request_id_context.get() or new_prefixed_ulid("req_"),
            scope_id=scope_id,
            request_key=idempotency_key,
            embedding_model_code=get_settings().embedding_model,
        )
        result = _view(item)
        result.index_job_no = state.command_job_no
        result.index_status = state.status
        return result

    async def withdraw(self, access: AdminAccess, document_no: str) -> KnowledgeDocumentView:
        item = await self._document(access, document_no, lock=True)
        item.document_status = "withdrawn"
        item.version += 1
        record_admin_operation(
            self.mysql,
            access,
            action="knowledge.document.withdraw",
            target_type="knowledge_document",
            target_no=item.document_no,
        )
        await self.mysql.commit()
        await self.postgres.execute(
            text("DELETE FROM knowledge.document_chunks WHERE document_no=:no"),
            {"no": item.document_no},
        )
        await self.postgres.execute(
            text("""UPDATE knowledge.index_generations SET generation_status='retired'
            WHERE document_no=:no AND generation_status='active'"""),
            {"no": item.document_no},
        )
        await self.postgres.commit()
        return _view(item)

    async def _document(
        self, access: AdminAccess, document_no: str, *, lock: bool
    ) -> KnowledgeDocument:
        query = select(KnowledgeDocument).where(KnowledgeDocument.document_no == document_no)
        item = await self.mysql.scalar(query.with_for_update() if lock else query)
        if item is None:
            raise _not_found()
        if item.scope_type == "store":
            store = await self.mysql.scalar(select(Store).where(Store.store_no == item.scope_no))
            if store is None:
                raise _not_found()
            access.require_scope("store", store.id)
        else:
            access.require_scope("platform", 0)
        return item


def _view(item: KnowledgeDocument) -> KnowledgeDocumentView:
    return KnowledgeDocumentView(
        document_id=item.document_no,
        scope_type=item.scope_type,
        scope_id=item.scope_no,
        title=item.title,
        content_version=item.content_version,
        status=item.document_status,
    )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="未找到知识文档。",
    )
