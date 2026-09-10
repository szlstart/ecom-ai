from __future__ import annotations

from collections.abc import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.modules.identity.models import User
from app.modules.messaging.models import Conversation, Message
from app.modules.messaging.service import MessagingService


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
                .limit(12)
            )
        ).all()
    )
    for message in rows:
        payload = message.content_payload if isinstance(message.content_payload, dict) else {}
        cards = payload.get("order_cards")
        if not isinstance(cards, list):
            continue
        order_nos = _unique_order_nos(
            card.get("order_id") for card in cards if isinstance(card, Mapping)
        )
        if order_nos:
            return order_nos
    return []


def _unique_order_nos(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.startswith("ord_") or value in result:
            continue
        result.append(value)
    return result
