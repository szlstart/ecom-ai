from app.modules.agent_runtime.exclusive_agent import _render, _resource_no
from app.modules.agent_runtime.exclusive_model_gateway import ExclusiveAgentPlan
from app.modules.agent_runtime.exclusive_tools import _catalog_search_candidates


def test_resource_no_extracts_only_bounded_business_identifier() -> None:
    order_no = "ord_01M19K9GS9ZG90TSGAFJ3DPMNY"
    assert _resource_no(f"请查询订单 {order_no} 的物流", "ord") == order_no
    assert _resource_no("请查询别人的编号 ord_short", "ord") is None
    assert _resource_no("prd_01M19K9GS9ZG90TSGAFJ3DPMNY", "ord") is None


def test_catalog_candidates_remove_instruction_but_keep_business_term() -> None:
    candidates = _catalog_search_candidates(
        "请列出平台当前在售的文具商品，告诉我商品名、价格和店铺，并说明推荐依据。不要转人工。"
    )
    assert candidates[0] == "文具"
    assert None not in candidates


def test_catalog_candidates_allow_only_genuinely_broad_catalog_fallback() -> None:
    assert _catalog_search_candidates("请列出全平台当前在售的全部商品")[-1] is None
    assert _catalog_search_candidates("不存在的独角兽水杯") == ["不存在的独角兽水杯"]


def test_order_fallback_renders_amount_and_localized_status() -> None:
    rendered = _render(
        ExclusiveAgentPlan("order_lookup"),
        {
            "items": [
                {
                    "order_id": "ord_01M19K9GS9ZG90TSGAFJ3DPMNY",
                    "store_name": "文具专卖店",
                    "status": {"order": "pending_shipment"},
                    "amounts": {"paid": {"display": "¥6.00"}},
                }
            ]
        },
    )
    assert "¥6.00" in rendered
    assert "待发货" in rendered
    assert "pending_shipment" not in rendered


def test_logistics_fallback_renders_tracking_location_and_localized_status() -> None:
    rendered = _render(
        ExclusiveAgentPlan("logistics_lookup"),
        {
            "items": [
                {
                    "carrier_name": "模拟快递",
                    "tracking_no_masked": "FAKE****1234",
                    "shipment_status": "in_transit",
                    "last_track": {
                        "description": "正在派送中...",
                        "location_text": "海淀区",
                    },
                    "delivery_estimate": {"status": "unavailable"},
                }
            ]
        },
    )
    assert "FAKE****1234" in rendered
    assert "运输中" in rendered
    assert "海淀区" in rendered
    assert "in_transit" not in rendered
