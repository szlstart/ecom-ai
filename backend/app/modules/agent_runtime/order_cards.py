from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.modules.identity.models import User
from app.modules.messaging.models import Conversation, Message
from app.modules.messaging.service import MessagingService


def requests_direct_transaction_action(user_text: str) -> bool:
    """Return true when a shopper asks the Agent to perform a protected action."""

    compact = "".join(user_text.split()).casefold()
    return any(
        marker in compact
        for marker in (
            "帮我付款",
            "替我付款",
            "直接付款",
            "帮我支付",
            "替我支付",
            "直接支付",
            "帮我确认收货",
            "帮我取消订单",
        )
    )


def order_nos_from_result(data: Mapping[str, object]) -> list[str]:
    """Extract unique public order numbers from a trusted tool result."""

    values: list[object] = [data.get("order_id")]
    items = data.get("items")
    if isinstance(items, list):
        values.extend(item.get("order_id") for item in items if isinstance(item, Mapping))
    return _unique_order_nos(values)


async def build_order_cards(
    session: AsyncSession,
    user: User,
    conversation: Conversation,
    order_nos: Iterable[object],
    *,
    limit: int = 5,
) -> list[dict[str, object]]:
    """Hydrate order cards through the canonical ACL-aware message builder."""

    service = MessagingService(session)
    cards: list[dict[str, object]] = []
    for order_no in _unique_order_nos(order_nos)[:limit]:
        try:
            cards.append(await service.order_card_payload(user, conversation, order_no))
        except ApplicationError:
            # An order may disappear between the read tool and card hydration.  Do
            # not leak its identifier or fail the entire Agent response.
            continue
    return cards


async def recent_agent_order_nos(
    session: AsyncSession,
    conversation: Conversation,
    *,
    before_sequence: int,
    minimum_count: int = 1,
) -> list[str]:
    """Return cards from the latest prior Agent turn that presented orders."""

    rows = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.sender_type == "agent",
                    Message.sequence_no < before_sequence,
                )
                .order_by(Message.sequence_no.desc())
                # Keep enough structured turns for a realistic support session.
                # We still select the newest order-card message, never a textual
                # model guess, so a longer window does not broaden data access.
                .limit(30)
            )
        ).all()
    )
    fallback: list[str] = []
    for message in rows:
        payload = message.content_payload if isinstance(message.content_payload, dict) else {}
        cards = payload.get("order_cards")
        if not isinstance(cards, list):
            continue
        order_nos = _unique_order_nos(
            card.get("order_id") for card in cards if isinstance(card, Mapping)
        )
        if order_nos and not fallback:
            fallback = order_nos
        if len(order_nos) >= max(1, minimum_count):
            return order_nos
    return fallback


def _unique_order_nos(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.startswith("ord_") or value in result:
            continue
        result.append(value)
    return result


def referenced_order_no(user_text: str, order_nos: list[str]) -> str | None:
    compact = re.sub(r"\s+", "", user_text).casefold()
    ordinal_groups = (
        (("第一笔", "第一个", "第1笔", "1号"), 0),
        (("第二笔", "第二个", "第2笔", "2号"), 1),
        (("第三笔", "第三个", "第3笔", "3号"), 2),
        (("第四笔", "第四个", "第4笔", "4号"), 3),
        (("第五笔", "第五个", "第5笔", "5号"), 4),
    )
    for markers, index in ordinal_groups:
        if any(marker in compact for marker in markers):
            return order_nos[index] if index < len(order_nos) else None
    if len(order_nos) == 1 and any(
        marker in compact for marker in ("这个订单", "这笔订单", "它", "刚才那个", "刚才的")
    ):
        return order_nos[0]
    return None


def order_reference_index(user_text: str) -> int | None:
    compact = re.sub(r"\s+", "", user_text).casefold()
    ordinal_groups = (
        (("第一笔", "第一个", "第1笔", "1号"), 0),
        (("第二笔", "第二个", "第2笔", "2号"), 1),
        (("第三笔", "第三个", "第3笔", "3号"), 2),
        (("第四笔", "第四个", "第4笔", "4号"), 3),
        (("第五笔", "第五个", "第5笔", "5号"), 4),
    )
    for markers, index in ordinal_groups:
        if any(marker in compact for marker in markers):
            return index
    return None
