from types import SimpleNamespace
from typing import Any, cast

from app.modules.agent_runtime.exclusive_agent import (
    _address_cards,
    _asks_address_postcode,
    _asks_order_spend_summary,
    _cart_add_verification_text,
    _cart_card,
    _cart_hypothetical_projection,
    _compact_policy_sources,
    _compact_tracking_no,
    _comparative_product_card,
    _corrects_order_reference,
    _delivery_comparison_text,
    _delivery_estimate_text,
    _exclusive_detail_cards,
    _is_chinese_trace_text,
    _merge_supervisor_plans,
    _order_spend_summary_text,
    _preferred_sku_nos,
    _product_stock_extreme_answer,
    _references_purchased_item,
    _render,
    _requested_cart_add_quantity,
    _requested_product_sku,
    _requests_address_mutation,
    _requests_compound_advice,
    _requests_favorite_mutation,
    _requests_latest_order,
    _requests_multiple_logistics,
    _requests_order_list,
    _requires_exact_catalog_rendering,
    _resource_no,
    _select_cart_item,
    _single_plan_with_non_overridable_guards,
    _sku_stock_extreme_answer,
    _store_name_from_favorite_update_message,
    _targets_store_favorite_update,
    _wallet_cart_affordability_text,
)
from app.modules.agent_runtime.exclusive_model_gateway import (
    DeterministicExclusiveModelGateway,
    ExclusiveAgentPlan,
    ExclusiveSupervisorPlan,
    ExclusiveSupervisorSubtask,
    _requested_catalog_groups,
    _requests_memory_lookup,
)
from app.modules.agent_runtime.exclusive_tools import (
    _catalog_search_candidates,
    _catalog_search_constraints,
    _combined_catalog_search_candidates,
    _excluded_catalog_terms,
    _explicit_catalog_colors,
    _extract_catalog_sort,
    _filter_order_rows,
    _matches_explicit_catalog_kind,
    _matches_requested_catalog_seasons,
    _meaningful_product_reference,
    _preferred_catalog_colors,
    _requested_catalog_weight,
    _requested_order_limit,
    _requests_order_spend_summary_query,
    _requests_order_state_overview,
    _semantic_catalog_expansions,
    catalog_query_with_inherited_constraints,
)
from app.modules.agent_runtime.operations_agent import _render_multi_agent


def test_requested_order_limit_honors_explicit_card_count() -> None:
    assert _requested_order_limit("我最近买过哪些东西?只展示最近3笔") == 3
    assert _requested_order_limit("列出两笔订单") == 2
    assert _requested_order_limit("看看我的订单") == 5


def test_order_list_request_accepts_qualifier_between_count_and_order() -> None:
    assert _requests_order_list("请用订单卡片列出我最近两笔有效订单，只说关键状态")
    assert _requests_order_list("展示最近3笔可见订单")


def test_multi_order_after_sale_question_cannot_become_refund_draft() -> None:
    fallback = ExclusiveAgentPlan("order_lookup")
    provider = ExclusiveAgentPlan("refund_eligibility")

    selected, source = _single_plan_with_non_overridable_guards(
        "这两笔订单中，哪一笔现在可以申请售后?",
        provider_plan=provider,
        fallback_plan=fallback,
    )

    assert selected.intent == "order_lookup"
    assert source == "deterministic_read_only_guard"


async def test_valid_provider_catalog_plan_is_not_replaced_by_keyword_fallback() -> None:
    question = "帮我找适合考试的文具和便携笔记本，分成两组推荐"
    deterministic = await DeterministicExclusiveModelGateway().plan_tasks(question)

    assert _requested_catalog_groups(question) == (
        "推荐适合考试的文具",
        "推荐便携笔记本",
    )
    assert [task.objective for task in deterministic.tasks] == [
        "推荐适合考试的文具",
        "推荐便携笔记本",
    ]

    provider = ExclusiveSupervisorPlan(
        (
            ExclusiveSupervisorSubtask(
                subtask_key="task_1",
                intent="personalized_recommendation",
                objective=question,
            ),
        )
    )
    merged = _merge_supervisor_plans(
        provider,
        deterministic,
        force_deterministic_intent=None,
        preserve_compound_coverage=False,
    )
    assert [task.objective for task in merged.tasks] == [question]
    assert _requested_order_limit("哪些订单可以评价和申请售后") == 8
    assert _requested_order_limit("告诉我先关注哪一笔") == 5
    assert _requests_order_list("只展示我最近3笔订单，用订单卡片给我") is True


def test_explicit_refund_draft_guard_replaces_wrong_provider_review_draft() -> None:
    provider = ExclusiveSupervisorPlan(
        (
            ExclusiveSupervisorSubtask(
                subtask_key="task_1",
                intent="review_draft",
                objective="准备一份草稿",
            ),
        )
    )
    deterministic = ExclusiveSupervisorPlan(
        (
            ExclusiveSupervisorSubtask(
                subtask_key="task_1",
                intent="refund_eligibility",
                objective="为明确订单准备退款草稿并等待用户确认",
            ),
        )
    )

    merged = _merge_supervisor_plans(
        provider,
        deterministic,
        force_deterministic_intent="refund_eligibility",
        preserve_compound_coverage=False,
    )

    assert [task.intent for task in merged.tasks] == ["refund_eligibility"]
    assert "退款" in merged.tasks[0].objective


async def test_named_product_introduction_routes_and_searches_only_the_product_name() -> None:
    question = "请介绍三端联动验收笔记本,并说明你使用了什么可信依据。"

    plan = await DeterministicExclusiveModelGateway().plan(question)
    constraints = _catalog_search_constraints(plan.search_text, question)

    assert plan.intent == "product_search"
    assert plan.search_text == "三端联动验收笔记本"
    assert constraints.keywords == ("三端联动验收笔记本",)
    assert constraints.candidates[0] == "三端联动验收笔记本"
    assert all("可信依据" not in str(candidate) for candidate in constraints.candidates)


def test_order_spend_summary_query_requires_the_full_visible_order_window() -> None:
    assert _requests_order_spend_summary_query(
        "我的可见订单累计实付多少、已退款多少？请给订单明细卡片"
    ) is True
    assert _requests_order_spend_summary_query("看看最近一笔订单") is False


def test_multiple_logistics_request_does_not_default_to_latest_order() -> None:
    assert _requests_multiple_logistics("我现在所有运输中的快递都到哪了?") is True
    assert _requests_multiple_logistics("第一笔快递到哪了?") is False


async def test_multi_order_delivery_comparison_routes_to_one_logistics_specialist() -> None:
    gateway = DeterministicExclusiveModelGateway()
    question = "把我所有运输中的订单用卡片列出, 并比较哪一单预计最早送达"

    assert (await gateway.plan(question)).intent == "logistics_lookup"
    supervisor = await gateway.plan_tasks(question)
    assert [task.intent for task in supervisor.tasks] == ["logistics_lookup"]
    assert (
        await gateway.plan("我所有运输中的快递都到哪了?比较哪单最早送达")
    ).intent == "logistics_lookup"


async def test_ordinal_order_latest_location_routes_to_logistics() -> None:
    plan = await DeterministicExclusiveModelGateway().plan("第二笔, 告诉我最新位置和是否已经签收")

    assert plan.intent == "logistics_lookup"


def test_resource_no_extracts_only_bounded_business_identifier() -> None:
    order_no = "ord_01M19K9GS9ZG90TSGAFJ3DPMNY"
    assert _resource_no(f"请查询订单 {order_no} 的物流", "ord") == order_no
    assert _resource_no("请查询别人的编号 ord_short", "ord") is None
    assert _resource_no("prd_01M19K9GS9ZG90TSGAFJ3DPMNY", "ord") is None


