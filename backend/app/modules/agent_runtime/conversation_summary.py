from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.modules.agent_runtime.context_window import (
    MAX_RECENT_MESSAGES,
    ContextWindow,
    _role,
    _safe_dialogue_text,
)
from app.modules.messaging.models import Conversation, Message

SUMMARY_PROMPT_VERSION = "deterministic-dossier-v2"
SUMMARY_MODEL_NAME = "deterministic-safe-dossier-v2"
SUMMARY_EXPIRY_DAYS = 90
MIN_NEW_MESSAGES = 8
MAX_NEW_MESSAGES = 50
MAX_SUMMARY_CHARACTERS = 3_000


@dataclass(frozen=True)
class RollingConversationSummary:
    summary_no: str
    text: str
    message_count: int
    start_message_no: str
    end_message_no: str


class ConversationSummaryRuntime:
    """Build an encrypted, scope-bound continuity summary outside the recent window."""

    def __init__(
        self,
        mysql: AsyncSession,
        postgres: AsyncSession,
        security: SecurityService,
    ) -> None:
        self.mysql = mysql
        self.postgres = postgres
        self.security = security

    async def load_or_update(
        self,
        conversation: Conversation,
        trigger: Message,
        *,
        user_no: str,
        store_no: str | None,
    ) -> RollingConversationSummary | None:
        if trigger.sequence_no <= MAX_RECENT_MESSAGES + 1:
            return None
        await self.postgres.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"conversation-summary:{conversation.conversation_no}"},
        )
        previous = (
            (
                await self.postgres.execute(
                    text(
                        """SELECT id,summary_no,start_message_no,end_message_no,message_count,
                        summary_ciphertext FROM memory.summaries
                        WHERE conversation_no=:conversation_no AND user_no=:user_no
                          AND store_no IS NOT DISTINCT FROM :store_no
                          AND summary_status='active' AND expires_at > now()
                        ORDER BY created_at DESC LIMIT 1"""
                    ),
                    {
                        "conversation_no": conversation.conversation_no,
                        "user_no": user_no,
                        "store_no": store_no,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        previous_end_sequence = 0
        previous_text = ""
        if previous is not None:
            previous_end_sequence = int(
                await self.mysql.scalar(
                    select(Message.sequence_no).where(
                        Message.conversation_id == conversation.id,
                        Message.message_no == previous["end_message_no"],
                    )
                )
                or 0
            )
            try:
                previous_text = self.security.decrypt(
                    "ai-conversation-summary", previous["summary_ciphertext"]
                )
            except (TypeError, ValueError):
                previous = None
                previous_end_sequence = 0
                previous_text = ""

        cutoff = trigger.sequence_no - MAX_RECENT_MESSAGES
        messages = list(
            (
                await self.mysql.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation.id,
                        Message.sequence_no > previous_end_sequence,
                        Message.sequence_no <= cutoff,
                        Message.recalled_at.is_(None),
                        Message.message_status == "sent",
                        Message.sender_type.in_(("user", "human", "agent")),
                        Message.text_content.is_not(None),
                    )
                    .order_by(Message.sequence_no)
                    .limit(MAX_NEW_MESSAGES)
                )
            ).all()
        )
        turns = [
            (message, safe_text)
            for message in messages
            if (safe_text := _safe_dialogue_text(message.text_content or ""))
        ]
        if len(turns) < MIN_NEW_MESSAGES:
            await self.postgres.commit()
            return (
                RollingConversationSummary(
                    summary_no=str(previous["summary_no"]),
                    text=previous_text,
                    message_count=int(previous["message_count"]),
                    start_message_no=str(previous["start_message_no"]),
                    end_message_no=str(previous["end_message_no"]),
                )
                if previous is not None
                else None
            )

        new_lines = [f"{_role(message.sender_type)}: {safe_text}" for message, safe_text in turns]
        summary_text = _compact_summary(previous_text, new_lines)
        first_message_no = (
            str(previous["start_message_no"]) if previous is not None else turns[0][0].message_no
        )
        end_message_no = turns[-1][0].message_no
        message_count = (int(previous["message_count"]) if previous is not None else 0) + len(turns)
        source_material = "\n".join(
            f"{message.message_no}:{safe_text}" for message, safe_text in turns
        )
        source_hash = hashlib.sha256(source_material.encode()).digest()
        summary_hash = hashlib.sha256(summary_text.encode()).digest()
        summary_no = new_prefixed_ulid("sum_")
        expires_at = utc_now() + timedelta(days=SUMMARY_EXPIRY_DAYS)
        flags = json.dumps(
            {
                "trust_level": "untrusted_dialogue",
                "business_fact_authoritative": False,
                "prompt_injection_isolated": any("疑似越权指令" in line for line in new_lines),
                "contains_plaintext_secrets": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        inserted = (
            await self.postgres.execute(
                text(
                    """INSERT INTO memory.summaries
                    (summary_no,conversation_no,user_no,store_no,start_message_no,end_message_no,
                     message_count,source_token_count,summary_token_count,summary_ciphertext,
                     summary_hash,source_hash,key_version,model_name,prompt_version,summary_status,
                     supersedes_summary_id,quality_flags,expires_at)
                    VALUES (:summary_no,:conversation_no,:user_no,:store_no,:start_message_no,
                     :end_message_no,:message_count,:source_tokens,:summary_tokens,:ciphertext,
                     :summary_hash,:source_hash,1,:model_name,:prompt_version,'active',
                     :supersedes_id,CAST(:flags AS JSONB),:expires_at)
                    ON CONFLICT (conversation_no,end_message_no,prompt_version) DO NOTHING
                    RETURNING id"""
                ),
                {
                    "summary_no": summary_no,
                    "conversation_no": conversation.conversation_no,
                    "user_no": user_no,
                    "store_no": store_no,
                    "start_message_no": first_message_no,
                    "end_message_no": end_message_no,
                    "message_count": message_count,
                    "source_tokens": max(1, len(source_material) // 4),
                    "summary_tokens": max(1, len(summary_text) // 4),
                    "ciphertext": self.security.encrypt("ai-conversation-summary", summary_text),
                    "summary_hash": summary_hash,
                    "source_hash": source_hash,
                    "model_name": SUMMARY_MODEL_NAME,
                    "prompt_version": SUMMARY_PROMPT_VERSION,
                    "supersedes_id": previous["id"] if previous is not None else None,
                    "flags": flags,
                    "expires_at": expires_at,
                },
            )
        ).scalar_one_or_none()
        if inserted is None:
            await self.postgres.rollback()
            return await self._load_exact(
                conversation.conversation_no, user_no=user_no, store_no=store_no
            )
        if previous is not None:
            await self.postgres.execute(
                text(
                    "UPDATE memory.summaries SET summary_status='superseded' "
                    "WHERE id=:previous_id AND summary_status='active'"
                ),
                {"previous_id": previous["id"]},
            )
        await self.postgres.commit()
        return RollingConversationSummary(
            summary_no=summary_no,
            text=summary_text,
            message_count=message_count,
            start_message_no=first_message_no,
            end_message_no=end_message_no,
        )

    async def _load_exact(
        self, conversation_no: str, *, user_no: str, store_no: str | None
    ) -> RollingConversationSummary | None:
        row = (
            (
                await self.postgres.execute(
                    text(
                        """SELECT summary_no,start_message_no,end_message_no,message_count,
                        summary_ciphertext FROM memory.summaries
                        WHERE conversation_no=:conversation_no AND user_no=:user_no
                          AND store_no IS NOT DISTINCT FROM :store_no
                          AND summary_status='active' AND expires_at > now()
                        ORDER BY created_at DESC LIMIT 1"""
                    ),
                    {
                        "conversation_no": conversation_no,
                        "user_no": user_no,
                        "store_no": store_no,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            summary_text = self.security.decrypt(
                "ai-conversation-summary", row["summary_ciphertext"]
            )
        except (TypeError, ValueError):
            return None
        return RollingConversationSummary(
            summary_no=str(row["summary_no"]),
            text=summary_text,
            message_count=int(row["message_count"]),
            start_message_no=str(row["start_message_no"]),
            end_message_no=str(row["end_message_no"]),
        )


async def attach_rolling_summary(
    window: ContextWindow,
    *,
    mysql: AsyncSession,
    postgres: AsyncSession,
    security: SecurityService,
    conversation: Conversation,
    trigger: Message,
    user_no: str,
    store_no: str | None,
) -> ContextWindow:
    """Best-effort enrichment; PostgreSQL availability must not block customer support."""
    try:
        summary = await ConversationSummaryRuntime(mysql, postgres, security).load_or_update(
            conversation,
            trigger,
            user_no=user_no,
            store_no=store_no,
        )
    except Exception:
        await postgres.rollback()
        return window
    if summary is None:
        return window
    return window.with_summary(
        summary.text,
        summary_no=summary.summary_no,
        message_count=summary.message_count,
    )


def _compact_summary(previous: str, new_lines: list[str]) -> str:
    """Rewrite previous and new turns into a bounded e-commerce continuity dossier."""

    dossier = _load_previous_dossier(previous)
    for line in new_lines:
        role, _, text_value = line.partition(":")
        value = text_value.strip()
        if not value:
            continue
        _append_unique(dossier["continuity_notes"], line, 12)
        if role == "用户":
            if not _is_small_reply(value):
                dossier["current_goal"] = value
            if _contains_any(
                value,
                "预算",
                "偏好",
                "喜欢",
                "不喜欢",
                "不要",
                "需要",
                "想要",
                "必须",
                "尺码",
                "颜色",
                "用途",
                "场景",
            ):
                _append_unique(dossier["user_constraints"], value, 8)
        else:
            if _contains_any(value, "已经", "已为", "已读取", "查到", "确认了", "创建了", "提交了"):
                _append_unique(dossier["completed_actions"], value, 8)
            if _contains_any(value, "我会", "我来", "可以继续", "接下来", "帮你", "为你查询"):
                _append_unique(dossier["commitments"], value, 8)
            if _looks_like_open_question(value):
                _append_unique(dossier["unresolved_questions"], value, 8)
        for resource in re.findall(
            r"\b(?:prd|sku|ord|ref|shp|sto)_[A-Za-z0-9]{6,40}\b", value
        ):
            _append_unique(dossier["resource_mentions"], resource, 8)
    return _bounded_dossier_json(dossier)


def _load_previous_dossier(previous: str) -> dict[str, object]:
    empty: dict[str, object] = {
        "schema_version": "conversation_dossier_v1",
        "trust_level": "untrusted_dialogue_continuity",
        "business_fact_authoritative": False,
        "current_goal": None,
        "resource_mentions": [],
        "user_constraints": [],
        "completed_actions": [],
        "commitments": [],
        "unresolved_questions": [],
        "continuity_notes": [],
    }
    try:
        parsed = json.loads(previous) if previous else {}
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    if isinstance(parsed, dict) and parsed.get("schema_version") == "conversation_dossier_v1":
        for key in empty:
            value = parsed.get(key)
            if key == "current_goal":
                empty[key] = value if isinstance(value, str) else None
            elif isinstance(empty[key], list) and isinstance(value, list):
                empty[key] = [str(item) for item in value if isinstance(item, str)][-12:]
        return empty
    legacy_lines = [line for line in previous.splitlines() if line and not line.startswith("[")]
    empty["continuity_notes"] = legacy_lines[-12:]
    return empty


def _append_unique(value: object, item: str, limit: int) -> None:
    if not isinstance(value, list):
        return
    safe = item.strip()[:500]
    if safe and safe not in value:
        value.append(safe)
        del value[:-limit]


def _bounded_dossier_json(dossier: dict[str, object]) -> str:
    def dump() -> str:
        return json.dumps(dossier, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    serialized = dump()
    if len(serialized) <= MAX_SUMMARY_CHARACTERS:
        return serialized
    for key in (
        "continuity_notes",
        "completed_actions",
        "commitments",
        "unresolved_questions",
        "user_constraints",
        "resource_mentions",
    ):
        values = dossier.get(key)
        while (
            isinstance(values, list)
            and len(values) > 2
            and len(serialized) > MAX_SUMMARY_CHARACTERS
        ):
            values.pop(0)
            serialized = dump()
    current = dossier.get("current_goal")
    if len(serialized) > MAX_SUMMARY_CHARACTERS and isinstance(current, str):
        dossier["current_goal"] = current[:240]
        serialized = dump()
    if len(serialized) > MAX_SUMMARY_CHARACTERS:
        raise ValueError("conversation dossier exceeds the configured size limit")
    return serialized


def _contains_any(value: str, *tokens: str) -> bool:
    return any(token in value for token in tokens)


def _is_small_reply(value: str) -> bool:
    return re.sub(r"\s+", "", value).casefold() in {
        "好",
        "好的",
        "可以",
        "行",
        "继续",
        "嗯",
        "嗯嗯",
        "ok",
        "okay",
    }


def _looks_like_open_question(value: str) -> bool:
    return value.rstrip().endswith(("?", "？")) or _contains_any(  # noqa: RUF001
        value,
        "你想先",
        "你更关心",
        "你更想",
        "需要我继续",
        "是否需要",
        "告诉我具体",
    )
