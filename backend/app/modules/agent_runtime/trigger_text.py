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
            "用户尚未说明具体问题。只需自然确认已经看到订单，并询问用户遇到的是付款、"
            "发货、物流、收货还是售后问题。不要主动罗列全部订单字段，也不要仅因收到卡片"
            "而转人工。"
        )
    if message.message_type == "product_card":
        product_name = str(payload.get("product_name") or payload.get("product_id") or "当前商品")
        return (
            f"用户发送了商品卡片“{product_name}”。请读取这张卡片绑定的当前商品上下文，"
            "用两到四个短段落做简洁、有购买帮助的介绍，只挑三到五项重要信息，最后询问"
            "用户更关心款式、尺码或规格、库存、发货还是使用场景。不要罗列全部字段，也不要"
            "仅因收到卡片而转人工。"
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