async def test_supervisor_decomposes_cart_clear_and_address_lookup() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "帮我把购物车里面的内容都删了，并且把我的收货地址发给我"
    )

    assert [task.intent for task in plan.tasks] == ["cart_clear", "address_lookup"]


async def test_cart_mutation_with_total_request_has_one_unique_subtask() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "刚才那条裤子改回1件，并告诉我购物车现在总额。"
    )

    assert [(task.subtask_key, task.intent) for task in plan.tasks] == [("task_1", "cart_update")]


async def test_supervisor_decomposes_order_and_address_lookup() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "把待付款订单和我的收货地址一起发给我"
    )

    assert [task.intent for task in plan.tasks] == ["order_lookup", "address_lookup"]


async def test_wallet_cart_payment_refusal_does_not_append_unrelated_order_lookup() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "余额够买购物车里的裤子吗?如果够就帮我付款"
    )

    assert [task.intent for task in plan.tasks] == ["cart_lookup", "wallet_lookup"]


async def test_refund_delay_follow_up_and_recharge_route_to_correct_domains() -> None:
    gateway = DeterministicExclusiveModelGateway()

    assert (await gateway.plan("为什么它还没有退款到账?")).intent == "refund_progress"
    assert (await gateway.plan("帮我用支付宝给余额充值100元")).intent == "policy_qa"
    assert (await gateway.plan("我最近下的那一单现在是什么状态?")).intent == "order_lookup"
    assert (
        await gateway.plan("为什么这笔最多只能退19.10元?运输中要先确认收货吗?")
    ).intent == "refund_precheck"


async def test_wallet_history_classification_is_not_misread_as_a_recharge_request() -> None:
    gateway = DeterministicExclusiveModelGateway()

    plan = await gateway.plan_tasks(
        "给我看最近5笔余额变动，并告诉我哪笔是消费、哪笔是充值。"
    )

    assert [task.intent for task in plan.tasks] == ["wallet_lookup"]


async def test_multi_state_order_overview_is_not_captured_by_after_sale_progress() -> None:
    gateway = DeterministicExclusiveModelGateway()
    question = "按订单状态整理：待付款、待发货、运输中、待评价、售后中，每种最多2笔。"

    assert (await gateway.plan(question)).intent == "order_lookup"
    assert [task.intent for task in (await gateway.plan_tasks(question)).tasks] == [
        "order_lookup"
    ]


def test_memory_latest_order_and_field_specific_queries_are_understood() -> None:
    assert _requests_memory_lookup("你目前记得我哪些购物偏好?") is True
    assert _requests_latest_order("我最近下的那一单现在是什么状态?") is True
    assert _asks_address_postcode("默认地址的邮编是多少?") is True
    assert _requests_favorite_mutation("不要执行，怎么取消收藏刚才那件裤子?") is True
    assert _asks_order_spend_summary("我已完成的订单一共花了多少钱?") is True


def test_order_spend_summary_uses_minor_units_and_reports_refunds() -> None:
    items = [
        {
            "amounts": {
                "paid": {"minor_units": "1280", "currency": "CNY"},
                "refunded": {"minor_units": "280", "currency": "CNY"},
            }
        },
        {
            "amounts": {
                "paid": {"minor_units": "600", "currency": "CNY"},
                "refunded": {"minor_units": "0", "currency": "CNY"},
            }
        },
    ]

    assert _order_spend_summary_text(items) == (
        "共 2 笔订单，累计实付 ¥18.80，已退款 ¥2.80，当前净支出 ¥16.00。明细已放在订单卡片中。"
    )


def test_single_order_payment_method_question_answers_channel_and_amount() -> None:
    answer = _render(
        ExclusiveAgentPlan("order_lookup"),
        {
            "items": [
                {
                    "payment": {
                        "method": "wallet_balance",
                        "paid_amount": {"minor_units": "600", "currency": "CNY"},
                    }
                }
            ]
        },
        "最近一笔已完成订单当时使用什么方式支付？实付多少？",
    )

    assert answer == "最近这笔订单使用“商城余额”支付，实际支付 ¥6.00。订单卡片已放在下方。"


def test_multi_state_order_overview_uses_requested_business_labels() -> None:
    answer = _render(
        ExclusiveAgentPlan("order_lookup"),
        {
            "items": [{}, {}, {}, {}, {}, {}],
            "requested_state_counts": {
                "待付款": 0,
                "待发货": 1,
                "运输中": 2,
                "待评价": 3,
                "售后中": 1,
            },
        },
        "按订单状态整理：待付款、待发货、运输中、待评价、售后中，每种最多2笔。",
    )

    assert "待付款 0 笔、待发货 1 笔、运输中 2 笔、待评价 3 笔、售后中 1 笔" in answer
    assert "当前没有待付款订单" in answer


def test_field_specific_and_hypothetical_fallbacks_state_real_boundaries() -> None:
    address_answer = _render(
        ExclusiveAgentPlan("address_lookup"),
        {"items": [{"address_id": "addr_1", "is_default": True}]},
        "默认地址邮编是多少?没有记录就直接说没有",
    )
    favorite_answer = _render(
        ExclusiveAgentPlan("favorites_lookup"),
        {"favorite_product_count": 1, "followed_store_count": 0},
        "如果取消收藏刚才的裤子会怎样?现在不要执行",
    )
    refund_answer = _render(
        ExclusiveAgentPlan("refund_progress"),
        {"items": [{"refund_id": "ref_1", "refund_status": "merchant_review"}]},
        "为什么它还没有退款到账?",
    )

    assert "没有保存邮编" in address_answer
    assert "本次没有修改任何数据" in favorite_answer
    assert "商家处理中" in refund_answer
    assert "无需重复申请" in refund_answer


async def test_supervisor_keeps_order_logistics_state_difference_as_one_grounded_task() -> None:
    gateway = DeterministicExclusiveModelGateway()
    plan = await gateway.plan_tasks("第二个物流已经签收，为什么订单还显示运输中?")
    direct = await gateway.plan("第二个物流已经签收")

    assert [task.intent for task in plan.tasks] == ["logistics_lookup"]
    assert direct.intent == "logistics_lookup"


async def test_refund_check_with_ordinal_and_do_not_submit_never_builds_a_draft() -> None:
    plan = await DeterministicExclusiveModelGateway().plan(
        "第二笔多少钱、在哪家店?顺便只检查能不能退款，不要提交。"
    )

    assert plan.intent == "refund_precheck"


async def test_explicit_refund_draft_request_is_not_reduced_to_precheck() -> None:
    plan = await DeterministicExclusiveModelGateway().plan(
        "请为我那笔8元订单准备仅退款草稿,原因是不再需要,但不要提交,等我点卡片确认。"
    )

    assert plan.intent == "refund_eligibility"


async def test_agent_capability_comparison_is_not_misrouted_as_catalog_search() -> None:
    plan = await DeterministicExclusiveModelGateway().plan("你和普通商城搜索框有什么不同?")

    assert plan.intent == "general_chat"


async def test_recent_three_product_stock_extreme_routes_to_comparison() -> None:
    plan = await DeterministicExclusiveModelGateway().plan(
        "这三件里哪一个库存最少?只看刚才这三件。"
    )

    assert plan.intent == "product_compare"


def test_product_stock_extreme_answer_names_rechecked_winner() -> None:
    answer = _product_stock_extreme_answer(
        "这三件里哪一个库存最少?",
        [
            {"name": "橡皮", "available_stock": 99},
            {"name": "铅笔", "available_stock": 98},
            {"name": "直尺", "available_stock": 99},
        ],
    )

    assert answer is not None
    assert "铅笔" in answer
    assert "98 件" in answer


