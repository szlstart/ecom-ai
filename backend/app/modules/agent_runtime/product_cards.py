from __future__ import annotations

import re
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.modules.agent_runtime.conversation_state import (
    ConversationStateRuntime,
    latest_card_set,
)
from app.modules.messaging.models import Conversation, Message
from app.modules.messaging.service import MessagingService

_SHORT_AFFIRMATIVES = frozenset(
    {"好", "好的", "好呀", "可以", "行", "行啊", "继续", "嗯", "嗯嗯", "ok", "okay"}
)


def is_short_affirmative(user_text: str) -> bool:
    """Return true only for a bare acknowledgement that requires dialogue context."""

    return "".join(user_text.split()).casefold() in _SHORT_AFFIRMATIVES


def product_nos_from_result(data: Mapping[str, object]) -> list[str]:
    """Extract stable product numbers from trusted tool output in display order."""

    values: list[str] = []
    product_no = data.get("product_id")
    if isinstance(product_no, str):
        values.append(product_no)
    items = data.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            candidate = item.get("product_id")
            if isinstance(candidate, str):
                values.append(candidate)
    return list(dict.fromkeys(values))[:5]


async def build_product_cards(
    session: AsyncSession,
    conversation: Conversation,
    product_nos: list[str],
    *,
    sku_nos_by_product: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    """Build canonical public product cards while reapplying conversation scope."""

    service = MessagingService(session)
    cards: list[dict[str, object]] = []
    for product_no in list(dict.fromkeys(product_nos))[:5]:
        try:
            cards.append(
                await service.product_card_payload(
                    conversation,
                    product_no,
                    (sku_nos_by_product or {}).get(product_no),
                )
            )
        except ApplicationError:
            # A product can leave sale between the read tool and presentation
            # assembly. Omitting the stale card is safer than rendering it.
            continue
    return cards


async def recent_agent_product_cards(
    session: AsyncSession,
    conversation: Conversation,
    *,
    before_sequence: int,
    minimum_count: int = 1,
) -> list[dict[str, object]]:
    """Return the latest Agent product-card set for deterministic coreference."""

    state_cards = latest_card_set(
        await ConversationStateRuntime(session).load(
            conversation,
            before_sequence=before_sequence,
        ),
        "product",
        minimum_count=minimum_count,
    )
    if state_cards is not None:
        return [
            card
            for card in state_cards
            if isinstance(card.get("product_id"), str)
        ][:5]

    messages = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.sequence_no < before_sequence,
                    Message.sender_type == "agent",
                    Message.message_status == "sent",
                    Message.recalled_at.is_(None),
                )
                .order_by(Message.sequence_no.desc())
                # Users often ask several policy or order questions before
                # returning to a previously shown product. Select the newest
                # structured product-card turn from a bounded, wider window.
                .limit(30)
            )
        ).all()
    )
    for message in messages:
        payload = message.content_payload if isinstance(message.content_payload, dict) else {}
        values = payload.get("product_cards")
        trace = payload.get("execution_trace")
        trace_intent = trace.get("intent") if isinstance(trace, Mapping) else None
        if trace_intent in {
            "product_search",
            "personalized_recommendation",
            "product_compare",
        } and (not isinstance(values, list) or not values):
            # A completed catalogue turn with no cards is an explicit context
            # boundary.  Never skip across it and bind “这两件” to an older,
            # unrelated recommendation set.
            return []
        if not isinstance(values, list):
            continue
        cards = [
            dict(item)
            for item in values
            if isinstance(item, Mapping) and isinstance(item.get("product_id"), str)
        ]
        if cards:
            # The newest visual set owns product coreference even if it contains
            # fewer cards than the shopper's ordinal requires.  Callers can ask
            # for clarification; skipping to an older, larger set would silently
            # switch the products being discussed.
            return cards[:5]
    return []


async def recent_named_agent_product_cards(
    session: AsyncSession,
    conversation: Conversation,
    *,
    before_sequence: int,
    user_text: str,
    maximum_count: int = 3,
) -> list[dict[str, object]]:
    """Recover explicitly named products from recent structured card turns.

    A newer, unrelated one-card turn must not erase an explicit reference such
    as "比较刚才的 2B 铅笔和 15 厘米直尺".  This recovery is deliberately
    bounded and name-based; pronouns and ordinals still bind only to the newest
    visual card set through :func:`recent_agent_product_cards`.
    """

    messages = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.sequence_no < before_sequence,
                    Message.sender_type == "agent",
                    Message.message_status == "sent",
                    Message.recalled_at.is_(None),
                )
                .order_by(Message.sequence_no.desc())
                .limit(30)
            )
        ).all()
    )
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    for message in messages:
        payload = message.content_payload if isinstance(message.content_payload, dict) else {}
        values = payload.get("product_cards")
        if not isinstance(values, list):
            continue
        for raw_card in values:
            if not isinstance(raw_card, Mapping):
                continue
            product_no = raw_card.get("product_id")
            if not isinstance(product_no, str) or product_no in seen:
                continue
            seen.add(product_no)
            candidates.append(dict(raw_card))
    return _mentioned_product_cards(user_text, candidates)[: max(2, maximum_count)]


