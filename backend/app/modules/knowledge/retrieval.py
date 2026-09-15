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
    parent_section_id: str | None = None
    section_type: str | None = None


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
            item.document_no,
            item.content_version,
            item.text,
            scores[key],
            item.chunk_no,
            item.parent_section_id,
            item.section_type,
        )
        for key, item in sorted(values.items(), key=lambda pair: scores[pair[0]], reverse=True)
    ]


def lexical_search(
    texts: Sequence[RetrievedChunk], query: str, limit: int = 20
) -> list[RetrievedChunk]:
    terms = _query_terms(query)
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
                    chunk.parent_section_id,
                    chunk.section_type,
                )
            )
    return sorted(ranked, key=lambda item: (-item.score, item.document_no))[:limit]


def rerank(chunks: Sequence[RetrievedChunk], query: str) -> list[RetrievedChunk]:
    """Rerank deterministically; a configured provider may replace this implementation."""
    terms = set(_query_terms(query))
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
    min_vector_similarity: float = 0.35,
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
                              chunk.metadata->>'parent_section_id' AS parent_section_id,
                              chunk.metadata->>'section_type' AS section_type,
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
    # PostgreSQL's `simple` text-search configuration does not segment Chinese
    # sentences into useful business terms.  Add a bounded ILIKE candidate pass
    # and score those candidates with the same deterministic term set.  This is
    # still ACL-filtered by scope and active index generation.
    query_terms = [term for term in _query_terms(query) if len(term) >= 2][:20]
    if query_terms:
        fallback_rows = (
            (
                await session.execute(
                    text(
                        """SELECT chunk.chunk_no, chunk.document_no, chunk.content_version,
                                  chunk.safe_text,
                                  chunk.metadata->>'parent_section_id' AS parent_section_id,
                                  chunk.metadata->>'section_type' AS section_type,
                                  0.0 AS score
                           FROM knowledge.document_chunks AS chunk
                           JOIN knowledge.index_generations AS generation
                             ON generation.generation_no = chunk.generation_no
                            AND generation.generation_status = 'active'
                           WHERE chunk.scope_type=:scope_type AND chunk.scope_no=:scope_no
                             AND chunk.safe_text ILIKE ANY(CAST(:patterns AS text[]))
                           ORDER BY chunk.chunk_no
                           LIMIT 80"""
                    ),
                    {
                        "scope_type": scope_type,
                        "scope_no": scope_no,
                        "patterns": [f"%{term}%" for term in query_terms],
                    },
                )
            )
            .mappings()
            .all()
        )
        fallback = lexical_search(
            [_row(cast(Mapping[str, Any], item)) for item in fallback_rows],
            query,
            40,
        )
        by_chunk = {item.chunk_no: item for item in lexical}
        for item in fallback:
            existing = by_chunk.get(item.chunk_no)
            if existing is None or item.score > existing.score:
                by_chunk[item.chunk_no] = item
        lexical = sorted(by_chunk.values(), key=lambda item: (-item.score, item.chunk_no))[:40]
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
                                  chunk.metadata->>'parent_section_id' AS parent_section_id,
                                  chunk.metadata->>'section_type' AS section_type,
                                  1 - (chunk.embedding <=> CAST(:embedding AS vector)) AS score
                           FROM knowledge.document_chunks AS chunk
                           JOIN knowledge.index_generations AS generation
                             ON generation.generation_no = chunk.generation_no
                            AND generation.generation_status = 'active'
                           WHERE chunk.scope_type=:scope_type AND chunk.scope_no=:scope_no
                             AND chunk.embedding IS NOT NULL
                             AND chunk.embedding_model_code=:model_code
                             AND 1 - (chunk.embedding <=> CAST(:embedding AS vector))
                                 >= :min_vector_similarity
                           ORDER BY chunk.embedding <=> CAST(:embedding AS vector), chunk.chunk_no
                           LIMIT 40"""
                    ),
                    {
                        **base_parameters,
                        "embedding": vector_literal(query_embedding),
                        "model_code": embedder.model_code,
                        "min_vector_similarity": min_vector_similarity,
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
    fused = await _expand_parent_sections(session, fused)
    await session.execute(
        text(
            """INSERT INTO knowledge.retrieval_logs
               (retrieval_no, trace_id, query_hash, scope_type, scope_no,
                embedding_model_code, candidate_count, returned_count, degraded, latency_ms)
               VALUES (:retrieval_no,:trace_id,:query_hash,:scope_type,:scope_no,
                       :model_code,:candidate_count,:returned_count,:degraded,:latency_ms)"""
        ),
        {
            "retrieval_no": new_prefixed_ulid("rtv_"),
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
        parent_section_id=(
            str(row["parent_section_id"]) if row.get("parent_section_id") is not None else None
        ),
        section_type=(str(row["section_type"]) if row.get("section_type") is not None else None),
    )


async def _expand_parent_sections(
    session: AsyncSession,
    chunks: Sequence[RetrievedChunk],
) -> list[RetrievedChunk]:
    """Replace matched children with their bounded semantic parent section."""

    selected = [item.chunk_no for item in chunks if item.chunk_no]
    if not selected:
        return list(chunks)
    rows = (
        (
            await session.execute(
                text(
                    """SELECT selected.chunk_no AS selected_chunk_no,
                              sibling.safe_text,
                              sibling.metadata->>'parent_section_id' AS parent_section_id,
                              sibling.metadata->>'section_type' AS section_type
                       FROM knowledge.document_chunks AS selected
                       JOIN knowledge.index_generations AS generation
                         ON generation.generation_no=selected.generation_no
                        AND generation.generation_status='active'
                       JOIN knowledge.document_chunks AS sibling
                         ON sibling.generation_no=selected.generation_no
                        AND sibling.document_no=selected.document_no
                        AND sibling.content_version=selected.content_version
                        AND sibling.metadata->>'parent_section_id'=
                            selected.metadata->>'parent_section_id'
                       WHERE selected.chunk_no=ANY(CAST(:selected AS text[]))
                         AND selected.metadata->>'parent_section_id' IS NOT NULL
                       ORDER BY selected.chunk_no,
                                CAST(sibling.metadata->>'chunk_index' AS integer),
                                sibling.chunk_no"""
                ),
                {"selected": selected},
            )
        )
        .mappings()
        .all()
    )
    by_selected: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        selected_no = str(row["selected_chunk_no"])
        by_selected.setdefault(selected_no, []).append(cast(Mapping[str, Any], row))
    expanded: list[RetrievedChunk] = []
    seen_parent: set[tuple[str, str, str]] = set()
    for item in chunks:
        siblings = by_selected.get(item.chunk_no)
        if not siblings:
            expanded.append(item)
            continue
        parent_id = str(siblings[0].get("parent_section_id") or item.chunk_no)
        identity = (item.document_no, item.content_version, parent_id)
        if identity in seen_parent:
            continue
        seen_parent.add(identity)
        parent_text = _join_overlapping_chunks(
            [str(row.get("safe_text") or "") for row in siblings]
        )[:2400]
        expanded.append(
            RetrievedChunk(
                document_no=item.document_no,
                content_version=item.content_version,
                text=parent_text or item.text,
                score=item.score,
                chunk_no=item.chunk_no,
                parent_section_id=parent_id,
                section_type=str(siblings[0].get("section_type") or "general"),
            )
        )
    return expanded


def _join_overlapping_chunks(values: Sequence[str]) -> str:
    result = ""
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        if not result:
            result = value
            continue
        overlap = 0
        maximum = min(160, len(result), len(value))
        for size in range(maximum, 19, -1):
            if result.endswith(value[:size]):
                overlap = size
                break
        result += "\n" + value[overlap:]
    return result


_CHINESE_DOMAIN_TERMS = (
    # Keep specific commerce phrases before their shorter components.  Chinese
    # PostgreSQL `simple` text search cannot segment these phrases for us, so
    # query-side extraction must preserve the business meaning instead of
    # reducing every policy question to the generic word ``规则``.
    "商家商品审核",
    "商品自动审核",
    "商品审核规则",
    "商品审核",
    "自动审核",
    "审核不通过",
    "违禁内容",
    "违禁词",
    "禁售规则",
    "禁售商品",
    "商家经营",
    "商家规则",
    "平台规则",
    "商品资料",
    "提交审核",
    "重新审核",
    "商品上架",
    "商品下架",
    "删除商品",
    "自动确认收货",
    "自动推进",
    "自动更新",
    "固定几秒",
    "每隔五秒",
    "每五秒",
    "每隔5秒",
    "每5秒",
    "物流节点",
    "模拟物流",
    "退款到账",
    "退款进度",
    "售后资格",
    "确认收货",
    "收货地址",
    "人工客服",
    "支付方式",
    "发货时间",
    "配送方式",
    "物流",
    "快递",
    "包裹",
    "退款",
    "退货",
    "售后",
    "到账",
    "运费",
    "包邮",
    "暂停营业",
    "恢复营业",
    "店铺经营",
    "商家",
    "店铺",
    "审核",
    "禁售",
    "违禁",
    "上架",
    "下架",
    "购物车",
    "不可购买",
    "结算",
    "新订单",
    "发货",
    "签收",
    "支付",
    "余额",
    "账号",
    "隐私",
    "密码",
    "更新",
    "推进",
    "固定",
    "规则",
)


def _query_terms(query: str) -> list[str]:
    """Extract bounded Chinese business terms instead of one full sentence token."""

    normalized = re.sub(r"\s+", "", query.casefold())
    terms = [term for term in _CHINESE_DOMAIN_TERMS if term in normalized]
    terms.extend(token for token in re.findall(r"[a-z0-9_]{2,}", normalized) if token not in terms)
    if not terms:
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
        terms.extend(
            chinese[index : index + 2]
            for index in range(max(0, len(chinese) - 1))
            if chinese[index : index + 2]
        )
    return list(dict.fromkeys(terms))[:24]