async def test_purchased_product_status_question_routes_to_orders() -> None:
    question = "我在文具专卖店买的铅笔发货了吗?如果已经签收,还能不能评价?"
    plan = await DeterministicExclusiveModelGateway().plan(question)

    assert plan.intent == "order_lookup"
    assert _references_purchased_item(question) is True


async def test_logistics_and_after_sale_precheck_are_both_preserved() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "那笔8元的呢?只告诉我物流现状和能否申请售后。"
    )

    assert [task.intent for task in plan.tasks] == [
        "logistics_lookup",
        "refund_precheck",
    ]


def test_chinese_purchased_product_noun_matches_without_spaces() -> None:
    assert (
        _meaningful_product_reference(
            "绿杆2B书写铅笔考试专用",
            "我在文具专卖店买的铅笔发货了吗如果已经签收还能不能评价",
        )
        is True
    )
    assert (
        _meaningful_product_reference(
            "记录本写作本日记本",
            "我在文具专卖店买的铅笔发货了吗",
        )
        is False
    )


def test_related_order_status_answer_summarizes_cards_and_review_entry() -> None:
    answer = _render(
        ExclusiveAgentPlan("order_lookup"),
        {
            "items": [
                {
                    "status": {"order": "pending_shipment"},
                    "available_actions": ["apply_after_sale"],
                },
                {
                    "status": {"order": "completed"},
                    "available_actions": ["review"],
                },
            ]
        },
        "我买的铅笔发货了吗?签收后能不能评价?",
    )

    assert "待发货 1 笔、已完成 1 笔" in answer
    assert "1 笔当前可以评价" in answer


async def test_supervisor_keeps_catalog_constraints_in_one_agent_task() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "给我推荐3款适合考试、20元以内的文具，按价格从低到高"
    )

    assert len(plan.tasks) == 1
    assert plan.tasks[0].intent == "personalized_recommendation"
    assert "20元以内" in plan.tasks[0].objective


async def test_supervisor_merges_catalog_constraints_inside_account_request() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "先告诉我余额和默认收货地址,再推荐两件20元以内适合考试的文具,按价格从低到高。"
    )

    assert [task.intent for task in plan.tasks] == [
        "address_lookup",
        "wallet_lookup",
        "personalized_recommendation",
    ]
    assert "两件20元以内适合考试的文具" in plan.tasks[-1].objective
    assert "按价格从低到高" in plan.tasks[-1].objective


async def test_supervisor_does_not_drop_order_logistics_or_after_sale_tasks() -> None:
    gateway = DeterministicExclusiveModelGateway()
    for question in (
        "我有一笔待付款、一笔运输中和一笔售后中的订单，请分别用可点击卡片展示，"
        "并告诉我运输中的包裹到哪了、售后到哪一步",
        "请把我的待付款订单、运输中订单和售后中订单分别用卡片展示，"
        "再告诉我运输中的物流进度和售后处理进度",
    ):
        plan = await gateway.plan_tasks(question)
        assert [task.intent for task in plan.tasks] == [
            "order_lookup",
            "logistics_lookup",
            "refund_progress",
        ]
        assert [task.subtask_key for task in plan.tasks] == ["task_1", "task_2", "task_3"]
        assert plan.tasks[1].objective == "查询当前用户运输中订单的物流进度"


async def test_supervisor_keeps_policy_orders_and_wallet_in_compound_request() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "告诉我退款一般多久到账，再把我最近两笔订单和余额一起发来"
    )

    assert [task.intent for task in plan.tasks] == [
        "order_lookup",
        "wallet_lookup",
        "policy_qa",
    ]
    assert plan.tasks[-1].objective == "退款一般多久到账以及固定到账时效规则"


async def test_general_withdrawal_and_refund_destination_stays_policy_only() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "余额充值后可以提现吗?订单退款是退到余额还是原支付渠道?"
    )

    assert [task.intent for task in plan.tasks] == ["policy_qa"]


async def test_supervisor_keeps_colloquial_wallet_clause_with_cart() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "购务车里头都有啥玩意儿?顺便告诉我钱还剩多少"
    )

    assert [task.intent for task in plan.tasks] == ["cart_lookup", "wallet_lookup"]


async def test_amount_qualified_order_logistics_is_one_specialist_task() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "我那笔19.10元的订单，现在快递具体到哪了?"
    )

    assert [task.intent for task in plan.tasks] == ["logistics_lookup"]
    assert plan.tasks[0].objective.startswith("我那笔19.10元")


async def test_supervisor_does_not_execute_explicitly_rejected_other_user_clause() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "别人的订单我知道不能看\uff1b那就只告诉我自己的账户余额和默认收货地址"
    )

    assert [task.intent for task in plan.tasks] == ["address_lookup", "wallet_lookup"]


async def test_product_card_cart_add_is_one_reversible_write_task() -> None:
    gateway = DeterministicExclusiveModelGateway()
    plan = await gateway.plan("把第一个加到购物车，数量2")
    supervisor = await gateway.plan_tasks("把第一个加到购物车，数量2")

    assert plan.intent == "cart_add"
    assert [task.intent for task in supervisor.tasks] == ["cart_add"]
    assert _requested_cart_add_quantity("把第一个加到购物车，数量2") == 2
    assert _requested_cart_add_quantity("把第一个6支款加入购物车2件") == 2


async def test_search_then_cart_add_first_returns_ranked_choices() -> None:
    gateway = DeterministicExclusiveModelGateway()
    question = "帮我找15元以内的考试铅笔, 并把最便宜的加入购物车2件;如果候选不唯一先让我选"

    assert (await gateway.plan(question)).intent == "product_search"
    supervisor = await gateway.plan_tasks(question)
    assert [task.intent for task in supervisor.tasks] == ["product_search"]
    assert _extract_catalog_sort(question) == "price_asc"
    assert _extract_catalog_sort("想要价格不贵的考试文具") == "price_asc"
    assert _extract_catalog_sort("这次想看不便宜、做工更好的款式") == "price_desc"
    assert _requests_compound_advice("分析我现在最应该先关注什么") is True
    assert _is_chinese_trace_text("先核对订单，再给出建议") is True
    assert _is_chinese_trace_text("Prioritizing order status") is False

    rendered = _render(
        ExclusiveAgentPlan("product_search"),
        {"items": [{"name": "商品A"}, {"name": "商品B"}]},
        question,
    )
    assert "本次还没有修改购物车" in rendered
    assert "请回复第几件、具体款式和数量" in rendered


async def test_cart_update_and_remove_route_to_direct_cart_actions() -> None:
    gateway = DeterministicExclusiveModelGateway()

    assert (await gateway.plan("把刚加的铅笔数量改成3件")).intent == "cart_update"
    assert (await gateway.plan("把购物车里的铅笔删掉")).intent == "cart_remove"
    assert (await gateway.plan("把购物车里的裤子数量改回1件")).intent == "cart_update"
    assert _requested_cart_add_quantity("把购物车里的裤子数量改回1件") == 1
    compound = await gateway.plan_tasks("把购物车里的裤子数量改成2件，再告诉我账户余额")
    assert [task.intent for task in compound.tasks] == ["cart_update", "wallet_lookup"]


async def test_cart_hypothetical_never_routes_to_a_write_action() -> None:
    gateway = DeterministicExclusiveModelGateway()
    text = "如果把购物车里的裤子数量改成2件要多少钱?只试算"

    assert (await gateway.plan(text)).intent == "cart_lookup"
    assert [task.intent for task in (await gateway.plan_tasks(text)).tasks] == ["cart_lookup"]


