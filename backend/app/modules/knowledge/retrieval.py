from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import new_prefixed_ulid
from app.modules.knowledge.embedding import (
    EmbeddingProvider,
    EmbeddingUnavailable,
    vector_literal,
)


@dataclass(frozen=True)
class RetrievedChunk:
    document_no: str
    content_version: str
    text: str
    score: float
    chunk_no: str = ""


def reciprocal_rank_fusion(
    rankings: list[list[RetrievedChunk]], *, constant: int = 60
) -> list[RetrievedChunk]:
    scores: dict[tuple[str, str], float] = {}
    values: dict[tuple[str, str], RetrievedChunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            identity = chunk.chunk_no or f"{chunk.document_no}:{hash(chunk.text)}"
            key = (identity, chunk.content_version)
            scores[key] = scores.get(key, 0.0) + 1.0 / (constant + rank)
            values[key] = chunk
    return [
        RetrievedChunk(
            item.document_no, item.content_version, item.text, scores[key], item.chunk_no
        )
        for key, item in sorted(values.items(), key=lambda pair: scores[pair[0]], reverse=True)
    ]


def lexical_search(
    texts: Sequence[RetrievedChunk], query: str, limit: int = 20
) -> list[RetrievedChunk]:
    terms = [term for term in re.findall(r"[\w\u4e00-\u9fff]+", query.casefold()) if term]
    if not terms:
        return []
    ranked: list[RetrievedChunk] = []
    for chunk in texts:
        score = sum(chunk.text.casefold().count(term) for term in terms)
        if score:
            ranked.append(
                RetrievedChunk(
                    chunk.document_no,
                    chunk.content_version,
                    chunk.text,
                    float(score),
                    chunk.chunk_no,
                )
            )
    return sorted(ranked, key=lambda item: (-item.score, item.document_no))[:limit]


def rerank(chunks: Sequence[RetrievedChunk], query: str) -> list[RetrievedChunk]:
    """Rerank deterministically; a configured provider may replace this implementation."""
    terms = {term for term in re.findall(r"[\w\u4e00-\u9fff]+", query.casefold()) if term}
    if not terms:
        return list(chunks)

    def score(item: RetrievedChunk) -> tuple[float, str]:
        text_value = item.text.casefold()
        coverage = sum(1 for term in terms if term in text_value) / len(terms)
        phrase = 1.0 if query.casefold() in text_value else 0.0
        return (item.score + coverage * 0.02 + phrase * 0.01, item.chunk_no)

    return sorted(chunks, key=score, reverse=True)


@dataclass(frozen=True)
class HybridRetrieval:
    chunks: list[RetrievedChunk]
    degraded: bool


async def hybrid_search(
    session: AsyncSession,
    embedder: EmbeddingProvider,
    *,
    query: str,
    scope_type: str,
    scope_no: str,
    limit: int,
    trace_id: str,
) -> HybridRetrieval:
    """Run ACL-filtered keyword/vector retrieval and fuse rankings by chunk identity."""
    started = time.monotonic()
    base_parameters = {"query": query, "scope_type": scope_type, "scope_no": scope_no}
    lexical_rows = (
        (
            await session.execute(
                text(
                    """SELECT chunk.chunk_no, chunk.document_no, chunk.content_version,
                              chunk.safe_text,
                              ts_rank_cd(chunk.search_vector,
                                  websearch_to_tsquery('simple', :query)) AS score
                       FROM knowledge.document_chunks AS chunk
                       JOIN knowledge.index_generations AS generation
                         ON generation.generation_no = chunk.generation_no
                        AND generation.generation_status = 'active'
                       WHERE chunk.scope_type=:scope_type AND chunk.scope_no=:scope_no
                         AND chunk.search_vector @@ websearch_to_tsquery('simple', :query)
                       ORDER BY score DESC, chunk.chunk_no
                       LIMIT 40"""
                ),
                base_parameters,
            )
        )
        .mappings()
        .all()
    )
    lexical = [_row(cast(Mapping[str, Any], item)) for item in lexical_rows]
    vector: list[RetrievedChunk] = []
    degraded = False
    try:
        query_embedding = (await embedder.embed([query]))[0]
        vector_rows = (
            (
                await session.execute(
                    text(
                        """SELECT chunk.chunk_no, chunk.document_no, chunk.content_version,
                                  chunk.safe_text,
                                  1 - (chunk.embedding <=> CAST(:embedding AS vector)) AS score
                           FROM knowledge.document_chunks AS chunk
                           JOIN knowledge.index_generations AS generation
                             ON generation.generation_no = chunk.generation_no
                            AND generation.generation_status = 'active'
                           WHERE chunk.scope_type=:scope_type AND chunk.scope_no=:scope_no
                             AND chunk.embedding IS NOT NULL
                             AND chunk.embedding_model_code=:model_code
                           ORDER BY chunk.embedding <=> CAST(:embedding AS vector), chunk.chunk_no
                           LIMIT 40"""
                    ),
                    {
                        **base_parameters,
                        "embedding": vector_literal(query_embedding),
                        "model_code": embedder.model_code,
                    },
                )
            )
            .mappings()
            .all()
        )
        vector = [_row(cast(Mapping[str, Any], item)) for item in vector_rows]
    except (EmbeddingUnavailable, httpx.HTTPError):
        degraded = True
    fused = rerank(reciprocal_rank_fusion([lexical, vector] if vector else [lexical]), query)[
        :limit
    ]
    await session.execute(
        text(
            """INSERT INTO knowledge.retrieval_logs
               (retrieval_no, trace_id, query_hash, scope_type, scope_no,
                embedding_model_code, candidate_count, returned_count, degraded, latency_ms)
               VALUES (:retrieval_no,:trace_id,:query_hash,:scope_type,:scope_no,
                       :model_code,:candidate_count,:returned_count,:degraded,:latency_ms)"""
        ),
        {
            "retrieval_no": new_prefixed_ulid("ret_"),
            "trace_id": trace_id,
            "query_hash": hashlib.sha256(query.encode()).digest(),
            "scope_type": scope_type,
            "scope_no": scope_no,
            "model_code": embedder.model_code,
            "candidate_count": len(lexical) + len(vector),
            "returned_count": len(fused),
            "degraded": degraded,
            "latency_ms": int((time.monotonic() - started) * 1000),
        },
    )
    await session.commit()
    return HybridRetrieval(fused, degraded)


def _row(row: Mapping[str, Any]) -> RetrievedChunk:
    return RetrievedChunk(
        document_no=str(row["document_no"]),
        content_version=str(row["content_version"]),
        text=str(row["safe_text"]),
        score=float(row["score"]),
        chunk_no=str(row["chunk_no"]),
    )
