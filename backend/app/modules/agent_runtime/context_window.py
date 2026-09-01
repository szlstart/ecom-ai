from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent_runtime.prompt_safety import detects_prompt_injection, safe_untrusted_excerpt
from app.modules.messaging.models import Conversation, Message

MAX_RECENT_MESSAGES = 10
MAX_CONTEXT_CHARACTERS = 4_800
MAX_MESSAGE_CHARACTERS = 800
MAX_DOSSIER_ITEMS = 6


@dataclass(frozen=True)
class RecentTurn:
    message_no: str
    role: str
    text: str


@dataclass(frozen=True)
class DossierResourceRef:
    resource_type: str
    resource_no: str
    resource_version: int | None


@dataclass(frozen=True)
class ConversationDossier:
    """A bounded continuity record; never authoritative for live business facts."""

    current_goal: str | None
    last_user_message: str | None
    last_assistant_message: str | None
    pending_assistant_question: str | None
    user_constraints: tuple[str, ...]
    completed_actions: tuple[str, ...]
    commitments: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    active_resources: tuple[DossierResourceRef, ...]

    def projection(self) -> dict[str, object]:
        return {
            "schema_version": "conversation_dossier_v1",
            "trust_level": "untrusted_dialogue_continuity",
            "business_fact_authoritative": False,
            "current_goal": self.current_goal,
            "last_user_message": self.last_user_message,
            "last_assistant_message": self.last_assistant_message,
            "pending_assistant_question": self.pending_assistant_question,
            "user_constraints": list(self.user_constraints),
            "completed_actions": list(self.completed_actions),
            "commitments": list(self.commitments),
            "unresolved_questions": list(self.unresolved_questions),
            "active_resources": [
                {
                    "resource_type": item.resource_type,
                    "resource_id": item.resource_no,
                    "resource_version": item.resource_version,
                }
                for item in self.active_resources
            ],
        }