def test_cart_hypothetical_read_guard_overrides_provider_checkout_plan() -> None:
    plan, source = _single_plan_with_non_overridable_guards(
        "如果把它改成3件,总价是多少钱?只试算,不要修改购物车。",
        provider_plan=ExclusiveAgentPlan("checkout_preview"),
        fallback_plan=ExclusiveAgentPlan("cart_lookup"),
    )

    assert plan.intent == "cart_lookup"
    assert plan.continuation_of_previous_turn is True
    assert source == "deterministic_read_only_guard"


async def test_recently_added_cart_follow_up_routes_to_cart_update() -> None:
    plan = await DeterministicExclusiveModelGateway().plan("把刚加入的商品数量改成3")

    assert plan.intent == "cart_update"


async def test_cart_add_verification_is_read_only_not_a_duplicate_add() -> None:
    gateway = DeterministicExclusiveModelGateway()
    question = "核对刚加入购物车的是不是8支款。"

    assert (await gateway.plan(question)).intent == "cart_lookup"
    assert [task.intent for task in (await gateway.plan_tasks(question)).tasks] == [
        "cart_lookup"
    ]
    answer = _cart_add_verification_text(
        {
            "groups": [
                {
                    "items": [
                        {
                            "product_name": "考试铅笔",
                            "sku_name": "8支",
                            "quantity": 1,
                        }
                    ]
                }
            ]
        },
        question,
    )
    assert answer is not None
    assert "本次只是核对，没有重复加入" in answer


async def test_cart_card_field_list_is_not_split_into_a_catalog_search() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "核对购物车，只告诉我商品、款式、数量和总额。"
    )

    assert [task.intent for task in plan.tasks] == ["cart_lookup"]


async def test_favorite_confirmation_is_not_split_into_a_catalog_search() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "再核对收藏列表，确认商品已经恢复。"
    )

    assert [task.intent for task in plan.tasks] == ["favorites_lookup"]


def test_wallet_cart_affordability_computes_remaining_balance() -> None:
    text = _wallet_cart_affordability_text(
        {
            "wallet_lookup": {"balance": {"minor_units": "97390", "currency": "CNY"}},
            "cart_lookup": {
                "selected_quantity": 1,
                "amount_summary": {
                    "selected_goods_amount": {
                        "minor_units": "11200",
                        "currency": "CNY",
                    }
                },
            },
        },
        "我的余额够不够买购物车里已选的全部商品?告诉我差额",
    )

    assert text is not None
    assert "够" in text
    assert "¥973.90" in text
    assert "¥112.00" in text
    assert "¥861.90" in text


def test_wallet_cart_affordability_computes_shortfall() -> None:
    text = _wallet_cart_affordability_text(
        {
            "wallet_lookup": {"balance": {"minor_units": "1000", "currency": "CNY"}},
            "cart_lookup": {
                "selected_quantity": 2,
                "amount_summary": {
                    "selected_goods_amount": {
                        "minor_units": "2500",
                        "currency": "CNY",
                    }
                },
            },
        },
        "余额够吗，购物车还差多少钱?",
    )

    assert text is not None
    assert "暂时不够" in text
    assert "还差 ¥15.00" in text


def test_wallet_checkout_affordability_uses_payable_amount() -> None:
    text = _wallet_cart_affordability_text(
        {
            "wallet_lookup": {
                "balance": {"minor_units": "97390", "currency": "CNY"}
            },
            "checkout_preview": {
                "amounts": {
                    "payable_amount": {"minor_units": "11200", "currency": "CNY"}
                }
            },
        },
        "按购物车生成结算预览，并告诉我余额够不够",
    )

    assert text is not None
    assert "本次结算应付 ¥112.00" in text
    assert "预计还剩 ¥861.90" in text


def test_combined_order_actions_are_reported_separately_not_as_an_intersection() -> None:
    answer = _render(
        ExclusiveAgentPlan("order_lookup"),
        {
            "items": [
                {"available_actions": ["confirm_receipt"]},
                {"available_actions": ["confirm_receipt"]},
            ],
            "eligibility_match_items": [
                {"available_actions": ["confirm_receipt"]},
                {"available_actions": ["confirm_receipt"]},
            ],
            "requested_eligibility_actions": ["cancel_order", "confirm_receipt"],
        },
        "哪些订单现在可以取消，哪些可以确认收货？",
    )

    assert "可取消 0 笔" in answer
    assert "可确认收货 2 笔" in answer


def test_catalog_count_does_not_override_relaxed_price_budget() -> None:
    from app.modules.agent_runtime.exclusive_tools import _catalog_search_constraints

    constraints = _catalog_search_constraints(
        None,
        "预算放宽到6元，其余考试文具条件不变，给我最多2件",
    )

    assert constraints.price_max == 600
    assert constraints.requested_limit == 2

    with_memory = _catalog_search_constraints(
        None,
        "预算放宽到6元，其他条件不变。已确认购物偏好：预算通常不超过30元",
    )
    assert with_memory.price_max == 600


def test_explicit_pencil_request_excludes_erasers_but_general_stationery_does_not() -> None:
    assert _matches_explicit_catalog_kind("绿杆2B考试铅笔", "找15元以内考试铅笔") is True
    assert _matches_explicit_catalog_kind("PILOT铅笔擦橡皮", "找15元以内考试铅笔") is False
    assert _matches_explicit_catalog_kind("PILOT铅笔擦橡皮", "找15元以内考试文具") is True
    assert _excluded_catalog_terms("不要铅笔，预算和排序保持不变") == ("铅笔",)
    assert _excluded_catalog_terms("适合考试、不是铅笔的文具，按价格排序") == ("铅笔",)
    assert _excluded_catalog_terms("排除蓝色和黑色，价格不变") == ("蓝色", "黑色")


def test_delivery_comparison_identifies_earliest_order_card() -> None:
    answer = _delivery_comparison_text(
        {
            "matched_order_ids": ["ord_late", "ord_early"],
            "items": [
                {
                    "order_id": "ord_late",
                    "delivery_estimate": {
                        "status": "available",
                        "min_at": "2026-09-16T02:00:00Z",
                        "max_at": "2026-09-17T02:00:00Z",
                    },
                },
                {
                    "order_id": "ord_early",
                    "delivery_estimate": {
                        "status": "available",
                        "min_at": "2026-09-14T02:00:00Z",
                        "max_at": "2026-09-15T02:00:00Z",
                    },
                },
            ],
        },
        "比较哪一单预计最早送达",
    )

    assert answer is not None
    assert "第 2 笔订单" in answer
    assert "预计最早送达" in answer


def test_delivery_comparison_distinguishes_signed_from_future_estimate() -> None:
    answer = _delivery_comparison_text(
        {
            "matched_order_ids": ["ord_future", "ord_signed"],
            "items": [
                {
                    "order_id": "ord_future",
                    "shipment_status": "in_transit",
                    "delivery_estimate": {
                        "status": "available",
                        "min_at": "2026-09-16T02:00:00Z",
                        "max_at": "2026-09-17T02:00:00Z",
                    },
                },
                {
                    "order_id": "ord_signed",
                    "shipment_status": "delivered",
                    "delivery_estimate": {
                        "status": "available",
                        "min_at": "2026-09-14T02:00:00Z",
                        "max_at": "2026-09-15T02:00:00Z",
                    },
                },
            ],
        },
        "比较哪一单预计最早送达",
    )

    assert answer is not None
    assert "第 2 笔订单的包裹已经签收" in answer
    assert "第 1 笔尚未签收" in answer
    assert "当前预计送达范围" in answer


