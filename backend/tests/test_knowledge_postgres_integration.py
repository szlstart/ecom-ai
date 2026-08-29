import secrets

from httpx import AsyncClient
from sqlalchemy import delete, text

from app.core.config import get_settings
from app.database.mysql import mysql_session
from app.database.postgres import close_postgres, initialize_postgres, postgres_session
from app.modules.knowledge.embedding import DisabledEmbeddingProvider
from app.modules.knowledge.indexing import run_index_job
from app.modules.knowledge.models import KnowledgeDocument
from app.modules.knowledge.retrieval import hybrid_search
from app.modules.knowledge.service import KnowledgeService


async def test_shadow_index_and_acl_filtered_keyword_retrieval() -> None:
    settings = get_settings()
    initialize_postgres(settings.postgres_dsn)
    document_no = "kdoc_phase09_integration"
    job_no = "idx_phase09_integration"
    command_no = "job_phase09_integration"
    provider = DisabledEmbeddingProvider("ecom-multilingual-v1", 1536)
    try:
        async for session in postgres_session():
            await session.execute(
                text("DELETE FROM knowledge.retrieval_logs WHERE trace_id='phase09-integration'")
            )
            await session.execute(
                text("DELETE FROM knowledge.document_chunks WHERE document_no=:document_no"),
                {"document_no": document_no},
            )
            await session.execute(
                text("DELETE FROM knowledge.index_generations WHERE document_no=:document_no"),
                {"document_no": document_no},
            )
            await session.execute(
                text("DELETE FROM knowledge.indexing_jobs WHERE command_job_no=:command_no"),
                {"command_no": command_no},
            )
            await session.execute(
                text("""INSERT INTO knowledge.indexing_jobs
                (job_no, command_job_no, scope_type, scope_no, job_status, progress)
                VALUES (:job_no,:command_no,'store','sto_phase09','queued',0)"""),
                {"job_no": job_no, "command_no": command_no},
            )
            await session.commit()
            await run_index_job(
                session,
                {
                    "document_no": document_no,
                    "content_version": "kver_phase09",
                    "scope_type": "store",
                    "scope_no": "sto_phase09",
                    "safe_text": "refund policy allows return within seven days",
                },
                job_no,
                provider,
            )
            allowed = await hybrid_search(
                session,
                provider,
                query="refund policy",
                scope_type="store",
                scope_no="sto_phase09",
                limit=5,
                trace_id="phase09-integration",
            )
            denied = await hybrid_search(
                session,
                provider,
                query="refund policy",
                scope_type="store",
                scope_no="sto_other",
                limit=5,
                trace_id="phase09-integration",
            )
            assert [item.document_no for item in allowed.chunks] == [document_no]
            assert all(item.chunk_no.startswith("kch_") for item in allowed.chunks)
            assert allowed.degraded is True
            assert denied.chunks == []
            retrieval_nos = list(
                (
                    await session.scalars(
                        text(
                            "SELECT retrieval_no FROM knowledge.retrieval_logs "
                            "WHERE trace_id='phase09-integration'"
                        )
                    )
                ).all()
            )
            assert retrieval_nos
            assert all(item.startswith("rtv_") for item in retrieval_nos)
            await session.execute(
                text("DELETE FROM knowledge.retrieval_logs WHERE trace_id='phase09-integration'")
            )
            await session.execute(
                text("DELETE FROM knowledge.document_chunks WHERE document_no=:document_no"),
                {"document_no": document_no},
            )
            await session.execute(
                text("DELETE FROM knowledge.indexing_jobs WHERE command_job_no=:command_no"),
                {"command_no": command_no},
            )
            await session.execute(
                text("DELETE FROM knowledge.index_generations WHERE document_no=:document_no"),
                {"document_no": document_no},
            )
            await session.commit()
    finally:
        await close_postgres()


