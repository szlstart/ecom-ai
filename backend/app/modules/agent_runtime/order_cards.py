from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.modules.agent_runtime.conversation_state import (
    ConversationStateRuntime,
    latest_card_set,
)
from app.modules.identity.models import User
from app.modules.messaging.models import Conversation, Message
from app.modules.messaging.service import MessagingService


def requests_direct_transaction_action(user_text: str) -> bool:
    """Return true when a shopper asks the Agent to perform a protected action."""

    compact = "".join(user_text.split()).casefold()
    # Payment-channel names such as “支付宝” contain the word “支付” but do
    # not by themselves mean “pay an order”. Keep “用支付宝帮我付款” blocked
    # through the remaining explicit 付款 phrase.
    action_text = compact.replace("支付宝", "渠道")
    for state_label in (
        "待付款",
        "未付款",
        "已付款",
        "付款订单",
        "支付状态",
        "支付记录",
        # These are temporal conditions in pre-sale/service questions, not a
        # request for the Agent to move money.  For example: “告诉我付款后几天
        # 发货” must remain a product-policy question even though “给我” appears
        # elsewhere in the compound sentence.
        "付款后",
        "支付后",
        "付款前",
        "支付前",
    ):
        action_text = action_text.replace(state_label, "")
    action = any(marker in action_text for marker in ("付款", "支付", "确认收货", "取消订单"))
    delegation = any(marker in compact for marker in ("帮我", "替我", "代我", "给我", "直接"))
    explanatory_only = any(
        marker in compact
        for marker in (
            "只解释",
            "只说明",
            "别操作",
            "不要操作",
            "不执行",
            "不要执行",
            "能不能",
            "是否可以",
            "怎么",
            "如何",
        )
    )
    return action and delegation and not explanatory_only


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
    limit: int = 8,
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

    state_cards = latest_card_set(
        await ConversationStateRuntime(session).load(
            conversation,
            before_sequence=before_sequence,
        ),
        "order",
        minimum_count=minimum_count,
    )
    if state_cards is not None:
        return _unique_order_nos(card.get("order_id") for card in state_cards)

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
        trace = payload.get("execution_trace")
        trace_intent = trace.get("intent") if isinstance(trace, Mapping) else None
        if trace_intent == "order_lookup" and (not isinstance(cards, list) or not cards):
            # “当前没有待付款订单” is a real, completed order-list result. It
            # owns deictic references such as “刚才第一笔”; skipping across this
            # empty turn would silently bind them to an unrelated older order.
            return []
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
    latest_visible_order_nos: list[str] | None = None
    for message in rows:
        payload = message.content_payload if isinstance(message.content_payload, dict) else {}
        raw_cards = payload.get("order_cards")
        trace = payload.get("execution_trace")
        trace_intent = trace.get("intent") if isinstance(trace, Mapping) else None
        if trace_intent == "order_lookup" and (not isinstance(raw_cards, list) or not raw_cards):
            # Stop at the newest explicit empty result. A later named query can
            # still search live orders, but pronouns and ordinals must not tunnel
            # through to stale visual context.
            break
        if not isinstance(raw_cards, list):
            continue
        message_order_nos = _unique_order_nos(
            card.get("order_id") for card in raw_cards if isinstance(card, Mapping)
        )
        if latest_visible_order_nos is None and message_order_nos:
            latest_visible_order_nos = message_order_nos
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
    latest_cards = [
        card
        for card in cards
        if latest_visible_order_nos is not None and card.get("order_id") in latest_visible_order_nos
    ]
    extreme_order_no = extreme_order_no_from_cards(user_text, latest_cards)
    if extreme_order_no is not None:
        return extreme_order_no
    matched = referenced_order_no_from_cards(user_text, cards)
    if matched is not None:
        return matched
    compact = re.sub(r"\s+", "", user_text).casefold()
    if (
        latest_visible_order_nos is not None
        and len(latest_visible_order_nos) == 1
        and any(
            marker in compact
            for marker in ("那为什么", "刚才", "这个订单", "这笔订单", "订单卡片", "它")
        )
    ):
        return latest_visible_order_nos[0]
    return None


def extreme_order_no_from_cards(
    user_text: str,
    cards: Iterable[Mapping[str, object]],
) -> str | None:
    """Resolve an explicit highest/lowest amount request within one visible card set."""

    compact = re.sub(r"\s+", "", user_text).casefold()
    wants_highest = any(
        marker in compact for marker in ("金额最高", "实付最高", "最贵一笔", "花得最多", "金额最大")
    )
    wants_lowest = any(
        marker in compact
        for marker in ("金额最低", "实付最低", "最便宜一笔", "花得最少", "金额最小")
    )
    if wants_highest == wants_lowest:
        return None
    priced: list[tuple[int, str]] = []
    for card in cards:
        order_no = card.get("order_id")
        amount = card.get("payable_amount")
        if not isinstance(order_no, str) or not isinstance(amount, Mapping):
            continue
        try:
            priced.append((max(0, int(amount.get("minor_units") or 0)), order_no))
        except (TypeError, ValueError):
            continue
    if not priced:
        return None
    target = (max if wants_highest else min)(amount for amount, _order_no in priced)
    winners = [order_no for amount, order_no in priced if amount == target]
    return winners[0] if len(winners) == 1 else None


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
    chinese_digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    for raw in re.findall(r"([一二两三四五六七八九十])(?:元|块钱|块)", compact):
        result.add(str(chinese_digits[raw] * 100))
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
    index = order_reference_index(user_text)
    if index is not None:
        return order_nos[index] if index < len(order_nos) else None
    if len(order_nos) == 1 and any(
        marker in compact
        for marker in (
            "这个订单",
            "这笔订单",
            "这笔",
            "它",
            "刚才那个",
            "刚才的",
            "现在可以",
            "只检查",
            "不要提交",
        )
    ):
        return order_nos[0]
    return None


def order_reference_index(user_text: str) -> int | None:
    indices = order_reference_indices(user_text)
    compact = re.sub(r"\s+", "", user_text).casefold()
    if len(indices) >= 2 and any(
        marker in compact
        for marker in ("我说的是", "应该是", "而是", "改成", "改为", "纠正")
    ):
        # “不是第一笔，我说的是第二笔”中的第一笔是被否定对象，
        # 真正目标是更正短语之后最后出现的序号。
        return indices[-1]
    # 普通复合问题可能在说明原因时再次提到旧序号，例如：
    # “第二笔是哪一笔，并说明为什么上一轮仍显示第一笔”。此时最先出现
    # 的序号才是本次查询目标，不能按固定的一二三匹配顺序误选第一笔。
    return indices[0] if indices else None


def order_reference_indices(user_text: str) -> list[int]:
    """Return every order-card ordinal in textual order."""

    compact = re.sub(r"\s+", "", user_text).casefold()
    ordinal_groups = (
        (("第一笔", "第一个", "第1笔", "1号"), 0),
        (("第二笔", "第二个", "第2笔", "2号"), 1),
        (("第三笔", "第三个", "第3笔", "3号"), 2),
        (("第四笔", "第四个", "第4笔", "4号"), 3),
        (("第五笔", "第五个", "第5笔", "5号"), 4),
    )
    matches: list[tuple[int, int]] = []
    for markers, index in ordinal_groups:
        positions = [compact.find(marker) for marker in markers if marker in compact]
        if positions:
            matches.append((min(positions), index))
    return [index for _position, index in sorted(matches)]