def test_select_cart_item_uses_unique_name_or_explicit_ordinal() -> None:
    pencil = {
        "cart_item_id": "cit_pencil",
        "product_name": "绿杆2B书写铅笔考试专用",
        "sku_name": "6支",
    }
    pants = {
        "cart_item_id": "cit_pants",
        "product_name": "男士灰色长裤",
        "sku_name": "灰色S",
    }
    data = {"groups": [{"items": [pencil, pants]}]}

    assert _select_cart_item(data, "把刚加的铅笔数量改成3件") == pencil
    assert _select_cart_item(data, "把购物车第二件删掉") == pants
    assert _select_cart_item(data, "把购物车商品删掉") is None
    assert _select_cart_item(data, "把购物车里的铅笔删掉, 只删除铅笔, 保留裤子") == pencil


def test_requested_cart_sku_uses_the_explicit_variant_not_the_card_default() -> None:
    skus = [
        {"sku_id": "sku_6", "sku_name": "6支", "available_stock": 98},
        {"sku_id": "sku_8", "sku_name": "8支", "available_stock": 99},
        {"sku_id": "sku_10", "sku_name": "10支", "available_stock": 0},
    ]

    selected = _requested_product_sku({"items": [{"skus": skus}]}, "把10支款加入购物车")

    assert selected == skus[2]
    answer = _render(
        ExclusiveAgentPlan("cart_add"),
        {
            "cart_add_unavailable_variant": {
                "product_name": "考试铅笔",
                "sku_name": "10支",
            }
        },
        "把10支款加入购物车",
    )
    assert "当前可售库存为 0" in answer
    assert "本次没有加入购物车" in answer


def test_store_favorite_follow_up_recovers_the_last_action_target() -> None:
    assert (
        _store_name_from_favorite_update_message("已收藏店铺“文具专卖店”。更新后的收藏已放在下方。")
        == "文具专卖店"
    )
    assert (
        _store_name_from_favorite_update_message("已取消收藏店铺“男装专卖店”。")
        == "男装专卖店"
    )
    assert _targets_store_favorite_update("取消收藏文具专卖店") is True
    assert _targets_store_favorite_update("取消收藏刚才这个商品") is False


async def test_two_product_card_ordinals_route_to_comparison_without_keyword() -> None:
    plan = await DeterministicExclusiveModelGateway().plan(
        "第2个和第3个哪个更便宜?顺便看看各自库存"
    )

    assert plan.intent == "product_compare"


def test_profile_lookup_understands_current_account_fields() -> None:
    from app.modules.agent_runtime.exclusive_model_gateway import _requests_profile_lookup

    assert _requests_profile_lookup("告诉我当前账号的用户名、邮箱提示") is True
    assert _requests_profile_lookup("用户名和邮箱分别是什么") is True
    assert _requests_profile_lookup("怎么注册邮箱") is False
    assert _requests_address_mutation("把我的默认收货地址直接改成北京市朝阳区") is True


async def test_two_products_fit_question_routes_to_comparison() -> None:
    plan = await DeterministicExclusiveModelGateway().plan("这两件哪个更适合150斤的人?")

    assert plan.intent == "product_compare"


async def test_comparative_follow_up_routes_to_one_product() -> None:
    plan = await DeterministicExclusiveModelGateway().plan(
        "刚才对比里更便宜的那个有几个款式?哪些有货"
    )

    assert plan.intent == "product_search"


async def test_in_progress_refund_question_routes_to_progress() -> None:
    plan = await DeterministicExclusiveModelGateway().plan(
        "我现在有进行中的退款吗?如果有就给我可点击卡片"
    )

    assert plan.intent == "refund_progress"


async def test_refund_delay_and_timing_question_keeps_progress_and_policy() -> None:
    gateway = DeterministicExclusiveModelGateway()
    plan = await gateway.plan_tasks("为什么还没退款?平台一般多久到账?")

    assert [task.intent for task in plan.tasks] == ["policy_qa", "refund_progress"]


def test_comparative_follow_up_resolves_cheapest_card() -> None:
    cards: list[dict[str, object]] = [
        {"product_id": "prd_a", "price": {"minor_units": "871"}},
        {"product_id": "prd_b", "price": {"minor_units": "480"}},
    ]

    assert _comparative_product_card("更便宜的那个", cards) == cards[1]
    assert _comparative_product_card("那最便宜的具体有哪些款式", cards) == cards[1]


async def test_protected_transaction_action_keeps_safe_companion_read() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "帮我确认收货，再告诉我余额还有多少"
    )

    assert [task.intent for task in plan.tasks] == ["order_lookup", "wallet_lookup"]


async def test_favorites_and_saved_preferences_do_not_route_to_catalog_search() -> None:
    plan = await DeterministicExclusiveModelGateway().plan_tasks(
        "把我收藏的商品和你记住的购物偏好一起发给我"
    )

    assert [task.intent for task in plan.tasks] == ["favorites_lookup", "memory_lookup"]


def test_order_state_overview_uses_a_broader_card_window() -> None:
    assert _requests_order_state_overview("我有哪些订单?请按状态给我看看") is True
    assert _requests_order_state_overview("待付款、待发货、运输中订单都找出来") is True
    assert _requests_order_state_overview("看看我的最近订单") is False


def test_address_cards_keep_same_user_delivery_details_actionable() -> None:
    cards = _address_cards(
        {
            "items": [
                {
                    "address_id": "addr_01TEST",
                    "recipient_name": "张三",
                    "phone": "13800138000",
                    "province_code": "440000",
                    "city_code": "440100",
                    "district_code": "440106",
                    "address": "体育西路 1 号",
                    "is_default": True,
                }
            ]
        }
    )

    assert cards[0]["address_id"] == "addr_01TEST"
    assert cards[0]["phone"] == "13800138000"
    assert cards[0]["is_default"] is True


def test_empty_logistics_result_does_not_render_a_blank_card() -> None:
    assert (
        _exclusive_detail_cards(
            ExclusiveAgentPlan("logistics_lookup"),
            {"items": [], "order_id": "ord_01TEST"},
        )
        == []
    )


def test_empty_requested_order_state_is_not_reported_as_no_order_history() -> None:
    answer = _render(
        ExclusiveAgentPlan("order_lookup"),
        {"items": [], "requested_state_counts": {"待付款": 0}},
    )

    assert answer == "你当前没有待付款订单，所以没有对应卡片。"


def test_checkout_preview_is_explicitly_non_payment_and_actionable() -> None:
    data = {
        "checkout_id": "chk_01TEST",
        "store_groups": [
            {
                "store_name": "文具专卖店",
                "goods_amount": {"minor_units": "600", "currency": "CNY"},
                "freight_amount": {"minor_units": "0", "currency": "CNY"},
            }
        ],
        "amounts": {"payable_amount": {"minor_units": "600", "currency": "CNY"}},
        "blocking_issues": [],
    }

    answer = _render(ExclusiveAgentPlan("checkout_preview"), data)
    cards = _exclusive_detail_cards(ExclusiveAgentPlan("checkout_preview"), data)

    assert "没有创建订单，也没有付款" in answer
    assert "应付 ¥6.00" in str(cards)
    assert "/checkout/chk_01TEST" in str(cards)


def test_logistics_eta_question_reports_missing_estimate_honestly() -> None:
    answer = _render(
        ExclusiveAgentPlan("logistics_lookup"),
        {"items": [{"shipment_id": "shp_1", "delivery_estimate": None}]},
        "预计什么时候到?",
    )

    assert "没有承运商或配送模板给出的可靠预计送达时间" in answer


def test_delivery_estimate_is_localized_for_shopper_instead_of_raw_iso() -> None:
    rendered = _delivery_estimate_text(
        {
            "delivery_estimate": {
                "status": "available",
                "min_at": "2026-09-13T06:34:06.533051",
                "max_at": "2026-09-15T06:34:06.533051",
                "source": "template",
            }
        }
    )

    assert "9月13日 14:34" in rendered
    assert "T06:34" not in rendered


