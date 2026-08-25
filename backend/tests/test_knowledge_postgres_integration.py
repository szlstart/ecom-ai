from sqlalchemy import text

from app.core.config import get_settings
from app.database.postgres import close_postgres, initialize_postgres, postgres_session
from app.modules.knowledge.embedding import DisabledEmbeddingProvider
from app.modules.knowledge.indexing import run_index_job
from app.modules.knowledge.retrieval import hybrid_search


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
