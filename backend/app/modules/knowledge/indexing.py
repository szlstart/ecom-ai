from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast

import httpx
from sqlalchemy import select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.modules.knowledge.embedding import (
    EmbeddingProvider,
    EmbeddingUnavailable,
    vector_literal,
)
from app.modules.knowledge.models import KnowledgeDocument
from app.modules.system.models import AdminBatchJob


@dataclass(frozen=True)
class IndexJobState:
    job_no: str
    command_job_no: str
    status: str
    progress: int
    error_code: str | None


async def create_index_job(
    mysql: AsyncSession,
    postgres: AsyncSession,
    document: KnowledgeDocument,
    *,
    requested_by: int,
    trace_id: str,
    scope_id: int,
    request_key: str,
    embedding_model_code: str,
) -> IndexJobState:
    """Create the MySQL command and PostgreSQL execution child idempotently."""
    candidates = list(
        (
            await mysql.scalars(
                select(AdminBatchJob).where(
                    AdminBatchJob.execution_backend == "postgres_knowledge",
                    AdminBatchJob.job_type == "knowledge_index",
                )
            )
        ).all()
    )
    command = next(
        (
            item
            for item in candidates
            if item.request_config.get("request_key") == request_key
        ),
        None,
    )
    if command is None:
        command_no = new_prefixed_ulid("job_")
        command = AdminBatchJob(
            job_no=command_no,
            job_type="knowledge_index",
            requested_by=requested_by,
            scope_type=document.scope_type,
            scope_id=scope_id,
            permission_code="knowledge:manage",
            request_config={
                "document_no": document.document_no,
                "content_version": document.content_version,
                "scope_no": document.scope_no,
                "request_key": request_key,
                "embedding_model_code": embedding_model_code,
            },
            request_hash=hashlib.sha256(
                f"{document.document_no}:{document.content_version}:{embedding_model_code}:{request_key}".encode()
            ).digest(),
            job_status="queued",
            execution_backend="postgres_knowledge",
            execution_job_no=new_prefixed_ulid("idx_"),
            total_count=1,
            trace_id=trace_id,
        )
        mysql.add(command)
        await mysql.flush()
    child_no = command.execution_job_no or new_prefixed_ulid("idx_")
    command.execution_job_no = child_no
    command.execution_backend = "postgres_knowledge"
    await postgres.execute(
        text(
            """INSERT INTO knowledge.indexing_jobs
            (job_no, command_job_no, scope_type, scope_no, job_status, progress)
            VALUES (:job_no, :command_job_no, :scope_type, :scope_no, 'queued', 0)
            ON CONFLICT (command_job_no) DO NOTHING"""
        ),
        {
            "job_no": child_no,
            "command_job_no": command.job_no,
            "scope_type": document.scope_type,
            "scope_no": document.scope_no,
        },
    )
    await mysql.commit()
    await postgres.commit()
    return IndexJobState(
        child_no,
        command.job_no,
        command.job_status,
        100 if command.job_status == "succeeded" else 0,
        command.error_code,
    )


async def run_index_job(
    postgres: AsyncSession,
    document: KnowledgeDocument | dict[str, object],
    job_no: str,
    embedder: EmbeddingProvider,
) -> IndexJobState:
    """Build a shadow generation and atomically switch the active document index."""
    if isinstance(document, dict):
        document_no = str(document["document_no"])
        version = str(document["content_version"])
        scope_type = str(document["scope_type"])
        scope_no = str(document["scope_no"])
        body = str(document["safe_text"])
    else:
        document_no, version, scope_type, scope_no, body = (
            document.document_no,
            document.content_version,
            document.scope_type,
            document.scope_no,
            document.safe_text,
        )
    generation_no = new_prefixed_ulid("kgen_")
    await postgres.execute(
        text("""INSERT INTO knowledge.embedding_models
        (model_code, provider, dimension, model_status)
        VALUES (:model_code, 'configured-provider', :dimension, 'active')
        ON CONFLICT (model_code) DO UPDATE
        SET model_status='active'
        WHERE knowledge.embedding_models.dimension=EXCLUDED.dimension"""),
        {"model_code": embedder.model_code, "dimension": embedder.dimension},
    )
    await postgres.execute(
        text("""INSERT INTO knowledge.index_generations
        (generation_no, document_no, scope_type, scope_no, model_code, generation_status)
        VALUES (:generation_no,:document_no,:scope_type,:scope_no,:model_code,'building')"""),
        {
            "generation_no": generation_no,
            "document_no": document_no,
            "scope_type": scope_type,
            "scope_no": scope_no,
            "model_code": embedder.model_code,
        },
    )
    claimed = cast(
        CursorResult[Any],
        await postgres.execute(
            text("""UPDATE knowledge.indexing_jobs
            SET job_status='running', progress=5,
                generation_no=:generation_no, status_version=status_version+1, updated_at=now()
            WHERE job_no=:job_no AND job_status IN ('queued','running')"""),
            {"job_no": job_no, "generation_no": generation_no},
        ),
    )
    if claimed.rowcount != 1:
        raise RuntimeError("knowledge index job is no longer executable")
    chunks = chunk_text(body)
    try:
        embeddings: list[list[float] | None] = list(await embedder.embed(chunks))
    except (EmbeddingUnavailable, httpx.HTTPError):
        embeddings = [None] * len(chunks)
    for index, chunk in enumerate(chunks):
        embedding = embeddings[index]
        chunk_no = (
            "kch_" + hashlib.sha256(f"{document_no}:{version}:{index}".encode()).hexdigest()[:30]
        )
        await postgres.execute(
            text(
                """INSERT INTO knowledge.document_chunks
                (chunk_no, document_no, content_version, generation_no, scope_type, scope_no,
                 safe_text, embedding, embedding_model_code, metadata)
                VALUES (:chunk_no,:document_no,:version,:generation_no,:scope_type,:scope_no,
                        :safe_text,CAST(:embedding AS vector),:model_code,CAST(:metadata AS jsonb))
                ON CONFLICT (document_no, content_version, chunk_no)
                DO UPDATE SET safe_text=EXCLUDED.safe_text, embedding=EXCLUDED.embedding,
                    generation_no=EXCLUDED.generation_no,
                    embedding_model_code=EXCLUDED.embedding_model_code,
                    metadata=EXCLUDED.metadata"""
            ),
            {
                "chunk_no": chunk_no,
                "document_no": document_no,
                "version": version,
                "generation_no": generation_no,
                "scope_type": scope_type,
                "scope_no": scope_no,
                "safe_text": chunk,
                "embedding": vector_literal(embedding) if embedding is not None else None,
                "model_code": embedder.model_code,
                "metadata": json.dumps(
                    {"chunk_index": index, "index_version": version}, separators=(",", ":")
                ),
            },
        )
    await postgres.execute(
        text("""UPDATE knowledge.index_generations
        SET generation_status='retired'
        WHERE document_no=:document_no AND generation_status='active'"""),
        {"document_no": document_no},
    )
    await postgres.execute(
        text("""UPDATE knowledge.index_generations
        SET generation_status='active', activated_at=now()
        WHERE generation_no=:generation_no"""),
        {"generation_no": generation_no},
    )
    await postgres.execute(
        text("""UPDATE knowledge.indexing_jobs
        SET job_status='succeeded', progress=100,
            status_version=status_version+1, updated_at=now()
        WHERE job_no=:job_no"""),
        {"job_no": job_no},
    )
    await postgres.execute(
        text("""DELETE FROM knowledge.document_chunks
        WHERE document_no=:document_no AND generation_no<>:generation_no"""),
        {"document_no": document_no, "generation_no": generation_no},
    )
    await postgres.commit()
    return IndexJobState(job_no, "", "succeeded", 100, None)