async def test_order_ordinal_with_eta_language_routes_to_logistics() -> None:
    plan = await DeterministicExclusiveModelGateway().plan("刚才运输中的第一笔预计什么时候到?")

    assert plan.intent == "logistics_lookup"


def test_cart_projection_understands_natural_quantity_change() -> None:
    projection = _cart_hypothetical_projection(
        "如果把购物车里的裤子改成2件要多少钱?只试算",
        {
            "groups": [
                {
                    "store_name": "男装专卖店",
                    "items": [
                        {
                            "product_name": "宽松裤子",
                            "sku_name": "灰色S",
                            "quantity": 1,
                            "is_selected": True,
                            "is_valid": True,
                            "current_price": {"minor_units": "11200", "currency": "CNY"},
                        }
                    ],
                }
            ],
            "amount_summary": {
                "selected_goods_amount": {"minor_units": "11200", "currency": "CNY"}
            },
        },
    )

    assert projection is not None
    assert projection["to_quantity"] == 2
    assert projection["projected_total_display"] == "¥224.00"

    natural_projection = _cart_hypothetical_projection(
        "如果这件买5个，购物车总额会是多少？只试算，不要修改",
        {
            "groups": [
                {
                    "store_name": "男装专卖店",
                    "items": [
                        {
                            "product_name": "宽松裤子",
                            "sku_name": "灰色S",
                            "quantity": 1,
                            "is_selected": True,
                            "is_valid": True,
                            "current_price": {"minor_units": "11200", "currency": "CNY"},
                        }
                    ],
                }
            ],
            "amount_summary": {
                "selected_goods_amount": {"minor_units": "11200", "currency": "CNY"}
            },
        },
    )

    assert natural_projection is not None
    assert natural_projection["to_quantity"] == 5
    assert natural_projection["projected_total_display"] == "¥560.00"


def test_catalog_color_constraint_is_explicit_and_preference_syntax_is_removed() -> None:
    constraints = _catalog_search_constraints(None, "我喜欢蓝色、20元以内的文具，推荐3款")

    assert _explicit_catalog_colors("我喜欢蓝色文具") == ("蓝色",)
    assert "我喜欢蓝色" not in constraints.keywords
    assert "蓝色" in constraints.keywords


def test_product_card_prefers_a_sku_matching_requested_color() -> None:
    selected = _preferred_sku_nos(
        {
            "items": [
                {
                    "product_id": "prd_01TEST",
                    "skus": [
                        {"sku_id": "sku_black", "sku_name": "10支黑色"},
                        {"sku_id": "sku_blue", "sku_name": "10支蓝色"},
                    ],
                }
            ]
        },
        "我喜欢蓝色文具",
    )

    assert selected == {"prd_01TEST": "sku_blue"}


def test_product_card_combines_dark_preference_with_required_weight() -> None:
    selected = _preferred_sku_nos(
        {
            "items": [
                {
                    "product_id": "prd_DRESS",
                    "skus": [
                        {"sku_id": "sku_s", "sku_name": "藏蓝色 S(90斤以下)"},
                        {"sku_id": "sku_l", "sku_name": "藏蓝色 L(130斤以下)"},
                    ],
                }
            ]
        },
        "找适合130斤的夏季女装，深色优先",
    )

    assert selected == {"prd_DRESS": "sku_l"}


def test_latest_order_language_is_detected_without_treating_any_order_question_as_latest() -> None:
    assert _requests_latest_order("请查我最近一笔订单的物流") is True
    assert _requests_latest_order("刚买的商品能不能退款") is True
    assert _requests_latest_order("最近那笔订单为什么还没发货") is True
    assert _requests_latest_order("这个订单能不能退款") is False


def test_pending_shipment_question_does_not_invent_a_ship_date() -> None:
    answer = _render(
        ExclusiveAgentPlan("order_lookup"),
        {"order_id": "ord_01TEST", "status": {"order": "pending_shipment"}},
        "最近那笔订单为什么还没发货?预计什么时候发",
    )

    assert "当前已付款、待商家发货" in answer
    assert "没有可核验的具体发货时间" in answer


def test_focused_order_after_sale_question_answers_yes_or_no() -> None:
    answer = _render(
        ExclusiveAgentPlan("order_lookup"),
        {"order_id": "ord_01TEST", "status": {"after_sale": "none"}},
        "刚才那笔订单现在有没有进行中的售后?",
    )

    assert "没有进行中的售后" in answer


def test_fresh_order_list_language_is_not_overridden_by_an_active_order() -> None:
    assert _requests_order_list("顺便查一下我最近买过什么，按订单卡片展示") is True
    assert _requests_order_list("我都买过什么订单?") is True
    assert _requests_order_list("刚才列表里的第一笔订单") is False
    assert _corrects_order_reference("不对，我想问第1笔") is True
    assert _requests_address_mutation("把默认收货地址改成测试路88号") is True
    assert _requests_order_list("把待发货和运输中的订单发来") is True


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


def test_sku_stock_extreme_answer_reports_a_tie_without_guessing() -> None:
    answer = _sku_stock_extreme_answer(
        "其中库存最少的是哪一款",
        {
            "skus": [
                {"sku_name": "小号", "available_stock": 99},
                {"sku_name": "大号", "available_stock": 99},
            ]
        },
    )

    assert answer is not None
    assert "库存相同" in answer
    assert "没有唯一" in answer


async def test_planner_keeps_sku_stock_extreme_as_product_continuation() -> None:
    plan = await DeterministicExclusiveModelGateway().plan("其中库存最少的是哪一款?")

    assert plan.intent == "product_search"
    assert plan.continuation_of_previous_turn is True


def test_focused_product_missing_variant_keeps_specific_context() -> None:
    answer = _render(
        ExclusiveAgentPlan("product_search"),
        {
            "items": [],
            "focused_product_name": "PILOT 百乐橡皮",
            "unavailable_variant_labels": ["蓝色"],
        },
        "更便宜的那个有没有蓝色款?",
    )

    assert "PILOT 百乐橡皮" in answer
    assert "没有找到蓝色款" in answer


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


def test_catalog_constraints_do_not_turn_budget_digits_into_unrelated_searches() -> None:
    constraints = _catalog_search_constraints(
        "帮我找500元以内的火星传送器，给我两件",
        "帮我找500元以内的火星传送器，给我两件",
    )

    assert constraints.keywords == ("火星传送器",)
    assert constraints.price_max == 50000
    assert all(
        candidate is not None and "火星传送器" in candidate
        for candidate in constraints.candidates
    )


def test_catalog_constraints_remove_model_presentation_language_from_tool_query() -> None:
    constraints = _catalog_search_constraints(
        "在本店范围内查找并推荐价格在20元以内的笔记本商品，结果需以可点击商品卡片形式展示。",
        "请推荐本店20元以内的笔记本，用可点击商品卡片展示。",
    )

    assert constraints.price_max == 2000
    assert constraints.keywords == ("笔记本",)
    assert constraints.candidates[0] == "笔记本"


def test_catalog_season_and_color_preferences_reject_contradictory_filler() -> None:
    assert _matches_requested_catalog_seasons("2026秋季白色阔腿裤", ("summer",)) is False
    assert _matches_requested_catalog_seasons("2026春秋长袖上衣", ("summer",)) is False
    assert _matches_requested_catalog_seasons("法式短袖衬衫", ("summer",)) is True
    assert "黑色" in _preferred_catalog_colors("夏季女装，深色优先")
    assert _requested_catalog_weight("适合150斤的人") == 150


