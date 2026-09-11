from types import SimpleNamespace
from typing import Any, cast

from app.modules.agent_runtime.exclusive_agent import (
    _cart_card,
    _compact_tracking_no,
    _exclusive_detail_cards,
    _render,
    _requests_latest_order,
    _requires_exact_catalog_rendering,
    _resource_no,
)
from app.modules.agent_runtime.exclusive_model_gateway import ExclusiveAgentPlan
from app.modules.agent_runtime.exclusive_tools import (
    _catalog_search_candidates,
    _catalog_search_constraints,
    _combined_catalog_search_candidates,
    _filter_order_rows,
)
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


def test_single_product_inventory_uses_actionable_detail_card() -> None:
    data = {
        "catalog_focus": "sku_availability",
        "items": [
            {
                "product_id": "prd_01KPRODUCT",
                "name": "考试铅笔",
                "available_stock": 12,
                "skus": [
                    {
                        "sku_name": "6支",
                        "available_stock": 12,
                        "availability_label": "有货",
                        "price": {"minor_units": "600", "currency": "CNY"},
                    }
                ],
            }
        ],
    }

    rendered = _render(ExclusiveAgentPlan("product_search"), data)
    cards = _exclusive_detail_cards(ExclusiveAgentPlan("product_search"), data)

    assert "共可售 12 件" in rendered
    assert cards[0]["kind"] == "sku_availability"
    assert "6支" in str(cards)
    assert "¥6.00" in str(cards)


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
    assert (
        _catalog_search_candidates(
            "请搜索当前在售的铅笔商品，列出商品名、店铺、价格和实时可售库存，不要转人工。"
        )[0]
        == "铅笔"
    )


def test_catalog_candidates_extract_product_from_store_worded_follow_up() -> None:
    candidates = _catalog_search_candidates(
        "请告诉我本店绿杆2B铅笔所有款式的名称、价格和实时可售库存，并说明10支款是否能买。不要转人工。"
    )
    assert candidates[0] == "绿杆2B铅笔"
    assert "绿杆" in candidates
    assert "2B" in candidates
    assert "铅笔" in candidates


def test_catalog_candidates_fall_back_to_exact_current_message() -> None:
    candidates = _combined_catalog_search_candidates(
        "所有款式和实时库存",
        "请告诉我本店绿杆2B铅笔所有款式的名称、价格和实时可售库存",
    )
    assert "绿杆" in candidates
    assert "2B" in candidates
    assert "铅笔" in candidates


def test_catalog_constraints_keep_budget_and_specific_terms_out_of_broad_fallback() -> None:
    constraints = _catalog_search_constraints(
        "全平台商品",
        "帮我找20元以内、适合考试使用的文具",
    )

    assert constraints.price_max == 2000
    assert constraints.price_min is None
    assert "考试" in constraints.keywords
    assert "文具" in constraints.keywords
    assert None not in constraints.candidates


def test_catalog_constraints_support_decimal_lower_and_upper_budget() -> None:
    constraints = _catalog_search_constraints(
        None,
        "想找至少 8.50 元、不超过 19.90 元的铅笔",
    )

    assert constraints.price_min == 850
    assert constraints.price_max == 1990
    assert "铅笔" in constraints.keywords


def test_catalog_constraints_discard_requested_result_count_before_searching() -> None:
    constraints = _catalog_search_constraints(None, "帮我找几件20元以内的文具")

    assert constraints.candidates[0] == "文具"
    assert "几件" not in constraints.keywords


def test_catalog_constraints_expand_exam_purpose_without_dropping_budget() -> None:
    constraints = _catalog_search_constraints(None, "推荐三件20元以内适合考试的文具")

    assert constraints.price_max == 2000
    assert constraints.requested_limit == 3
    assert {"铅笔", "橡皮", "直尺", "笔芯"}.issubset(set(constraints.candidates))


def test_order_list_filter_understands_store_and_product_references() -> None:
    male_order = SimpleNamespace(id=1)
    stationery_order = SimpleNamespace(id=2)
    male_store = SimpleNamespace(store_name="男装专卖店")
    stationery_store = SimpleNamespace(store_name="文具专卖店")
    rows = [(male_order, male_store), (stationery_order, stationery_store)]
    items = {
        1: [SimpleNamespace(product_name="CHICERRO 弯刀裤")],
        2: [SimpleNamespace(product_name="绿杆2B铅笔")],
    }

    typed_rows = cast(Any, rows)
    typed_items = cast(Any, items)
    assert _filter_order_rows(typed_rows, typed_items, "我刚刚买的男装订单")[0][0].id == 1
    assert _filter_order_rows(typed_rows, typed_items, "铅笔订单")[0][0].id == 2
    assert len(_filter_order_rows(typed_rows, typed_items, "我的订单")) == 2


