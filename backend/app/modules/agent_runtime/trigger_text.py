from __future__ import annotations

from collections.abc import Mapping

from app.modules.messaging.models import Message


def agent_trigger_text(message: Message) -> str:
    """Build a truthful current-turn description for text and structured cards."""

    if message.text_content:
        return message.text_content
    payload = message.content_payload if isinstance(message.content_payload, Mapping) else {}
    if message.message_type == "order_card":
        order_no = str(payload.get("order_id") or "当前订单")
        amount = payload.get("payable_amount")
        display_amount = _money_display(amount)
        suffix = f"，订单支付总额为 {display_amount}" if display_amount else ""
        return (
            f"用户发送了订单卡片 {order_no}{suffix}。请读取这张卡片绑定的当前订单上下文，"
            "确认已收到并解释可查询的订单信息。不要仅因收到卡片而转人工。"
        )
    if message.message_type == "product_card":
        product_name = str(payload.get("product_name") or payload.get("product_id") or "当前商品")
        return (
            f"用户发送了商品卡片“{product_name}”。请读取这张卡片绑定的当前商品上下文，"
            "确认已收到并说明可以继续咨询的商品信息。不要仅因收到卡片而转人工。"
        )
    return "用户发送了一条结构化会话消息，请按其消息类型和已绑定上下文处理。"


def agent_trace_question(message: Message) -> str:
    """Describe the current turn without exposing runtime guidance in the UI."""

    if message.text_content:
        return message.text_content
    payload = message.content_payload if isinstance(message.content_payload, Mapping) else {}
    if message.message_type == "order_card":
        order_no = str(payload.get("display_order_id") or payload.get("order_id") or "当前订单")
        display_amount = _money_display(payload.get("payable_amount"))
        amount_text = f"，支付总额 {display_amount}" if display_amount else ""
        return f"用户发送了订单卡片 {order_no}{amount_text}。"
    if message.message_type == "product_card":
        product_name = str(payload.get("product_name") or payload.get("product_id") or "当前商品")
        return f"用户发送了商品卡片“{product_name}”。"
    return "用户发送了一条结构化会话消息。"


def _money_display(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    try:
        minor_units = int(str(value.get("minor_units")))
    except (TypeError, ValueError):
        return None
    currency = str(value.get("currency") or "CNY").upper()
    symbol = "¥" if currency == "CNY" else f"{currency} "
    return f"{symbol}{minor_units / 100:.2f}"
