from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import SecurityService, utc_now
from app.modules.agent_runtime.models import UserAgentConsent
from app.modules.identity.models import User


@dataclass(frozen=True)
class RecalledMemory:
    memory_no: str
    memory_type: str
    memory_key: str
    value: str
    relevance: float
    expires_at: datetime


@dataclass(frozen=True)
class MemoryRecall:
    items: tuple[RecalledMemory, ...]
    authorized: bool
    degraded: bool = False


class AgentMemoryRuntime:
    """Read-only, consent-gated memory projection for an Agent context pack."""

    def __init__(
        self,
        mysql: AsyncSession,
        postgres: AsyncSession,
        security: SecurityService,
    ) -> None:
        self.mysql = mysql
        self.postgres = postgres
        self.security = security

    async def recall_exclusive(
        self,
        user: User,
        *,
        query: str,
        limit: int = 3,
    ) -> MemoryRecall:
        now = utc_now()
        consent_nos = tuple(
            str(item)
            for item in (
                await self.mysql.scalars(
                    select(UserAgentConsent.consent_no).where(
                        UserAgentConsent.user_id == user.id,
                        UserAgentConsent.consent_type == "personalization",
                        UserAgentConsent.consent_status == "active",
                        or_(
                            UserAgentConsent.expires_at.is_(None),
                            UserAgentConsent.expires_at > now,
                        ),
                    )
                )
            ).all()
        )
        if not consent_nos:
            return MemoryRecall(items=(), authorized=False)
        rows = (
            (
                await self.postgres.execute(
                    text(
                        """SELECT memory_no,memory_type,memory_key,content_ciphertext,
                        confidence,salience,expires_at
                        FROM memory.items
                        WHERE user_no=:user_no AND namespace='exclusive' AND store_no IS NULL
                          AND memory_status='active' AND content_ciphertext IS NOT NULL
                          AND consent_no = ANY(:consent_nos)
                          AND expires_at > now()
                          AND (valid_until IS NULL OR valid_until > now())
                        ORDER BY salience DESC, confidence DESC, updated_at DESC
                        LIMIT 24"""
                    ),
                    {"user_no": user.user_no, "consent_nos": list(consent_nos)},
                )
            )
            .mappings()
            .all()
        )
        terms = _query_terms(query)
        candidates: list[RecalledMemory] = []
        for row in rows:
            ciphertext = row["content_ciphertext"]
            if not isinstance(ciphertext, bytes):
                continue
            try:
                value = self.security.decrypt("ai-memory-content", ciphertext).strip()
            except Exception:
                continue
            if not value or _looks_sensitive(value):
                continue
            relevance = _relevance(
                value,
                str(row["memory_type"]),
                terms,
                Decimal(str(row["confidence"])),
                Decimal(str(row["salience"])),
            )
            if relevance < 0.35:
                continue
            candidates.append(
                RecalledMemory(
                    memory_no=str(row["memory_no"]),
                    memory_type=str(row["memory_type"]),
                    memory_key=str(row["memory_key"]),
                    value=value[:500],
                    relevance=relevance,
                    expires_at=row["expires_at"],
                )
            )
        candidates.sort(key=lambda item: (-item.relevance, item.memory_no))
        return MemoryRecall(items=tuple(candidates[: min(max(limit, 1), 5)]), authorized=True)


def _query_terms(value: str) -> frozenset[str]:
    return frozenset(
        term
        for term in re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", value.casefold())
        if term not in {"帮我", "一下", "请问", "商品", "推荐"}
    )


def _relevance(
    value: str,
    memory_type: str,
    terms: frozenset[str],
    confidence: Decimal,
    salience: Decimal,
) -> float:
    normalized = value.casefold()
    overlap = sum(1 for term in terms if term in normalized)
    type_weight = 0.55 if memory_type in {"preference", "constraint"} else 0.35
    return min(
        1.0,
        type_weight + overlap * 0.15 + float(confidence) * 0.15 + float(salience) * 0.15,
    )


def _looks_sensitive(value: str) -> bool:
    lowered = value.casefold()
    forbidden_tokens = (
        "password",
        "密码",
        "验证码",
        "api key",
        "apikey",
        "身份证",
        "银行卡",
        "cvv",
        "详细地址",
    )
    if any(token in lowered for token in forbidden_tokens):
        return True
    if re.search(r"\b1[3-9]\d{9}\b", value):
        return True
    if re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", value):
        return True
    return bool(re.search(r"\b\d{15,19}\b", value))