def test_catalog_constraints_support_decimal_lower_and_upper_budget() -> None:
    constraints = _catalog_search_constraints(
        None,
        "想找至少 8.50 元、不超过 19.90 元的铅笔",
    )

    assert constraints.price_min == 850
    assert constraints.price_max == 1990
    assert "铅笔" in constraints.keywords


def test_catalog_constraints_understand_natural_budget_change() -> None:
    constraints = _catalog_search_constraints(
        None,
        "预算改成10元，其他条件不变。",
    )

    assert constraints.price_max == 1000

    relaxed = _catalog_search_constraints(
        None,
        "那放宽到15元，其他条件还是不变。",
    )
    assert relaxed.price_max == 1500


def test_catalog_constraints_discard_requested_result_count_before_searching() -> None:
    constraints = _catalog_search_constraints(None, "帮我找几件20元以内的文具")

    assert constraints.candidates[0] == "文具"
    assert "几件" not in constraints.keywords


def test_catalog_constraints_expand_exam_purpose_without_dropping_budget() -> None:
    constraints = _catalog_search_constraints(
        None, "推荐三件20元以内适合考试的文具，按价格从低到高"
    )

    assert constraints.price_max == 2000
    assert constraints.requested_limit == 3
    assert constraints.sort == "price_asc"
    assert {"铅笔", "橡皮", "直尺", "笔芯"}.issubset(set(constraints.candidates))


def test_catalog_constraints_support_user_visible_sort_language() -> None:
    assert _catalog_search_constraints(None, "最畅销的两件文具").sort == "sales"
    assert _catalog_search_constraints(None, "最新上架的衣服").sort == "newest"
    assert _catalog_search_constraints(None, "价格从高到低").sort == "price_desc"


def test_catalog_constraints_follow_the_last_explicit_subject_correction() -> None:
    constraints = _catalog_search_constraints(
        None,
        "不要文具，先找女装; 算了，改成男装，预算 200 元以内，按价格从低到高给我 3 个",
    )

    assert constraints.keywords == ("男装",)
    assert constraints.price_max == 20_000
    assert constraints.requested_limit == 3
    assert constraints.sort == "price_asc"


def test_catalog_follow_up_inherits_prior_budget_count_and_sort() -> None:
    query = catalog_query_with_inherited_constraints(
        "不对，我改要男装，预算和排序不变。",
        "找女装，预算 200 元以内，按价格从低到高给我 3 个",
    )
    constraints = _catalog_search_constraints(None, query)

    assert constraints.keywords == ("男装",)
    assert constraints.price_max == 20_000
    assert constraints.requested_limit == 3
    assert constraints.sort == "price_asc"


def test_catalog_follow_up_inherits_prior_color_when_only_budget_changes() -> None:
    query = catalog_query_with_inherited_constraints(
        "预算改成15元，其他条件不变。",
        "找蓝色考试文具，预算20元以内，推荐3款",
    )
    constraints = _catalog_search_constraints(None, query)

    assert constraints.price_max == 1500
    assert "蓝色" in query


def test_catalog_follow_up_can_relax_season_and_keep_audience_weight_budget_color() -> None:
    query = catalog_query_with_inherited_constraints(
        "季节不限，其余条件不变。",
        "找适合150斤的夏季女装，200元以内，深色优先。",
    )
    constraints = _catalog_search_constraints(None, query)

    assert constraints.price_max == 20_000
    assert "女装" in query
    assert "150斤" in query
    assert "深色" in query
    assert "夏季" not in query


def test_catalog_semantic_candidates_include_explicit_audience_category() -> None:
    expansions = _semantic_catalog_expansions("找适合130斤的夏季女装，深色优先")

    assert "女装" in expansions


async def test_catalog_constraint_correction_remains_an_executable_agent_intent() -> None:
    plan = await DeterministicExclusiveModelGateway().plan("季节不限，其余条件不变。")

    assert plan.intent == "product_search"
    assert plan.continuation_of_previous_turn is True


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
    assert len(_filter_order_rows(typed_rows, typed_items, "哪些订单可以评价和申请售后")) == 2


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


def test_product_compare_keeps_price_and_exam_recommendation_in_one_answer() -> None:
    rendered = _render(
        ExclusiveAgentPlan("product_compare"),
        {
            "items": [
                {
                    "name": "考试2B铅笔",
                    "description": "答题书写",
                    "price": {"min_amount": 600, "currency": "CNY"},
                },
                {
                    "name": "金属美工刀",
                    "description": "手帐切割",
                    "price": {"min_amount": 900, "currency": "CNY"},
                },
            ]
        },
        "重点说价格和考试适用场景，然后推荐一件",
    )

    assert "低 ¥3.00" in rendered
    assert "更贴近考试使用场景" in rendered


def test_refund_progress_card_includes_application_reason() -> None:
    cards = _exclusive_detail_cards(
        ExclusiveAgentPlan("refund_progress"),
        {
            "items": [
                {
                    "refund_id": "ref_test",
                    "refund_status": "merchant_review",
                    "reason_code": "NOT_AS_DESCRIBED",
                    "reason_detail": "颜色与页面展示不一致",
                    "requested_amount": {"minor_units": "1280", "currency": "CNY"},
                }
            ]
        },
    )

    assert "申请原因" in str(cards)
    assert "颜色与页面展示不一致" in str(cards)


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
                    "excerpt": "当前使用模拟物流，每个节点由授权操作显式记录。",
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
            {
                "document_id": "doc_recharge",
                "title": "[系统] 支付、余额与模拟充值规则",
                "excerpt": "同一个充值请求必须使用幂等键，重复提交不能重复到账。",
            },
        ],
    }

    rendered = _render(ExclusiveAgentPlan("policy_qa"), data, "平台退款一般多久到账?")
    cards = _exclusive_detail_cards(ExclusiveAgentPlan("policy_qa"), data)

    assert "退款到账" in rendered
    assert not rendered.startswith("不承诺。")
    assert len(cards) == 1
    assert len(cast(list[dict[str, object]], cards[0]["rows"])) == 1
    assert "售后、退款与客服规则" in str(cards)
    assert "物流规则" not in str(cards)


def test_policy_fallback_answers_withdrawal_and_refund_destination_separately() -> None:
    data = {
        "knowledge_sources": [
            {
                "title": "[系统] 支付、余额与模拟充值规则",
                "excerpt": (
                    "当前商城余额不支持提现、转账或兑换为真实资金。"
                    "使用商城余额支付的订单，退款处理成功后原路退回用户的商城余额。"
                ),
            },
            {
                "title": "[系统] 售后、退款与客服规则",
                "excerpt": "售后资格由订单状态、支付状态和可退款数量共同决定。",
            },
        ]
    }
    rendered = _render(
        ExclusiveAgentPlan("policy_qa"),
        data,
        "余额充值后可以提现吗?订单退款退到哪里?",
    )
    cards = _exclusive_detail_cards(
        ExclusiveAgentPlan("policy_qa"),
        {
            **data,
            "policy_retrieval_query": "余额提现 订单退款原路退回余额",
        },
    )

    assert "不支持提现" in rendered
    assert "原路退回用户的商城余额" in rendered
    assert "支付、余额与模拟充值规则" in str(cards)
    assert "售后、退款与客服规则" not in str(cards)


def test_policy_fallback_answers_false_promise_before_showing_evidence() -> None:
    rendered = _render(
        ExclusiveAgentPlan("policy_qa"),
        {
            "knowledge_sources": [
                {
                    "title": "[系统] 售后、退款与客服规则",
                    "excerpt": "当前项目使用模拟支付与退款,不承诺真实支付渠道的固定到账天数。",
                }
            ]
        },
        "平台承诺所有退款一小时到账吗?",
    )

    assert rendered.startswith("不承诺。根据当前已发布平台规则")
    assert "退款，不承诺" in rendered


