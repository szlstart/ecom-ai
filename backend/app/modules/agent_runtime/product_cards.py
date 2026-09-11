from __future__ import annotations

import re
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
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
        if not isinstance(values, list):
            continue
        cards = [
            dict(item)
            for item in values
            if isinstance(item, Mapping) and isinstance(item.get("product_id"), str)
        ]
        if len(cards) >= max(1, minimum_count):
            return cards[:5]
    return []


def product_card_reference_index(user_text: str) -> int | None:
    indices = product_card_reference_indices(user_text)
    return indices[0] if indices else None


def product_card_reference_indices(user_text: str) -> list[int]:
    """Return every explicitly referenced card index in textual order."""

    compact = "".join(user_text.split()).casefold()
    ordinals = (
        (("第一个", "第一件", "第1个", "1号"), 0),
        (("第二个", "第二件", "第2个", "2号"), 1),
        (("第三个", "第三件", "第3个", "3号"), 2),
        (("第四个", "第四件", "第4个", "4号"), 3),
        (("第五个", "第五件", "第5个", "5号"), 4),
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
