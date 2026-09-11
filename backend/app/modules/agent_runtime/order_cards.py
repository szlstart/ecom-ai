from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation

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


async def referenced_recent_order_no(
    session: AsyncSession,
    conversation: Conversation,
    *,
    before_sequence: int,
    user_text: str,
) -> str | None:
    """Resolve a natural order reference from recently rendered trusted cards.

    Users normally remember a purchase by product, variant, store, or amount—not
    by its public ID. Only server-produced order-card payloads are considered, so
    matching a phrase never broadens the authenticated user's data scope.
    """

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
                .limit(30)
            )
        ).all()
    )
    cards: list[Mapping[str, object]] = []
    seen: set[str] = set()
    latest_single_order_no: str | None = None
    for message in rows:
        payload = message.content_payload if isinstance(message.content_payload, dict) else {}
        raw_cards = payload.get("order_cards")
        if not isinstance(raw_cards, list):
            continue
        message_order_nos = _unique_order_nos(
            card.get("order_id") for card in raw_cards if isinstance(card, Mapping)
        )
        if latest_single_order_no is None and len(message_order_nos) == 1:
            latest_single_order_no = message_order_nos[0]
        for card in raw_cards:
            if not isinstance(card, Mapping):
                continue
            order_no = card.get("order_id")
            if not isinstance(order_no, str) or not order_no.startswith("ord_"):
                continue
            if order_no in seen:
                continue
            cards.append(card)
            seen.add(order_no)
    matched = referenced_order_no_from_cards(user_text, cards)
    if matched is not None:
        return matched
    compact = re.sub(r"\s+", "", user_text).casefold()
    if latest_single_order_no is not None and any(
        marker in compact
        for marker in ("那为什么", "刚才", "这个订单", "这笔订单", "订单卡片", "它")
    ):
        return latest_single_order_no
    return None


def referenced_order_no_from_cards(
    user_text: str,
    cards: Iterable[Mapping[str, object]],
) -> str | None:
    """Choose one uniquely matching order card using user-visible attributes."""

    compact = re.sub(r"\s+", "", user_text).casefold()
    requested_amounts = _requested_amount_minor_units(compact)
    scored: list[tuple[int, str]] = []
    for card in cards:
        order_no = card.get("order_id")
        if not isinstance(order_no, str) or not order_no.startswith("ord_"):
            continue
        score = 0
        amount = card.get("payable_amount")
        if isinstance(amount, Mapping) and str(amount.get("minor_units")) in requested_amounts:
            score += 20
        store = card.get("store")
        if isinstance(store, Mapping):
            score += _visible_text_match_score(compact, store.get("store_name"), weight=3)
        items = card.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                score += _visible_text_match_score(compact, item.get("product_name"), weight=5)
                score += _visible_text_match_score(compact, item.get("sku_name"), weight=2)
        if score > 0:
            scored.append((score, order_no))
    if not scored:
        return None
    scored.sort(reverse=True)
    best_score = scored[0][0]
    winners = {order_no for score, order_no in scored if score == best_score}
    return next(iter(winners)) if len(winners) == 1 else None


def _requested_amount_minor_units(compact: str) -> set[str]:
    result: set[str] = set()
    for raw in re.findall(r"(?:¥|￥)?(\d+(?:\.\d{1,2})?)元", compact):
        try:
            result.add(str(int(Decimal(raw) * 100)))
        except (InvalidOperation, ValueError):
            continue
    for raw in re.findall(r"(\d+)块多", compact):
        try:
            base_minor = int(raw) * 100
        except ValueError:
            continue
        result.update(str(base_minor + remainder) for remainder in range(1, 100))
    return result


def _visible_text_match_score(compact: str, value: object, *, weight: int) -> int:
    if not isinstance(value, str):
        return 0
    candidate = re.sub(r"\s+", "", value).casefold()
    if not candidate:
        return 0
    longest = _longest_common_substring_length(compact, candidate)
    return weight * min(longest, 8) if longest >= 2 else 0


def _longest_common_substring_length(left: str, right: str) -> int:
    previous = [0] * (len(right) + 1)
    longest = 0
    for left_char in left:
        current = [0]
        for index, right_char in enumerate(right, start=1):
            value = previous[index - 1] + 1 if left_char == right_char else 0
            current.append(value)
            longest = max(longest, value)
        previous = current
    return longest


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