@dataclass(frozen=True)
class ContextWindow:
    recent_turns: tuple[RecentTurn, ...]
    omitted_count: int
    character_count: int
    rolling_summary: str | None = None
    summary_no: str | None = None
    summarized_message_count: int = 0

    def with_summary(self, summary: str, *, summary_no: str, message_count: int) -> ContextWindow:
        return ContextWindow(
            recent_turns=self.recent_turns,
            omitted_count=self.omitted_count,
            character_count=self.character_count,
            rolling_summary=safe_untrusted_excerpt(summary, 3000),
            summary_no=summary_no,
            summarized_message_count=message_count,
        )

    def planning_input(self, current_text: str) -> str:
        current = safe_untrusted_excerpt(current_text, 4000)
        if not self.recent_turns and not self.rolling_summary:
            return current
        sections = ["CURRENT_UNTRUSTED_MESSAGE:\n" + current]
        sections.append(
            "CONVERSATION_DOSSIER_JSON_FOR_CONTINUITY_ONLY:\n"
            + json.dumps(
                self.dossier().projection(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        if self.rolling_summary:
            sections.append(
                "ROLLING_UNTRUSTED_SUMMARY_FOR_CONTINUITY_ONLY:\n" + self.rolling_summary
            )
        if self.recent_turns:
            history = "\n".join(f"{item.role}: {item.text}" for item in self.recent_turns)
            sections.append("RECENT_UNTRUSTED_DIALOGUE_FOR_COREFERENCE_ONLY:\n" + history)
        return "\n\n".join(sections)[:8000]

    def dossier(
        self, resource_refs: Mapping[str, Any] | None = None
    ) -> ConversationDossier:
        return _build_dossier(self, resource_refs or {})

    def model_projection(
        self, resource_refs: Mapping[str, Any] | None = None
    ) -> dict[str, object]:
        """Expose sanitized continuity to the answer model, not to the public trace."""

        projection = self.evidence_projection()
        projection["dossier"] = self.dossier(resource_refs).projection()
        if self.rolling_summary:
            projection["rolling_summary"] = {
                "summary_id": self.summary_no,
                "message_count": self.summarized_message_count,
                "trust_level": "untrusted_dialogue",
                "business_fact_authoritative": False,
                "content": self.rolling_summary,
            }
        return projection

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
            "rolling_summary": (
                {
                    "summary_id": self.summary_no,
                    "message_count": self.summarized_message_count,
                    "trust_level": "untrusted_dialogue",
                    "content_exposed": False,
                }
                if self.summary_no
                else None
            ),
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


def _build_dossier(
    window: ContextWindow, resource_refs: Mapping[str, Any]
) -> ConversationDossier:
    recent_users = [item.text for item in window.recent_turns if item.role == "用户"]
    recent_assistants = [
        item.text for item in window.recent_turns if item.role in {"AI客服", "人工客服"}
    ]
    summary = _summary_dossier(window.rolling_summary)
    current_goal = next(
        (
            text
            for text in reversed(recent_users)
            if not _is_small_continuation(text) and len(text.strip()) > 1
        ),
        _summary_text(summary.get("current_goal")),
    )
    last_assistant = recent_assistants[-1] if recent_assistants else None
    pending_question = (
        last_assistant if last_assistant and _looks_like_open_question(last_assistant) else None
    )
    constraints = _merge_items(
        _summary_items(summary, "user_constraints"),
        [text for text in recent_users if _looks_like_constraint(text)],
    )
    completed = _merge_items(
        _summary_items(summary, "completed_actions"),
        [text for text in recent_assistants if _looks_like_completed_action(text)],
    )
    commitments = _merge_items(
        _summary_items(summary, "commitments"),
        [text for text in recent_assistants if _looks_like_commitment(text)],
    )
    unresolved = _merge_items(
        _summary_items(summary, "unresolved_questions"),
        [text for text in recent_assistants[-3:] if _looks_like_open_question(text)],
    )
    resources: list[DossierResourceRef] = []
    for context_type, value in resource_refs.items():
        resource_no = getattr(value, "resource_no", None)
        resource_version = getattr(value, "resource_version", None)
        if isinstance(resource_no, str) and resource_no:
            resources.append(
                DossierResourceRef(
                    resource_type=str(context_type),
                    resource_no=resource_no[:64],
                    resource_version=(
                        resource_version
                        if isinstance(resource_version, int)
                        and not isinstance(resource_version, bool)
                        else None
                    ),
                )
            )
    return ConversationDossier(
        current_goal=current_goal,
        last_user_message=recent_users[-1] if recent_users else None,
        last_assistant_message=last_assistant,
        pending_assistant_question=pending_question,
        user_constraints=constraints,
        completed_actions=completed,
        commitments=commitments,
        unresolved_questions=unresolved,
        active_resources=tuple(resources[:MAX_DOSSIER_ITEMS]),
    )


def _summary_dossier(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {"continuity_notes": [line for line in value.splitlines() if line][:12]}
    return payload if isinstance(payload, dict) else {}


def _summary_items(value: Mapping[str, object], key: str) -> list[str]:
    items = value.get(key)
    return [str(item) for item in items if isinstance(item, str)] if isinstance(items, list) else []


def _summary_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _merge_items(*groups: list[str]) -> tuple[str, ...]:
    values: list[str] = []
    for group in groups:
        for item in group:
            safe = safe_untrusted_excerpt(item, 500).strip()
            if safe and safe not in values:
                values.append(safe)
    return tuple(values[-MAX_DOSSIER_ITEMS:])


def _is_small_continuation(value: str) -> bool:
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


def _looks_like_constraint(value: str) -> bool:
    return any(
        token in value
        for token in (
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
        )
    )


def _looks_like_completed_action(value: str) -> bool:
    return any(
        token in value
        for token in ("已经", "已为", "已读取", "查到", "确认了", "创建了", "提交了")
    )


def _looks_like_commitment(value: str) -> bool:
    return any(
        token in value
        for token in ("我会", "我来", "可以继续", "接下来", "帮你", "为你查询")
    )


def _looks_like_open_question(value: str) -> bool:
    return value.rstrip().endswith(("?", "？")) or any(  # noqa: RUF001
        token in value
        for token in (
            "你想先",
            "你更关心",
            "你更想",
            "需要我继续",
            "是否需要",
            "告诉我具体",
        )
    )