def product_card_reference_index(user_text: str) -> int | None:
    indices = product_card_reference_indices(user_text)
    compact = "".join(user_text.split()).casefold()
    if len(indices) >= 2 and any(
        marker in compact
        for marker in ("我说的是", "应该是", "改成", "不是这个", "不是刚才那个")
    ):
        # In a correction the target normally follows the rejected ordinal:
        # “不是第二个，我说的是第一个”.  Resolve the corrected target rather
        # than anchoring to the first ordinal token in the sentence.
        return indices[-1]
    return indices[0] if indices else None


def product_card_reference_indices(user_text: str) -> list[int]:
    """Return every explicitly referenced card index in textual order."""

    compact = "".join(user_text.split()).casefold()
    ordinals = (
        (("第一个", "第一件", "第1个", "第1件", "1号"), 0),
        (("第二个", "第二件", "第2个", "第2件", "2号"), 1),
        (("第三个", "第三件", "第3个", "第3件", "3号"), 2),
        (("第四个", "第四件", "第4个", "第4件", "4号"), 3),
        (("第五个", "第五件", "第5个", "第5件", "5号"), 4),
    )
    matches: list[tuple[int, int]] = []
    for markers, index in ordinals:
        positions = [compact.find(marker) for marker in markers if marker in compact]
        if positions:
            matches.append((min(positions), index))
    return [index for _position, index in sorted(matches)]


def referenced_product_card(
    user_text: str,
    cards: list[dict[str, object]],
    *,
    include_single_deictic: bool = True,
) -> dict[str, object] | None:
    """Resolve explicit ordinals and single-result pronouns without model guessing."""

    compact = "".join(user_text.split()).casefold()
    index = product_card_reference_index(user_text)
    if index is not None:
        return cards[index] if index < len(cards) else None
    mentioned = _mentioned_product_card(user_text, cards)
    if mentioned is not None:
        return mentioned
    if (
        include_single_deictic
        and len(cards) == 1
        and any(
            marker in compact
            for marker in (
                "这个",
                "这件",
                "这款",
                "它",
                "刚才那个",
                "刚才推荐",
                "好",
                "可以",
                "继续",
                "行",
            )
        )
    ):
        return cards[0]
    return None


def _mentioned_product_card(
    user_text: str, cards: list[dict[str, object]]
) -> dict[str, object] | None:
    """Resolve a uniquely named recent card such as `这把直尺`."""

    normalized = "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", user_text)).casefold()
    ignored = {
        "这个",
        "这件",
        "这款",
        "这把",
        "这条",
        "商品",
        "东西",
        "适合",
        "考试",
        "多少",
        "具体",
        "现在",
        "还有",
        "库存",
        "价格",
        "比较",
        "对比",
        "区别",
    }
    phrases = {
        normalized[start:end]
        for start in range(len(normalized))
        for end in range(start + 2, min(len(normalized), start + 6) + 1)
        if normalized[start:end] not in ignored
    }
    scored: list[tuple[int, dict[str, object]]] = []
    for card in cards:
        name = str(card.get("product_name") or "").casefold()
        score = max((len(phrase) for phrase in phrases if phrase in name), default=0)
        if score >= 2:
            scored.append((score, card))
    if not scored:
        return None
    best = max(score for score, _card in scored)
    winners = [card for score, card in scored if score == best]
    return winners[0] if len(winners) == 1 else None


def _mentioned_product_cards(
    user_text: str, cards: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Return recent cards explicitly named by the user, strongest matches first."""

    normalized = "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", user_text)).casefold()
    ignored = {
        "这个",
        "这件",
        "这款",
        "这把",
        "这条",
        "商品",
        "东西",
        "适合",
        "考试",
        "多少",
        "具体",
        "现在",
        "还有",
        "库存",
        "价格",
        "比较",
        "对比",
        "区别",
        "刚才",
    }
    phrases = {
        normalized[start:end]
        for start in range(len(normalized))
        for end in range(start + 2, min(len(normalized), start + 9) + 1)
        if normalized[start:end] not in ignored
    }
    scored: list[tuple[int, int, int, int, dict[str, object]]] = []
    for position, card in enumerate(cards):
        name = str(card.get("product_name") or "").casefold()
        matched_phrases = [phrase for phrase in phrases if phrase in name]
        evidence: list[tuple[int, int, int, int]] = []
        for phrase in sorted(matched_phrases, key=lambda value: -len(value)):
            user_start = normalized.find(phrase)
            name_start = name.find(phrase)
            user_end = user_start + len(phrase)
            name_end = name_start + len(phrase)
            if any(
                not (user_end <= item[0] or user_start >= item[1])
                or not (name_end <= item[2] or name_start >= item[3])
                for item in evidence
            ):
                continue
            evidence.append((user_start, user_end, name_start, name_end))
        score = sum(item[1] - item[0] for item in evidence)
        if score >= 2:
            mention_position = min((item[0] for item in evidence), default=len(normalized))
            mention_end = max((item[1] for item in evidence), default=mention_position)
            scored.append((mention_position, mention_end, -score, position, card))
    selected: list[tuple[int, int, int, int, dict[str, object]]] = []
    for candidate in sorted(scored, key=lambda value: (value[2], value[3])):
        start, end = candidate[:2]
        if any(start >= chosen[0] and end <= chosen[1] for chosen in selected):
            continue
        selected.append(candidate)
    selected.sort(key=lambda value: (value[0], value[2], value[3]))
    return [card for _start, _end, _negative_score, _position, card in selected]
