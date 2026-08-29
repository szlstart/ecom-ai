from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
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


@dataclass(frozen=True)
class ProposedMemory:
    memory_no: str
    memory_type: str
    memory_key: str
    value: str
    expires_at: datetime
    version: int


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

    async def propose_exclusive(
        self,
        user: User,
        *,
        source_message_no: str,
        value: str,
    ) -> ProposedMemory | None:
        """Persist an encrypted candidate; it is never recalled before explicit activation."""
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 500
            or _looks_sensitive(normalized)
            or not _looks_like_preference(normalized)
        ):
            return None
        now = utc_now()
        consent = await self.mysql.scalar(
            select(UserAgentConsent).where(
                UserAgentConsent.user_id == user.id,
                UserAgentConsent.consent_type == "personalization",
                UserAgentConsent.consent_status == "active",
                or_(
                    UserAgentConsent.expires_at.is_(None),
                    UserAgentConsent.expires_at > now,
                ),
            )
        )
        if consent is None:
            return None
        memory_type, memory_key = _classify_explicit_preference(normalized)
        dedupe = self.security.keyed_hash(
            "ai-memory-dedupe",
            f"{user.user_no}:exclusive:{memory_key}:{normalized.casefold()}",
        )
        existing = (
            (
                await self.postgres.execute(
                    text(
                        """SELECT memory_no,memory_type,memory_key,content_ciphertext,
                        expires_at,version FROM memory.items
                        WHERE user_no=:user_no AND namespace='exclusive' AND store_no IS NULL
                          AND memory_status='candidate' AND dedupe_fingerprint=:dedupe
                          AND expires_at > now()
                        ORDER BY updated_at DESC LIMIT 1"""
                    ),
                    {"user_no": user.user_no, "dedupe": dedupe},
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            ciphertext = existing["content_ciphertext"]
            if isinstance(ciphertext, bytes):
                return ProposedMemory(
                    memory_no=str(existing["memory_no"]),
                    memory_type=str(existing["memory_type"]),
                    memory_key=str(existing["memory_key"]),
                    value=self.security.decrypt("ai-memory-content", ciphertext),
                    expires_at=existing["expires_at"],
                    version=int(existing["version"]),
                )
        memory_no = _new_memory_no()
        expires_at = now + timedelta(days=365)
        ciphertext = self.security.encrypt("ai-memory-content", normalized)
        content_hash = self.security.keyed_hash("ai-memory-content-hash", normalized)
        await self.postgres.execute(
            text(
                """INSERT INTO memory.items
                (memory_no,user_no,namespace,store_no,memory_type,confidence,
                 memory_status,consent_no,expires_at,memory_key,content_ciphertext,content_hash,
                 dedupe_fingerprint,key_version,source_type,source_ref,source_conversation_no,
                 source_message_no,consent_policy_version,validation_snapshot,salience,
                 data_classification,memory_risk_level,valid_from,valid_until,version)
                VALUES (:memory_no,:user_no,'exclusive',NULL,:memory_type,0.950,
                 'candidate',:consent_no,:expires_at,:memory_key,:ciphertext,:content_hash,
                 :dedupe,1,'explicit_user_message',:source_ref,NULL,:source_message_no,
                 :policy_version,CAST(:validation AS JSONB),0.800,'L2','low',:valid_from,
                 :valid_until,0)"""
            ),
            {
                "memory_no": memory_no,
                "user_no": user.user_no,
                "memory_type": memory_type,
                "consent_no": consent.consent_no,
                "expires_at": expires_at,
                "memory_key": memory_key,
                "ciphertext": ciphertext,
                "content_hash": content_hash,
                "dedupe": dedupe,
                "source_ref": source_message_no,
                "source_message_no": source_message_no,
                "policy_version": consent.policy_version,
                "validation": '{"explicit_user_statement":true,"requires_confirmation":true}',
                "valid_from": now,
                "valid_until": expires_at,
            },
        )
        await self.postgres.execute(
            text(
                """INSERT INTO memory.events
                (event_no,memory_id,event_type,actor_type,reason_code,user_no,from_status,
                 to_status,actor_no,consent_no,source_message_no,content_hash_after,
                 metadata_redacted)
                VALUES (:event_no,(SELECT id FROM memory.items WHERE memory_no=:memory_no),
                 'candidate_created','agent','EXPLICIT_USER_REQUEST',:user_no,NULL,'candidate',
                 :user_no,:consent_no,:source_message_no,:content_hash,
                 CAST(:metadata AS JSONB))"""
            ),
            {
                "event_no": _new_event_no(),
                "memory_no": memory_no,
                "user_no": user.user_no,
                "consent_no": consent.consent_no,
                "source_message_no": source_message_no,
                "content_hash": content_hash,
                "metadata": '{"requires_user_confirmation":true}',
            },
        )
        await self.postgres.commit()
        return ProposedMemory(
            memory_no=memory_no,
            memory_type=memory_type,
            memory_key=memory_key,
            value=normalized,
            expires_at=expires_at,
            version=0,
        )

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


def explicit_memory_request(value: str) -> str | None:
    match = re.fullmatch(
        r"\s*(?:请|麻烦你|帮我)?记住[\uFF1A:,，\s]+(.{1,500}?)\s*[。\uFF01!]?\s*",
        value,
    )
    if match is None:
        return None
    candidate = match.group(1).strip()
    return candidate if candidate and not _looks_sensitive(candidate) else None


def _classify_explicit_preference(value: str) -> tuple[str, str]:
    lowered = value.casefold()
    if any(token in lowered for token in ("预算", "不超过", "以内", "价格")):
        return "constraint", "shopping.budget"
    if any(token in lowered for token in ("颜色", "红色", "蓝色", "绿色", "黑色", "白色")):
        return "preference", "shopping.color"
    if any(token in lowered for token in ("材质", "棉", "羊毛", "真皮", "塑料", "金属")):
        return "preference", "shopping.material"
    if any(token in lowered for token in ("品牌", "牌子")):
        return "preference", "shopping.brand"
    return "preference", "shopping.general"


def _looks_like_preference(value: str) -> bool:
    lowered = value.casefold()
    return any(
        token in lowered
        for token in (
            "喜欢",
            "偏好",
            "不喜欢",
            "不要",
            "预算",
            "习惯",
            "常买",
            "优先",
            "尺码",
            "颜色",
            "材质",
            "品牌",
        )
    )


def _new_memory_no() -> str:
    from app.core.id_generator import new_prefixed_ulid

    return new_prefixed_ulid("mem_")


def _new_event_no() -> str:
    from app.core.id_generator import new_prefixed_ulid

    return new_prefixed_ulid("mev_")
