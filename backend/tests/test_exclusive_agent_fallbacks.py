from app.modules.agent_runtime.exclusive_agent import (
    _render,
    _requests_latest_order,
    _resource_no,
)
from app.modules.agent_runtime.exclusive_model_gateway import ExclusiveAgentPlan
from app.modules.agent_runtime.exclusive_tools import _catalog_search_candidates
from app.modules.agent_runtime.operations_agent import _render_multi_agent


def test_resource_no_extracts_only_bounded_business_identifier() -> None:
    order_no = "ord_01M19K9GS9ZG90TSGAFJ3DPMNY"
    assert _resource_no(f"请查询订单 {order_no} 的物流", "ord") == order_no
    assert _resource_no("请查询别人的编号 ord_short", "ord") is None
    assert _resource_no("prd_01M19K9GS9ZG90TSGAFJ3DPMNY", "ord") is None


def test_latest_order_language_is_detected_without_treating_any_order_question_as_latest() -> None:
    assert _requests_latest_order("请查我最近一笔订单的物流") is True
    assert _requests_latest_order("刚买的商品能不能退款") is True
    assert _requests_latest_order("这个订单能不能退款") is False


def test_catalog_candidates_remove_instruction_but_keep_business_term() -> None:
    candidates = _catalog_search_candidates(
        "请列出平台当前在售的文具商品，告诉我商品名、价格和店铺，并说明推荐依据。不要转人工。"
    )
    assert candidates[0] == "文具"
    assert None not in candidates


def test_catalog_candidates_allow_only_genuinely_broad_catalog_fallback() -> None:
    assert _catalog_search_candidates("请列出全平台当前在售的全部商品")[-1] is None
    assert _catalog_search_candidates("不存在的独角兽水杯") == ["不存在的独角兽水杯"]


def test_catalog_candidates_drop_trailing_presentation_columns() -> None:
    assert _catalog_search_candidates(
        "请搜索当前在售的铅笔商品，列出商品名、店铺、价格和实时可售库存，不要转人工。"
    )[0] == "铅笔"


def test_catalog_candidates_extract_product_from_store_worded_follow_up() -> None:
    assert _catalog_search_candidates(
        "请告诉我本店绿杆2B铅笔所有款式的名称、价格和实时可售库存，并说明10支款是否能买。不要转人工。"
    )[0] == "绿杆2B铅笔"


def test_product_recommendation_fallback_exposes_live_stock_evidence() -> None:
    rendered = _render(
        ExclusiveAgentPlan("personalized_recommendation"),
        {
            "items": [
                {
                    "product_id": "prd_01M11Z2GF6J1C8T661HPNBRQ2D",
                    "name": "蓝色测试文具",
                    "store_name": "文具专卖店",
                    "price": {"min_amount": 600, "currency": "CNY"},
                    "available_stock": 17,
                }
            ],
            "recalled_memories": [{"value": "偏好蓝色、简约风格"}],
        },
    )

    assert "偏好蓝色、简约风格" in rendered
    assert "¥6.00" in rendered
    assert "可售库存 17" in rendered


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


def test_refund_precheck_is_read_only_and_renders_exact_money() -> None:
    rendered = _render(
        ExclusiveAgentPlan("refund_precheck"),
        {
            "order_id": "ord_01M19K9GS9ZG90TSGAFJ3DPMNY",
            "status": {
                "order": "shipped",
                "payment": "paid",
                "fulfillment": "delivered",
            },
            "refund_eligibility": {
                "eligible": True,
                "suggested_refund_amount": {"minor_units": "600", "currency": "CNY"},
                "allowed_types": ["refund_only", "return_and_refund"],
                "blocking_reasons": [],
            },
            "shipments": [
                {
                    "shipment_status": "delivered",
                    "last_track": {"description": "已签收", "location_text": "河滨嘉苑14-1"},
                }
            ],
        },
    )

    assert "¥6.00" in rendered
    assert "仅退款、退货退款" in rendered
    assert "没有创建退款草稿或售后单" in rendered
    assert "河滨嘉苑14-1" in rendered


def test_multi_agent_fallback_flattens_metrics_and_provides_risk_advice() -> None:
    rendered = _render_multi_agent(
        {
            "specialists": {
                "users": {
                    "specialist": "governance_users",
                    "data": {"user_status_counts": {"active": 3}},
                },
                "runtime": {
                    "specialist": "observability",
                    "data": {"pending_outbox_events": 2, "failed_agent_runs": 0},
                },
                "stores": {
                    "specialist": "governance_stores",
                    "data": {"product_status_counts": {"on_sale": 2}},
                },
            }
        }
    )
    assert "user_status_counts.active=3" in rendered
    assert "pending_outbox_events=2" in rendered
    assert "风险" in rendered
    assert "上线前建议" in rendered
    assert "1. " in rendered
    assert "2. " in rendered
    assert "3. " in rendered
