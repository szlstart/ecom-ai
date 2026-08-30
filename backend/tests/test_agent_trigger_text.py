from app.modules.agent_runtime.trigger_text import agent_trace_question, agent_trigger_text
from app.modules.messaging.models import Message


def _message(message_type: str, payload: dict[str, object]) -> Message:
    return Message(
        message_no="msg_01KTRIGGER0000000000000000",
        conversation_id=1,
        sequence_no=1,
        client_message_no="cmsg_01KTRIGGER000000000000000",
        sender_type="user",
        sender_id=1,
        message_type=message_type,
        text_content=None,
        content_payload=payload,
        message_status="sent",
        moderation_status="passed",
    )


def test_order_card_trigger_uses_exact_major_amount_and_does_not_request_handoff() -> None:
    message = _message(
        "order_card",
        {
            "order_id": "ord_01KORDER",
            "payable_amount": {"minor_units": "600", "currency": "CNY"},
        },
    )
    text = agent_trigger_text(message)

    assert "¥6.00" in text
    assert "¥600" not in text
    assert "不要仅因收到卡片而转人工" in text
    public_text = agent_trace_question(message)
    assert public_text == "用户发送了订单卡片 ord_01KORDER，支付总额 ¥6.00。"
    assert "不要仅因" not in public_text


def test_product_card_trigger_names_selected_product() -> None:
    text = agent_trigger_text(
        _message("product_card", {"product_id": "prd_01KPRODUCT", "product_name": "测试铅笔"})
    )
    assert "测试铅笔" in text
    assert "当前商品上下文" in text