def test_product_recommendation_fallback_defers_dense_facts_to_product_cards() -> None:
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
                    "skus": [
                        {
                            "sku_name": "10支装",
                            "price": {"display": "¥8.00"},
                            "available_stock": 0,
                            "availability_label": "缺货",
                        }
                    ],
                }
            ],
            "recalled_memories": [{"value": "偏好蓝色、简约风格"}],
        },
    )

    assert "偏好蓝色、简约风格" in rendered
    assert "点击卡片" in rendered
    assert "10支装" not in rendered
    assert _requires_exact_catalog_rendering("personalized_recommendation") is True
    assert _requires_exact_catalog_rendering("product_search") is True
    assert _requires_exact_catalog_rendering("policy_qa") is True


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
    assert "1 笔最近订单" in rendered
    assert "点击卡片" in rendered
    assert "ord_01M19K9GS9ZG90TSGAFJ3DPMNY" not in rendered


def test_cart_fallback_and_card_keep_cart_distinct_from_orders() -> None:
    data = {
        "cart_total_quantity": 3,
        "selected_quantity": 2,
        "valid_item_count": 2,
        "amount_summary": {"selected_goods_amount": {"minor_units": "1200", "currency": "CNY"}},
        "groups": [
            {
                "store_id": "sto_1",
                "store_name": "文具专卖店",
                "selected_quantity": 2,
                "items": [
                    {
                        "product_id": "prd_1",
                        "product_name": "考试铅笔",
                        "sku_name": "2B",
                        "quantity": 3,
                        "current_price": {"minor_units": "600", "currency": "CNY"},
                        "is_selected": True,
                        "is_valid": True,
                    }
                ],
            }
        ],
    }

    rendered = _render(ExclusiveAgentPlan("cart_lookup"), data)
    card = _cart_card(data)

    assert "购物车里共有 3 件商品" in rendered
    assert card["total_quantity"] == 3
    assert card["selected_amount"] == {"minor_units": "1200", "currency": "CNY"}
    groups = cast(list[dict[str, Any]], card["groups"])
    assert groups[0]["items"][0]["product_name"] == "考试铅笔"


def test_product_compare_renders_structured_same_basis_rows() -> None:
    data = {
        "items": [
            {
                "product_id": "prd_1",
                "name": "考试铅笔",
                "store_name": "文具店",
                "price": {"min_amount": 600, "currency": "CNY"},
                "sku_count": 2,
                "available_stock": 30,
                "sales_count": 8,
                "rating": "4.80",
            },
            {
                "product_id": "prd_2",
                "name": "透明直尺",
                "store_name": "文具店",
                "price": {"min_amount": 871, "currency": "CNY"},
                "sku_count": 1,
                "available_stock": 20,
                "sales_count": 5,
                "rating": "4.70",
            },
        ]
    }

    rendered = _render(ExclusiveAgentPlan("product_compare"), data)
    cards = _exclusive_detail_cards(ExclusiveAgentPlan("product_compare"), data)

    assert "对比卡片" in rendered
    assert "考试铅笔" in str(cards)
    assert "¥6.00" in str(cards)
    assert "库存 30" in str(cards)


def test_product_compare_can_make_evidence_bounded_exam_recommendation() -> None:
    rendered = _render(
        ExclusiveAgentPlan("product_compare"),
        {
            "items": [
                {"name": "透明直尺15cm", "description": "绘图测量"},
                {"name": "金属美工刀", "description": "手帐切割"},
            ]
        },
        "对比前两个，哪个更适合考试?",
    )

    assert "透明直尺15cm" in rendered
    assert "更贴近考试使用场景" in rendered
    assert "具体考试规定" in rendered