def test_policy_fallback_prioritizes_explicit_logistics_update_rule() -> None:
    data = {
        "policy_query": "模拟物流是不是每5秒自动更新?",
        "knowledge_sources": [
            {
                "document_id": "doc_logistics",
                "title": "[系统] 物流与签收规则",
                "excerpt": (
                    "当前开发环境使用 Ecom 速运模拟物流，不代表已经接入真实快递公司。"
                    "模拟物流不会再根据支付后的经过时间自动推进，也不承诺固定几秒内更新。"
                    "发货、揽收、运输、派送、签收均由具备权限的人员显式记录。"
                ),
            }
        ],
    }

    rendered = _render(
        ExclusiveAgentPlan("policy_qa"),
        data,
        "模拟物流是不是每5秒自动更新?",
    )
    cards = _exclusive_detail_cards(ExclusiveAgentPlan("policy_qa"), data)

    assert "不会再根据支付后的经过时间自动推进" in rendered
    assert "不承诺固定几秒内更新" in rendered
    assert "不会再根据支付后的经过时间自动推进" in str(cards)


def test_policy_fallback_names_authorized_logistics_operators() -> None:
    data = {
        "policy_query": "物流为什么没有每5秒自动变一次？现在是谁更新物流节点？",
        "policy_retrieval_query": (
            "物流为什么没有每5秒自动变一次？现在是谁更新物流节点？ "
            "店铺人员 平台管理员 经确认的 Agent 显式记录物流节点"
        ),
        "knowledge_sources": [
            {
                "title": "[系统] 物流与签收规则",
                "excerpt": (
                    "模拟物流不会根据支付后的经过时间自动推进，也不承诺固定几秒内更新。"
                    "发货、揽收、运输、派送、签收均由具备权限的店铺人员、平台管理员"
                    "或经确认的 Agent 操作显式记录。"
                ),
            }
        ],
    }

    rendered = _render(ExclusiveAgentPlan("policy_qa"), data, str(data["policy_query"]))

    assert "店铺人员、平台管理员或经确认的 Agent" in rendered


def test_refund_selection_explains_stale_non_refundable_context() -> None:
    rendered = _render(
        ExclusiveAgentPlan("refund_eligibility"),
        {
            "selection_required": "refund",
            "refund_context_error": "ORDER_NOT_REFUNDABLE",
            "items": [{"order_id": "ord_ELIGIBLE"}],
        },
        "帮我退款",
    )

    assert "刚才关联的订单当前不能申请退款" in rendered
    assert "其他可申请售后的订单" in rendered
    assert "请选择" in rendered


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
    assert "包裹最新状态为“运输中”" in rendered
    assert "当前位置是 海淀区" in rendered
    assert "正在派送中" in rendered
    assert "FAKE****1234" in str(cards)
    assert "运输中" in str(cards)
    assert "海淀区" in str(cards)
    assert "in_transit" not in str(cards)
    assert _compact_tracking_no("*****************************ZVA9") == "尾号 ZVA9"


def test_single_selected_logistics_card_does_not_renumber_it_as_first_order() -> None:
    cards = _exclusive_detail_cards(
        ExclusiveAgentPlan("logistics_lookup"),
        {
            "matched_order_ids": ["ord_SECOND"],
            "items": [
                {
                    "order_id": "ord_SECOND",
                    "carrier_name": "模拟快递",
                    "tracking_no_masked": "FX64****77C8",
                    "shipment_status": "in_transit",
                    "last_track": {"description": "包裹运输中", "location_text": "运输途中"},
                }
            ],
        },
    )

    assert "第 1 笔订单" not in str(cards)
    assert cards[0]["action"]["resource_id"] == "ord_SECOND"


def test_logistics_reason_answer_explains_missing_later_node_without_claiming_loss() -> None:
    answer = _render(
        ExclusiveAgentPlan("logistics_lookup"),
        {
            "items": [
                {
                    "shipment_status": "in_transit",
                    "last_track": {"description": "包裹运输中", "location_text": "运输途中"},
                }
            ]
        },
        "为什么还在运输中？",
    )

    assert "没有查询到更晚的物流节点" in answer
    assert "不能证明包裹已经丢失" in answer


def test_logistics_reason_answer_accepts_why_synonym_from_specialist_objective() -> None:
    answer = _render(
        ExclusiveAgentPlan("logistics_lookup"),
        {
            "items": [
                {
                    "shipment_status": "in_transit",
                    "last_track": {"description": "包裹运输中", "location_text": "运输途中"},
                }
            ]
        },
        "解释第二笔订单为何仍在运输中",
    )

    assert "没有查询到更晚的物流节点" in answer


def test_policy_source_compaction_prioritizes_logistics_and_bounds_payload() -> None:
    sources = [
        {
            "document_id": "doc_after_sale",
            "title": "[系统] 售后、退款与客服规则",
            "excerpt": "商品质量问题可以申请售后。" * 200,
            "score": 0.99,
        },
        {
            "document_id": "doc_logistics",
            "title": "[系统] 物流与签收规则",
            "excerpt": (
                "包裹没有更晚的物流节点不等于已经丢件；"
                "应先核对承运商、运单号和最后更新时间。"
            ),
            "score": 0.70,
        },
        *[
            {
                "document_id": f"doc_{index}",
                "title": f"其他规则 {index}",
                "excerpt": "与当前问题无关的长内容" * 300,
                "score": 0.80,
            }
            for index in range(8)
        ],
    ]

    compacted = _compact_policy_sources(
        sources,
        "订单长时间未收到，平台规则怎么处理",
        limit=4,
    )

    assert compacted[0]["document_id"] == "doc_logistics"
    assert len(compacted) == 1
    assert all(source["document_id"] != "doc_after_sale" for source in compacted)
    assert len(str(compacted).encode()) < 32_768


def test_product_comparison_refuses_weight_fit_beyond_all_public_skus() -> None:
    answer = _render(
        ExclusiveAgentPlan("product_compare"),
        {
            "items": [
                {"name": "夏季衬衫", "skus": [{"sku_name": "L 130斤以下"}]},
                {"name": "夏季短裤", "skus": [{"sku_name": "XL 140斤以下"}]},
            ]
        },
        "这两件哪个适合150斤?",
    )

    assert "最高只覆盖到 140 斤以下" in answer
    assert "不建议直接下单" in answer


def test_stock_extreme_does_not_misrepresent_low_stock_as_recommendation() -> None:
    answer = _product_stock_extreme_answer(
        "这几个库存最少的是哪个?为什么推荐它?",
        [
            {"name": "铅笔", "available_stock": 20},
            {"name": "直尺", "available_stock": 10},
            {"name": "橡皮", "available_stock": 30},
        ],
    )

    assert answer is not None
    assert "直尺" in answer
    assert "不代表它一定更值得买" in answer


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


def test_multi_agent_platform_diagnosis_does_not_collapse_into_unfiltered_orders() -> None:
    rendered = _render_multi_agent(
        {
            "specialists": {
                "orders": {
                    "specialist": "governance_orders",
                    "data": {
                        "query_mode": "list",
                        "applied_filters": {
                            "customer_name": None,
                            "store_name": None,
                            "statuses": [],
                        },
                        "recent_orders": [{"order_id": "ord_1"}],
                        "order_status_counts": {"pending_shipment": 1},
                    },
                },
                "runtime": {
                    "specialist": "observability",
                    "data": {
                        "stale_pending_outbox_events": 0,
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
    assert "只读诊断" in rendered
    assert "已找到符合条件的 1 笔订单" not in rendered