async def test_agent_retrieval_rechecks_trusted_scope_version_and_publication(
    client: AsyncClient,
) -> None:
    del client
    suffix = secrets.token_hex(6)
    allowed_no = f"kdoc_agent_{suffix}"
    foreign_no = f"kdoc_foreign_{suffix}"
    scope_no = f"sto_agent_{suffix}"
    foreign_scope_no = f"sto_foreign_{suffix}"
    allowed_job_no = f"idx_agent_{suffix}"
    foreign_job_no = f"idx_foreign_{suffix}"
    allowed_command_no = f"job_agent_{suffix}"
    foreign_command_no = f"job_foreign_{suffix}"
    content_version = f"kver_{suffix}"
    provider = DisabledEmbeddingProvider("ecom-multilingual-v1", 1536)

    async for mysql in mysql_session():
        mysql.add_all(
            [
                KnowledgeDocument(
                    document_no=allowed_no,
                    scope_type="store",
                    scope_no=scope_no,
                    title="本店退换政策",
                    safe_text="agent-scope-keyword 本店支持七天退换。",
                    document_status="published",
                    content_version=content_version,
                ),
                KnowledgeDocument(
                    document_no=foreign_no,
                    scope_type="store",
                    scope_no=foreign_scope_no,
                    title="其他店铺保密政策",
                    safe_text="agent-scope-keyword 其他店铺不可见内容。",
                    document_status="published",
                    content_version=content_version,
                ),
            ]
        )
        await mysql.commit()
        try:
            async for postgres in postgres_session():
                for job_no, command_no, indexed_scope in (
                    (allowed_job_no, allowed_command_no, scope_no),
                    (foreign_job_no, foreign_command_no, foreign_scope_no),
                ):
                    await postgres.execute(
                        text(
                            """INSERT INTO knowledge.indexing_jobs
                            (job_no, command_job_no, scope_type, scope_no, job_status, progress)
                            VALUES (:job_no,:command_no,'store',:scope_no,'queued',0)"""
                        ),
                        {"job_no": job_no, "command_no": command_no, "scope_no": indexed_scope},
                    )
                await postgres.commit()
                await run_index_job(
                    postgres,
                    {
                        "document_no": allowed_no,
                        "content_version": content_version,
                        "scope_type": "store",
                        "scope_no": scope_no,
                        "safe_text": "agent-scope-keyword 本店支持七天退换。",
                    },
                    allowed_job_no,
                    provider,
                )
                await run_index_job(
                    postgres,
                    {
                        "document_no": foreign_no,
                        "content_version": content_version,
                        "scope_type": "store",
                        "scope_no": foreign_scope_no,
                        "safe_text": "agent-scope-keyword 其他店铺不可见内容。",
                    },
                    foreign_job_no,
                    provider,
                )
                service = KnowledgeService(mysql, postgres)
                result = await service.search_for_agent(
                    query="agent-scope-keyword",
                    scope_type="store",
                    scope_no=scope_no,
                    limit=20,
                    trace_id=f"agent-rag-{suffix}",
                )
                assert [item.document_id for item in result.items] == [allowed_no]
                assert all("其他店铺" not in item.excerpt for item in result.items)

                allowed = await mysql.scalar(
                    text("SELECT id FROM knowledge_documents WHERE document_no=:document_no"),
                    {"document_no": allowed_no},
                )
                assert allowed is not None
                document = await mysql.get(KnowledgeDocument, int(allowed))
                assert document is not None
                document.document_status = "draft"
                await mysql.commit()
                unpublished = await service.search_for_agent(
                    query="agent-scope-keyword",
                    scope_type="store",
                    scope_no=scope_no,
                    limit=6,
                    trace_id=f"agent-rag-unpublished-{suffix}",
                )
                assert unpublished.items == []

                await postgres.execute(
                    text(
                        "DELETE FROM knowledge.retrieval_logs "
                        "WHERE trace_id IN (:trace_id, :unpublished_trace_id)"
                    ),
                    {
                        "trace_id": f"agent-rag-{suffix}",
                        "unpublished_trace_id": f"agent-rag-unpublished-{suffix}",
                    },
                )
                await postgres.execute(
                    text(
                        "DELETE FROM knowledge.document_chunks "
                        "WHERE document_no IN (:allowed_no, :foreign_no)"
                    ),
                    {"allowed_no": allowed_no, "foreign_no": foreign_no},
                )
                await postgres.execute(
                    text(
                        "DELETE FROM knowledge.indexing_jobs "
                        "WHERE command_job_no IN (:allowed_command, :foreign_command)"
                    ),
                    {
                        "allowed_command": allowed_command_no,
                        "foreign_command": foreign_command_no,
                    },
                )
                await postgres.execute(
                    text(
                        "DELETE FROM knowledge.index_generations "
                        "WHERE document_no IN (:allowed_no, :foreign_no)"
                    ),
                    {"allowed_no": allowed_no, "foreign_no": foreign_no},
                )
                await postgres.commit()
                break
        finally:
            await mysql.execute(
                delete(KnowledgeDocument).where(
                    KnowledgeDocument.document_no.in_([allowed_no, foreign_no])
                )
            )
            await mysql.commit()
        break