def test_policy_fallback_selects_one_relevant_sentence_instead_of_dumping_chunks() -> None:
    rendered = _render(
        ExclusiveAgentPlan("policy_qa"),
        {
            "knowledge_sources": [
                {
                    "title": "[系统] 支付、余额与模拟充值规则",
                    "version": "v1",
                    "excerpt": (
                        "# 支付规则\n- 金额按分保存。\n"
                        "- 当前微信和支付宝充值只用于本地演示，不会产生真实资金扣款。\n"
                        "- 重复请求不会重复到账。"
                    ),
                },
                {
                    "title": "物流规则",
                    "version": "v1",
                    "excerpt": "当前使用模拟物流，轨迹按五秒间隔更新。",
                },
            ]
        },
        "请用一句话说明本地模拟充值会不会真的从微信或支付宝扣款。",
    )

    assert rendered == (
        "根据当前已发布平台规则\uff1a《支付、余额与模拟充值规则》\uff1a"
        "当前微信和支付宝充值只用于本地演示，"
        "不会产生真实资金扣款。"
    )
    assert "物流" not in rendered
    assert "金额按分保存" not in rendered


def test_policy_fallback_prioritizes_refund_timing_and_deduplicates_source_cards() -> None:
    data = {
        "policy_query": "平台退款一般多久到账?",
        "knowledge_sources": [
            {
                "document_id": "doc_refund",
                "title": "[系统] 售后、退款与客服规则",
                "excerpt": "提交退款申请不等于退款到账; 申请仍需经过售后处理。",
            },
            {
                "document_id": "doc_refund",
                "title": "[系统] 售后、退款与客服规则",
                "excerpt": "当前项目使用模拟支付与退款，不承诺真实支付渠道的固定到账天数。",
            },
            {
                "document_id": "doc_logistics",
                "title": "[系统] 物流规则",
                "excerpt": "物流轨迹按节点更新。",
            },
        ],
    }

    rendered = _render(ExclusiveAgentPlan("policy_qa"), data, "平台退款一般多久到账?")
    cards = _exclusive_detail_cards(ExclusiveAgentPlan("policy_qa"), data)

    assert "退款到账" in rendered
    assert len(cards) == 1
    assert len(cast(list[dict[str, object]], cards[0]["rows"])) == 1
    assert "售后、退款与客服规则" in str(cards)
    assert "物流规则" not in str(cards)


def test_logistics_fallback_renders_tracking_location_and_localized_status() -> None:
    data = {
        "order_id": "ord_TRACK",
        "items": [
            {
                "carrier_name": "模拟快递",
                "tracking_no_masked": "FAKE****1234",
                "shipment_status": "in_transit",
                "last_track": {
                    "description": "正在派送中...",
                    "location_text": "海淀区",
                },
            }
        ],
    }
    rendered = _render(ExclusiveAgentPlan("logistics_lookup"), data)
    cards = _exclusive_detail_cards(ExclusiveAgentPlan("logistics_lookup"), data)
    assert "点击卡片" in rendered
    assert "FAKE****1234" in str(cards)
    assert "运输中" in str(cards)
    assert "海淀区" in str(cards)
    assert "in_transit" not in str(cards)
    assert _compact_tracking_no("*****************************ZVA9") == "尾号 ZVA9"


def test_refund_precheck_is_read_only_and_renders_exact_money() -> None:
    data = {
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
    }
    rendered = _render(ExclusiveAgentPlan("refund_precheck"), data)
    cards = _exclusive_detail_cards(ExclusiveAgentPlan("refund_precheck"), data)

    assert "金额、类型和下一步入口已整理在卡片中" in rendered
    assert "没有创建退款草稿或售后单" in rendered
    assert "¥6.00" in str(cards)
    assert "仅退款、退货退款" in str(cards)
    assert "河滨嘉苑14-1" not in rendered


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
                    "data": {
                        "pending_outbox_events": 2,
                        "stale_pending_outbox_events": 2,
                        "failed_agent_runs_24h": 1,
                        "successful_runs_after_latest_failure": 35,
                        "unrecovered_agent_failures": 0,
                    },
                },
                "stores": {
                    "specialist": "governance_stores",
                    "data": {"product_status_counts": {"on_sale": 2}},
                },
            }
        }
    )
    assert "3 个专业 Agent" in rendered
    assert "2 条 Outbox 事件超过 5 分钟未处理" in rendered
    assert "已有 35 次成功运行" in rendered
    assert "卡片" in rendered
    assert "user_status_counts" not in rendered
    assert "pending_outbox_events" not in rendered
    assert len(rendered) < 220
