from __future__ import annotations

import hashlib
import json
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

SUMMARY_PROMPT_VERSION = "deterministic-summary-v1"
SUMMARY_MODEL_NAME = "deterministic-safe-summarizer-v1"
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
    warning = "[不可信对话连续性摘要; 涉及订单、金额、库存、权限和状态时必须调用业务工具重新核验]"
    previous_lines = [line for line in previous.splitlines() if line and line != warning]
    important = [
        line
        for line in previous_lines
        if any(token in line for token in ("确认", "决定", "偏好", "订单", "退款", "物流"))
    ]
    candidates = [*important[-8:], *previous_lines[-12:], *new_lines]
    deduped: list[str] = []
    for line in candidates:
        if line not in deduped:
            deduped.append(line)
    body: list[str] = []
    used = len(warning) + 1
    for line in reversed(deduped):
        if used + len(line) + 1 > MAX_SUMMARY_CHARACTERS:
            continue
        body.append(line)
        used += len(line) + 1
    body.reverse()
    return "\n".join((warning, *body))
