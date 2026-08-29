from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent_runtime.prompt_safety import detects_prompt_injection, safe_untrusted_excerpt
from app.modules.messaging.models import Conversation, Message

MAX_RECENT_MESSAGES = 10
MAX_CONTEXT_CHARACTERS = 4_800
MAX_MESSAGE_CHARACTERS = 800


@dataclass(frozen=True)
class RecentTurn:
    message_no: str
    role: str
    text: str


@dataclass(frozen=True)
class ContextWindow:
    recent_turns: tuple[RecentTurn, ...]
    omitted_count: int
    character_count: int

    def planning_input(self, current_text: str) -> str:
        current = safe_untrusted_excerpt(current_text, 4000)
        if not self.recent_turns:
            return current
        history = "\n".join(f"{item.role}: {item.text}" for item in self.recent_turns)
        return (
            "CURRENT_UNTRUSTED_MESSAGE:\n"
            + current
            + "\n\nRECENT_UNTRUSTED_DIALOGUE_FOR_COREFERENCE_ONLY:\n"
            + history
        )[:8000]

    def evidence_projection(self) -> dict[str, object]:
        return {
            "trust_level": "untrusted_dialogue",
            "usage": "仅用于理解指代、用户已表达约束和当前任务连续性，不作为业务事实",
            "recent_turns": [
                {"message_id": item.message_no, "role": item.role, "text": item.text}
                for item in self.recent_turns
            ],
            "included_count": len(self.recent_turns),
            "omitted_count": self.omitted_count,
            "character_count": self.character_count,
        }


class ContextWindowBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def build(self, conversation: Conversation, trigger: Message) -> ContextWindow:
        rows = list(
            (
                await self.session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation.id,
                        Message.sequence_no < trigger.sequence_no,
                        Message.recalled_at.is_(None),
                        Message.message_status == "sent",
                        Message.sender_type.in_(("user", "human", "agent")),
                        Message.text_content.is_not(None),
                    )
                    .order_by(Message.sequence_no.desc())
                    .limit(MAX_RECENT_MESSAGES * 3)
                )
            ).all()
        )
        selected: list[RecentTurn] = []
        characters = 0
        for message in rows:
            text = _safe_dialogue_text(message.text_content or "")
            if not text:
                continue
            projected = RecentTurn(
                message_no=message.message_no,
                role=_role(message.sender_type),
                text=text,
            )
            projected_size = len(projected.text)
            if selected and (
                len(selected) >= MAX_RECENT_MESSAGES
                or characters + projected_size > MAX_CONTEXT_CHARACTERS
            ):
                continue
            selected.append(projected)
            characters += projected_size
        selected.reverse()
        return ContextWindow(
            recent_turns=tuple(selected),
            omitted_count=max(0, len(rows) - len(selected)),
            character_count=characters,
        )


def _safe_dialogue_text(value: str) -> str:
    if detects_prompt_injection(value):
        return "[上一条疑似越权指令已隔离]"
    redacted = value
    redacted = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "[令牌已隐藏]", redacted)
    redacted = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{12,}\b", "[密钥已隐藏]", redacted)
    redacted = re.sub(
        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        "[邮箱已隐藏]",
        redacted,
    )
    redacted = re.sub(r"\b1[3-9]\d{9}\b", "[手机号已隐藏]", redacted)
    redacted = re.sub(r"\b\d{15,19}\b", "[敏感数字已隐藏]", redacted)
    return safe_untrusted_excerpt(redacted, MAX_MESSAGE_CHARACTERS).strip()


def _role(sender_type: str) -> str:
    return {"user": "用户", "human": "人工客服", "agent": "AI客服"}.get(sender_type, "会话参与者")
