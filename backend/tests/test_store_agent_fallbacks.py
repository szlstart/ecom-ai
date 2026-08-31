from app.modules.agent_runtime.model_gateway import StoreAgentPlan
from app.modules.agent_runtime.store_agent import _render


def test_order_fallback_localizes_status_actions_and_money() -> None:
    answer = _render(
        StoreAgentPlan("order_explain"),
        {
            "status": {
                "order": "shipped",
                "payment": "paid",
                "fulfillment": "shipped",
                "after_sale": "none",
            },
            "amounts": {"paid": {"minor_units": "600", "currency": "CNY"}},
            "available_actions": [
                "view_logistics",
                "confirm_receipt",
                "apply_after_sale",
            ],
            "shipments": [{}],
        },
    )

    assert "订单运输中" in answer
    assert "支付已支付" in answer
    assert "履约已发货" in answer
    assert "售后无进行中售后" in answer
    assert "¥6.00" in answer
    assert "查看物流、确认收货、申请售后" in answer
    for internal_code in (
        "shipped",
        "paid",
        "none",
        "view_logistics",
        "confirm_receipt",
        "apply_after_sale",
    ):
        assert internal_code not in answer