def chunk_text(body: str, *, size: int = 1200, overlap: int = 160) -> list[str]:
    """Create stable, overlapping chunks without emitting whitespace-only records."""
    normalized = "\n".join(line.strip() for line in body.splitlines() if line.strip())
    if not normalized:
        return [""]
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + size, len(normalized))
        if end < len(normalized):
            boundary = max(normalized.rfind("\n", start, end), normalized.rfind("。", start, end))
            if boundary > start + size // 2:
                end = boundary + 1
        chunks.append(normalized[start:end])
        if end == len(normalized):
            break
        start = max(start + 1, end - overlap)
    return chunks


async def reconcile_index_job(
    mysql: AsyncSession, postgres: AsyncSession, command_job_no: str
) -> IndexJobState | None:
    parent = await mysql.scalar(
        select(AdminBatchJob).where(AdminBatchJob.job_no == command_job_no).with_for_update()
    )
    row = (
        (
            await postgres.execute(
                text("""SELECT job_no, command_job_no, job_status, progress, error_code,
                    status_version
                FROM knowledge.indexing_jobs WHERE command_job_no=:no"""),
                {"no": command_job_no},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        if (
            parent is None
            or parent.execution_backend != "postgres_knowledge"
            or not parent.execution_job_no
        ):
            return None
        scope_no = parent.request_config.get("scope_no")
        if not isinstance(scope_no, str):
            return None
        await postgres.execute(
            text("""INSERT INTO knowledge.indexing_jobs
            (job_no, command_job_no, scope_type, scope_no, job_status, progress)
            VALUES (:job_no,:command_job_no,:scope_type,:scope_no,'queued',0)
            ON CONFLICT (command_job_no) DO NOTHING"""),
            {
                "job_no": parent.execution_job_no,
                "command_job_no": parent.job_no,
                "scope_type": parent.scope_type,
                "scope_no": scope_no,
            },
        )
        await postgres.commit()
        return IndexJobState(parent.execution_job_no, parent.job_no, "queued", 0, None)
    if parent is None:
        return IndexJobState(
            row["job_no"], command_job_no, row["job_status"], row["progress"], row["error_code"]
        )
    if int(row["status_version"]) > parent.execution_status_version:
        parent.job_status = "succeeded" if row["job_status"] == "succeeded" else row["job_status"]
        parent.execution_status_version = int(row["status_version"])
        parent.success_count = 1 if row["job_status"] == "succeeded" else 0
        parent.failure_count = 1 if row["job_status"] == "failed" else 0
        parent.error_code = row["error_code"]
    await mysql.commit()
    return IndexJobState(
        row["job_no"], row["command_job_no"], row["job_status"], row["progress"], row["error_code"]
    )


async def cancel_index_job(
    mysql: AsyncSession, postgres: AsyncSession, command_job_no: str
) -> IndexJobState:
    parent = await mysql.scalar(
        select(AdminBatchJob).where(AdminBatchJob.job_no == command_job_no).with_for_update()
    )
    if parent is None or parent.execution_backend != "postgres_knowledge":
        raise LookupError("knowledge index command not found")
    if parent.job_status not in {"succeeded", "failed", "cancelled"}:
        parent.cancel_requested_at = utc_now()
        parent.job_status = "cancelled"
        parent.execution_status_version += 1
    await postgres.execute(
        text("""UPDATE knowledge.indexing_jobs
        SET job_status='cancelled', status_version=status_version+1, updated_at=now()
        WHERE command_job_no=:command_job_no AND job_status IN ('queued','running')"""),
        {"command_job_no": command_job_no},
    )
    await mysql.commit()
    await postgres.commit()
    return IndexJobState(
        parent.execution_job_no or "", parent.job_no, parent.job_status, 0, parent.error_code
    )
