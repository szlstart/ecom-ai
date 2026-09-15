from datetime import timedelta
from types import SimpleNamespace
from typing import cast, get_args

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utc_now
from app.modules.agent_runtime.operations_approval import (
    OPERATIONS_ACTION_TYPES,
    OperationsActionConflict,
    _admin_address_fields,
    _admin_product_profile_changes,
    _admin_product_review_decision,
    _admin_store_create_fields,
    _admin_user_create_fields,
    _admin_user_profile_changes,
    _audit_permission_code,
    _evaluation_run_payload,
    _match_support_order,
    _match_support_product,
    _mentions_username,
    _merchant_policy_command,
    _merchant_policy_fields,
    _merchant_policy_type,
    _merchant_profile_changes,
    _operations_admin_access,
    _operations_session_client_types,
    _parse_admin_wallet_adjustment,
    _refund_decision_reason,
    _refund_more_info_requirements,
    _replace_detail_section_blocks,
    _requested_image_description,
    _requested_location,
    _requested_product_detail_section_change,
    _requested_product_draft,
    _requested_product_faq_change,
    _requested_product_fulfillment_changes,
    _requested_product_sku_create,
    _requested_product_sku_update,
    _requested_shipment_event,
    _requested_store_email_update,
    _requests_admin_cart_clear,
    _requests_admin_evaluation_run,
    _requests_admin_order_cancel,
    _requests_admin_product_profile,
    _requests_admin_product_review,
    _requests_admin_shipment_progress,
    _requests_admin_store_create,
    _requests_admin_store_delete,
    _requests_admin_store_profile,
    _requests_admin_user_asset_write,
    _requests_admin_user_create,
    _requests_admin_user_delete,
    _requests_admin_user_password_reset,
    _requests_admin_user_profile,
    _requests_admin_wallet_adjustment,
    _requests_dead_letter_replay,
    _requests_merchant_policy_write,
    _requests_product_delete,
    _requests_product_detail_section_write,
    _requests_product_faq_write,
    _requests_product_fulfillment_change,
    _requests_product_image_description_write,
    _requests_product_sku_disable,
    _requests_product_submit,
    _requests_refund_decision,
    _requests_review_reply,
    _requests_shipment_create,
    _requests_support_action,
    _requests_support_claim,
    _requests_support_reply,
    _requests_support_resolve,
    _review_reply_content,
    _support_attachment_kind,
    _support_reply_content,
    _target_integer,
    _target_price_minor,
    _uses_recent_reference,
    appears_to_request_operations_write,
)
from app.modules.agent_runtime.operations_context import TrustedOperationsContext
from app.modules.agent_runtime.schemas import AgentApprovalActionType
from app.modules.catalog.models import Product
from app.modules.knowledge.contracts import CONFIRMATION_REQUIRED_TOOLS
from app.modules.orders.models import Order
from app.modules.rbac.models import Permission
from app.modules.rbac.repository import RbacRepository


def test_merchant_write_detection_distinguishes_query_from_command() -> None:
    assert appears_to_request_operations_write("把绿杆铅笔的 8 支款式下架", "merchant")
    assert appears_to_request_operations_write("帮我把 8 支款式库存设置为 50 件", "merchant")
    assert appears_to_request_operations_write(
        "把 8 支款式名称改为考试 8 支装，价格改为 12.90 元，库存设为 40 件",
        "merchant",
    )
    assert not appears_to_request_operations_write("哪些商品已经上架?", "merchant")
    assert not appears_to_request_operations_write("如何下架商品?", "merchant")
    assert appears_to_request_operations_write("请把订单 ord_01ABC 安排发货", "merchant")
    assert appears_to_request_operations_write("请把刚才卡片里的待发货订单安排发货", "merchant")
    assert _requests_shipment_create("请把刚才卡片里的待发货订单安排发货")
    assert appears_to_request_operations_write("更新包裹 shp_01ABC 的物流为已揽收", "merchant")
    assert appears_to_request_operations_write("把刚才那个包裹的物流更新为已揽收", "merchant")
    assert appears_to_request_operations_write("回复评价 rev_01ABC: 感谢您的支持", "merchant")
    assert not _requests_review_reply("同时检查本店商品、库存、订单履约、待回复评价和顾客人工队列")
    assert not appears_to_request_operations_write(
        "同时检查本店商品、库存、订单履约、待回复评价和顾客人工队列", "merchant"
    )
    assert not appears_to_request_operations_write("还有多少待发货订单?", "merchant")
    assert appears_to_request_operations_write(
        "把店铺名称改为文具严选，并把店铺简介改为专注考试文具", "merchant"
    )
    assert appears_to_request_operations_write(
        "把商家邮箱改为 owner@example.com", "merchant"
    )
    assert appears_to_request_operations_write(
        "新建售后政策，标题: 七天退换说明，内容: 商品完好时可从售后入口申请退换",
        "merchant",
    )
    assert appears_to_request_operations_write(
        "把文具专卖店的绿杆铅笔第 2 张详情图片说明改为考试涂卡使用方法",
        "admin",
    )
    assert appears_to_request_operations_write(
        "给文具专卖店的绿杆铅笔新增常见问题，问题: 是否包邮，回答: 包邮",
        "admin",
    )
    assert appears_to_request_operations_write(
        "给用户 tulubi 新增收货地址，收货人: 张三，联系电话: 13800138000，"
        "地区: 广东省/深圳市/南山区，详细地址: 科技园 1 号",
        "admin",
    )
    assert appears_to_request_operations_write(
        "编辑用户 tulubi 的第 1 个收货地址，收货人改为李四", "admin"
    )
    assert appears_to_request_operations_write(
        "把用户 tulubi 的第 1 个收货地址的详细地址改为科技园 2 号", "admin"
    )
    assert appears_to_request_operations_write("清空用户 tulubi 的购物车", "admin")
    assert _requests_admin_cart_clear("清空用户acceptance_user的购物车")
    assert _requests_admin_cart_clear("把用户acceptance_user购物车里的商品全部移除")
    assert not _requests_admin_cart_clear("删除用户acceptance_user的第一个收货地址")
    assert appears_to_request_operations_write("启动AI评估并要求显著提升", "admin")
    assert _requests_admin_evaluation_run("运行AI评估")
    assert not _requests_admin_evaluation_run("查看最近AI评估结果")
    assert appears_to_request_operations_write(
        "取消订单 ord_01ABC，原因：顾客确认不再购买", "admin"
    )
    assert _requests_admin_order_cancel("关闭未付款订单 ord_01ABC")
    assert not _requests_admin_order_cancel("如何取消订单?")
    assert _requests_product_image_description_write(
        "把文具专卖店的绿杆铅笔第 2 张详情图片说明改为考试涂卡使用方法"
    )
    assert not _requests_product_image_description_write("查看绿杆铅笔有哪些图片说明")
    assert appears_to_request_operations_write("请删除商品 绿杆铅笔", "merchant")
    assert _requests_product_delete("请把绿杆铅笔这个商品永久删除")
    assert not _requests_product_delete("如何删除商品?")
    assert appears_to_request_operations_write("请把绿杆铅笔商品提交审核", "merchant")
    assert _requests_product_submit("提交审核这个商品")
    assert not _requests_product_submit("如何提交审核?")
    assert appears_to_request_operations_write(
        "把绿杆铅笔的发货地改为广东省，发货时效改为24到48小时", "merchant"
    )
    assert appears_to_request_operations_write(
        "创建商品草稿，商品名称: 考试中性笔，款式: 黑色，价格: 9.90 元，"
        "库存: 50，发货地: 广东省，24 到 48 小时发货",
        "merchant",
    )
    assert appears_to_request_operations_write(
        "给商品考试中性笔新增款式，款式名称: 蓝色，价格: 10.50 元，库存: 20",
        "merchant",
    )
    assert appears_to_request_operations_write(
        "把考试中性笔的商品描述改为适合日常书写和考试备用", "merchant"
    )
    sku_fields, sku_error = _requested_product_sku_create(
        "给商品考试中性笔新增款式，款式名称: 蓝色，价格: 10.50 元，库存: 20"
    )
    assert sku_error is None
    assert sku_fields == {"sku_name": "蓝色", "price_minor": 1050, "stock_quantity": 20}
    missing_sku_fields, missing_sku_error = _requested_product_sku_create(
        "给商品考试中性笔新增款式，款式名称: 蓝色"
    )
    assert missing_sku_fields == {}
    assert missing_sku_error is not None
    assert "售价" in missing_sku_error and "库存" in missing_sku_error
    assert _requests_product_sku_disable("移除商品考试中性笔的款式蓝色")
    assert not _requests_product_sku_disable("怎么移除商品款式")
    assert appears_to_request_operations_write(
        "给商品考试中性笔新增常见问题，问题: 是否包邮，回答: 本商品包邮", "merchant"
    )
    faq_change, faq_error = _requested_product_faq_change(
        "给商品考试中性笔新增常见问题，问题: 是否包邮，回答: 本商品包邮"
    )
    assert faq_error is None
    assert faq_change == {"mode": "upsert", "question": "是否包邮", "answer": "本商品包邮"}
    delete_faq, delete_faq_error = _requested_product_faq_change(
        "删除商品考试中性笔的常见问题，问题: 是否包邮"
    )
    assert delete_faq_error is None
    assert delete_faq == {"mode": "delete", "question": "是否包邮"}
    assert _requests_product_faq_write("移除常见问题“是否包邮”")
    assert not _requests_product_faq_write("查看商品的常见问题")
    assert appears_to_request_operations_write(
        "给商品考试中性笔新增详情段落，标题: 适用场景，内容: 日常书写和考试备用",
        "merchant",
    )
    detail_change, detail_error = _requested_product_detail_section_change(
        "给商品考试中性笔新增详情段落，标题: 适用场景，内容: 日常书写和考试备用"
    )
    assert detail_error is None
    assert detail_change == {
        "mode": "upsert",
        "title": "适用场景",
        "content": "日常书写和考试备用",
    }
    delete_detail, delete_detail_error = _requested_product_detail_section_change(
        "删除商品考试中性笔的详情段落，标题: 适用场景"
    )
    assert delete_detail_error is None
    assert delete_detail == {"mode": "delete", "title": "适用场景"}
    assert _requests_product_detail_section_write("修改详情章节，标题: 适用场景，内容: 考试")
    assert not _requests_product_detail_section_write("查看详情章节")
    assert _requests_product_fulfillment_change(
        "把绿杆铅笔的发货地改为广东省，发货时效改为24到48小时"
    )
    assert not _requests_shipment_create("把绿杆铅笔的发货地改为广东省，发货时效改为24到48小时")
    assert not _requests_shipment_create("把绿杆铅笔的购买须知改为验收期间请保持包装完整")
    assert appears_to_request_operations_write("同意退款 rfd_01ABC", "merchant")
    assert _requests_refund_decision("拒绝售后，原因: 商品已经超过售后期限")
    assert _refund_decision_reason("拒绝售后，原因: 商品已经超过售后期限") == (
        "商品已经超过售后期限"
    )
    assert not _requests_refund_decision("如何处理退款?")
    assert _requests_refund_decision("要求该售后补充材料：商品问题照片和包装照片")
    assert _refund_more_info_requirements(
        "要求该售后补充材料：商品问题照片和包装照片"
    ) == "商品问题照片和包装照片"
    assert _refund_more_info_requirements("要求该售后补充材料") is None
    assert _requests_support_claim("接入人工工单 tic_01ABC")
    assert _requests_support_reply("回复顾客 tulubi，回复内容: 您好，我来处理")
    assert _requests_support_resolve("结束人工服务 tulubi")
    assert _requests_support_action("给顾客发消息，回复内容: 已为您核对")
    assert _requests_support_action("给顾客 tulubi 发送最近订单卡片")
    assert _requests_support_action("向用户 tulubi 发送商品卡片 三端验收笔记本")
    assert _support_reply_content("回复顾客 tulubi，回复内容: 已为您核对") == "已为您核对"
    assert appears_to_request_operations_write("回复顾客 tulubi，回复内容: 已为您核对", "merchant")


def test_approval_response_schema_covers_every_executable_action() -> None:
    assert set(get_args(AgentApprovalActionType)) == set(OPERATIONS_ACTION_TYPES) | {
        "refund_submit",
        "cart_clear",
    }


def test_admin_user_create_and_password_reset_require_explicit_safe_fields() -> None:
    message = "创建一个普通用户，用户名: browser_buyer，恢复邮箱: buyer@example.com"
    assert _requests_admin_user_create(message)
    assert _admin_user_create_fields(message) == (
        {
            "username": "browser_buyer",
            "email": "buyer@example.com",
        },
        None,
    )
    assert not _requests_admin_user_create("给用户 buyer 新增收货地址")
    assert _requests_admin_user_password_reset("要求用户 browser_buyer 重置密码")
    assert _requests_admin_user_password_reset("重置用户 browser_buyer 的密码")


def test_admin_store_create_requires_distinct_store_identity_and_recovery_email() -> None:
    message = (
        "创建店铺，店铺名称: AI 验收文具店，商家用户名: browser_store，"
        "商家邮箱: store@example.com，店铺简介: 仅用于隔离验收"
    )
    assert _requests_admin_store_create(message)
    assert _admin_store_create_fields(message) == (
        {
            "store_name": "AI 验收文具店",
            "merchant_username": "browser_store",
            "merchant_email": "store@example.com",
            "description": "仅用于隔离验收",
        },
        None,
    )
    assert not _requests_admin_store_create("如何创建店铺？")


def test_evaluation_run_payload_ignores_confirmation_card_metadata() -> None:
    request = _evaluation_run_payload(
        {
            "dataset_id": "ecom-ai-release-holdout",
            "dataset_version": "2026.09.01-v3",
            "baseline_type": "prompt",
            "baseline_version": "ecom-safe-router-v1",
            "candidate_type": "prompt",
            "candidate_version": "ecom-safe-router-v3",
            "require_significant_gain": True,
            "title": "确认启动 AI 发布准入评估",
            "summary": "仅用于确认卡展示",
            "target_label": "prompt · ecom-safe-router-v3",
            "changes": [{"label": "候选策略", "value": "v3"}],
            "tool_code": "governance.ai.evaluations.run.commit",
        }
    )

    assert request.candidate_version == "ecom-safe-router-v3"
    assert request.require_significant_gain is True


def test_admin_address_fields_resolve_human_region_names_without_trusting_codes() -> None:
    fields, error = _admin_address_fields(
        "给用户 tulubi 新增收货地址，收货人: 张三，联系电话: 13800138000，"
        "地区: 广东省/深圳市/南山区，详细地址: 科技园 1 号，设为默认地址",
        require_all=True,
    )
    assert error is None
    assert fields == {
        "recipient_name": "张三",
        "phone": "13800138000",
        "province_code": "440000",
        "city_code": "440300",
        "district_code": "440305",
        "region_label": "广东省 深圳市 南山区",
        "address": "科技园 1 号",
        "is_default": True,
    }

    patch, error = _admin_address_fields(
        "编辑用户 tulubi 的第 1 个收货地址，收货人改为李四，地区改为北京市/北京市/朝阳区",
        require_all=False,
    )
    assert error is None
    assert patch["recipient_name"] == "李四"
    assert patch["province_code"] == "110000"
    assert patch["city_code"] == "110100"
    assert patch["district_code"] == "110105"

    _, error = _admin_address_fields(
        "给用户 tulubi 新增收货地址，收货人: 张三，地区: 广东省/北京市/朝阳区",
        require_all=True,
    )
    assert error == "没有在上级地区内找到城市“北京市”。"


def test_support_attachment_selection_is_typed_and_requires_a_unique_resource() -> None:
    order_old = SimpleNamespace(order_no="ord_OLD")
    order_new = SimpleNamespace(order_no="ord_NEW")
    product_pen = SimpleNamespace(product_no="prd_PEN", product_name="考试铅笔")
    product_book = SimpleNamespace(product_no="prd_BOOK", product_name="验收笔记本")
    typed_order_old = cast(Order, order_old)
    typed_order_new = cast(Order, order_new)
    typed_product_pen = cast(Product, product_pen)
    typed_product_book = cast(Product, product_book)
    orders = [typed_order_new, typed_order_old]
    products = [typed_product_pen, typed_product_book]

    assert _support_attachment_kind("给顾客发送订单卡片") == "order"
    assert _support_attachment_kind("给顾客发送商品卡片") == "product"
    assert _support_attachment_kind("同时发送商品卡片和订单卡片") is None
    assert _match_support_order(orders, "发送订单 ord_OLD 的订单卡片") is typed_order_old
    assert _match_support_order(orders, "发送最近订单卡片") is typed_order_new
    assert _match_support_order(orders, "发送订单卡片") is None
    assert _match_support_product(products, "发送验收笔记本的商品卡片") is typed_product_book
    assert _match_support_product(products, "发送商品卡片") is None


def test_admin_write_detection_keeps_read_only_questions_read_only() -> None:
    assert appears_to_request_operations_write("把用户 tulubi 冻结", "admin")
    assert appears_to_request_operations_write("请强制下线用户 tulubi", "admin")
    assert not appears_to_request_operations_write("有哪些冻结用户?", "admin")
    assert not appears_to_request_operations_write("怎么暂停营业?", "admin")
    assert appears_to_request_operations_write("给用户 tulubi 余额增加 10.50 元", "admin")
    assert not appears_to_request_operations_write("查看用户 tulubi 的余额", "admin")
    assert appears_to_request_operations_write("请永久删除商品 绿杆铅笔", "admin")
    assert appears_to_request_operations_write("批准退款 rfd_01ABC", "admin")
    assert appears_to_request_operations_write("回复用户 tulubi，回复内容: 已为您处理", "admin")
    assert appears_to_request_operations_write("把用户 tulubi 的用户名改为 tulubi_new", "admin")
    assert appears_to_request_operations_write("删除用户 tulubi", "admin")
    assert appears_to_request_operations_write(
        "把用户 tulubi 购物车里的考试铅笔数量改为 2", "admin"
    )
    assert appears_to_request_operations_write("删除用户 tulubi 的第 2 个收货地址", "admin")
    assert appears_to_request_operations_write("取消用户 tulubi 收藏的商品考试铅笔", "admin")
    assert appears_to_request_operations_write(
        "把店铺 文具专卖店 的店铺简介改为专注考试文具", "admin"
    )
    assert appears_to_request_operations_write("注销店铺 文具专卖店", "admin")
    assert appears_to_request_operations_write(
        "把商品 绿杆铅笔 的商品描述改为考试书写专用", "admin"
    )
    assert appears_to_request_operations_write("审核通过商品 绿杆铅笔", "admin")
    assert appears_to_request_operations_write("重放死信 dlq_01ABC", "admin")
    assert appears_to_request_operations_write("把包裹 shp_01ABC 的物流更新为已揽收", "admin")
    assert _requests_admin_shipment_progress("把包裹 shp_01ABC 的物流更新为已揽收")
    assert not _requests_admin_shipment_progress("查看包裹 shp_01ABC 的物流轨迹")
    assert _requests_dead_letter_replay("请重放死信 dlq_01ABC，原因: 已修复消费者缺陷")
    assert not _requests_dead_letter_replay("如何重放死信?")
    assert appears_to_request_operations_write("发布 Agent agt_01ABC 的 v2 并发起审批", "admin")
    assert appears_to_request_operations_write("发布 Skill skl_01ABC 的版本 3", "admin")


def test_operation_targets_parse_exact_amounts() -> None:
    assert _target_integer("库存设置为 99 件", "库存") == 99
    assert _target_price_minor("把价格改为 9.90 元") == 990
    assert _target_price_minor("售价调整到 ¥12") == 1200


def test_product_fulfillment_change_parses_region_window_and_notice() -> None:
    changes, error = _requested_product_fulfillment_changes(
        "把绿杆铅笔的发货地改为广东省，发货时效改为24到48小时，购买须知改为拆封后请妥善保管"
    )
    assert error is None
    assert changes == {
        "origin_region_code": "440000",
        "dispatch_min_hours": 24,
        "dispatch_max_hours": 48,
        "purchase_notice": "拆封后请妥善保管",
    }
    changes, error = _requested_product_fulfillment_changes(
        "请把绿杆铅笔设置为72小时内发货并清空购买须知"
    )
    assert error is None
    assert changes == {"dispatch_max_hours": 72, "purchase_notice": None}
    changes, error = _requested_product_fulfillment_changes("把绿杆铅笔发货地改为火星")
    assert changes == {}
    assert error is not None


def test_detail_section_update_preserves_images_and_unrelated_sections() -> None:
    blocks: list[dict[str, object]] = [
        {"type": "heading", "level": 2, "text": "适用场景"},
        {"type": "paragraph", "text": "旧内容"},
        {"type": "image", "file_id": "file_01ARZ3NDEKTSV4RRFFQ69G5FAV", "alt": "详情图"},
        {"type": "heading", "level": 2, "text": "注意事项"},
        {"type": "paragraph", "text": "请妥善保管"},
    ]

    updated = _replace_detail_section_blocks(
        blocks,
        title="适用场景",
        content="考试和日常书写",
        delete=False,
    )
    assert updated[1] == {"type": "paragraph", "text": "考试和日常书写"}
    assert updated[2] == blocks[2]
    assert updated[3:] == blocks[3:]

    deleted = _replace_detail_section_blocks(
        updated,
        title="适用场景",
        content=None,
        delete=True,
    )
    assert deleted[0] == blocks[2]
    assert deleted[1:] == blocks[3:]


def test_product_draft_parser_requires_all_structured_business_fields() -> None:
    fields, error = _requested_product_draft(
        "创建商品草稿，商品名称: 考试中性笔，款式: 黑色 0.5mm，"
        "价格: 9.90 元，库存: 50，发货地: 广东省，24 到 48 小时发货"
    )
    assert error is None
    assert fields == {
        "product_name": "考试中性笔",
        "sku_name": "黑色 0.5mm",
        "price_minor": 990,
        "stock_quantity": 50,
        "origin_region_code": "440000",
        "dispatch_min_hours": 24,
        "dispatch_max_hours": 48,
    }

    fields, error = _requested_product_draft("创建商品草稿，商品名称: 考试中性笔")
    assert fields == {}
    assert error is not None
    assert "首个款式名称" in error
    assert "发货时效" in error
    assert _requested_product_draft("如何创建商品草稿?") == ({}, None)


def test_merchant_policy_commands_are_agent_routed_and_structured() -> None:
    value = "新建售后政策，标题: 七天退换说明，内容: 商品完好时支持七天退换"
    assert _requests_merchant_policy_write(value)
    assert _merchant_policy_command(value) == "create"
    assert _merchant_policy_type(value) == "after_sale"
    assert _merchant_policy_fields(value) == (
        "七天退换说明",
        "商品完好时支持七天退换",
    )
    assert _merchant_policy_command("发布店铺政策 pol_01ABC") == "publish"
    assert _merchant_policy_command("撤回发货政策 pol_01ABC") == "withdraw"
    assert _merchant_policy_command("修改客服政策 pol_01ABC，内容: 工作日两小时内响应") == (
        "update"
    )
    assert not _requests_merchant_policy_write("如何新建店铺政策?")


def test_short_username_does_not_match_arbitrary_text() -> None:
    assert _mentions_username("tulubi", "请冻结用户 tulubi")
    assert not _mentions_username("a", "请查看 all 用户")
    assert _mentions_username("a", "请冻结用户“a”")


def test_admin_agent_actions_use_the_real_business_permission_in_audit() -> None:
    assert _audit_permission_code("admin_user_status") == "users:manage"
    assert _audit_permission_code("admin_user_force_logout") == "users:sessions_revoke"
    assert _audit_permission_code("admin_user_create") == "users:manage"
    assert (
        _audit_permission_code("admin_user_password_reset_requirement")
        == "users:force_password_reset"
    )
    assert _audit_permission_code("admin_store_status") == "stores:manage"
    assert _audit_permission_code("admin_store_create") == "stores:manage"
    assert _audit_permission_code("admin_product_status") == "products:publish"
    assert _audit_permission_code("admin_product_delete") == "products:update"
    assert _audit_permission_code("admin_refund_decision") == "refunds:review"
    assert _audit_permission_code("admin_refund_more_info") == "refunds:review"
    assert _audit_permission_code("admin_user_wallet_adjust") == "users:manage"
    assert _audit_permission_code("admin_shipment_progress") == "shipments:create"
    assert _audit_permission_code("admin_order_cancel") == "orders:cancel"
    assert _audit_permission_code("admin_user_cart_item_update") == "users:manage"
    assert _audit_permission_code("admin_user_address_delete") == "users:manage"


def test_operations_access_accepts_every_real_management_login_session_kind() -> None:
    assert _operations_session_client_types("merchant") == ("merchant",)
    assert _operations_session_client_types("admin") == ("admin", "admin_password")


def test_all_operations_confirmation_tools_are_registered() -> None:
    assert {
        "store_ops.catalog.delete.commit",
        "store_ops.catalog.submit_review.commit",
        "store_ops.catalog.update_image_description.commit",
        "store_ops.catalog.fulfillment.update.commit",
        "store_ops.catalog.save_draft.commit",
        "store_ops.catalog.skus.create.commit",
        "store_ops.catalog.skus.disable.commit",
        "store_ops.catalog.update.commit",
        "store_ops.catalog.faqs.upsert.commit",
        "store_ops.catalog.faqs.delete.commit",
        "store_ops.policy.manage.commit",
        "store_ops.after_sale.decide.commit",
        "store_ops.support.claim.commit",
        "store_ops.conversations.send_message.commit",
        "store_ops.support.resolve.commit",
        "governance.catalog.delete.commit",
        "governance.catalog.update.commit",
        "governance.catalog.review.commit",
        "governance.trade.shipments.progress.commit",
        "governance.trade.orders.cancel.commit",
        "governance.users.update_profile.commit",
        "governance.users.create.commit",
        "governance.users.require_password_reset.commit",
        "governance.users.delete.commit",
        "governance.users.addresses.delete.commit",
        "governance.users.addresses.set_default.commit",
        "governance.users.addresses.create.commit",
        "governance.users.addresses.update.commit",
        "governance.users.cart.update_quantity.commit",
        "governance.users.cart.remove_item.commit",
        "governance.users.cart.clear.commit",
        "governance.users.favorites.remove_product.commit",
        "governance.users.favorites.remove_store.commit",
        "governance.stores.update.commit",
        "governance.stores.create.commit",
        "governance.stores.delete.commit",
        "governance.after_sale.decide.commit",
        "governance.support.claim.commit",
        "governance.support.send_message.commit",
        "governance.support.resolve.commit",
        "governance.ai.agents.publish_request.commit",
        "governance.ai.skills.publish_request.commit",
        "governance.ai.tools.publish_request.commit",
        "governance.ai.evaluations.run.commit",
        "governance.catalog.faqs.upsert.commit",
        "governance.catalog.faqs.delete.commit",
        "governance.catalog.skus.create.commit",
        "governance.catalog.skus.disable.commit",
        "store_ops.catalog.detail_sections.upsert.commit",
        "store_ops.catalog.detail_sections.delete.commit",
        "governance.catalog.detail_sections.upsert.commit",
        "governance.catalog.detail_sections.delete.commit",
        "observability.dead_letters.replay_request.commit",
    } <= CONFIRMATION_REQUIRED_TOOLS


def test_admin_wallet_adjustment_requires_one_direction_and_one_amount() -> None:
    assert _requests_admin_wallet_adjustment("给用户 tulubi 余额增加 10.50 元")
    assert _parse_admin_wallet_adjustment("给用户 tulubi 余额增加 10.50 元") == (
        "credit",
        1050,
        None,
    )
    assert _parse_admin_wallet_adjustment("从 tulubi 余额扣减 2 元") == (
        "debit",
        200,
        None,
    )
    assert _parse_admin_wallet_adjustment("把余额增加 1 元再扣减 2 元")[2] is not None


def test_admin_user_asset_writes_require_explicit_mutation_language() -> None:
    assert _requests_admin_user_asset_write("把用户 tulubi 购物车里的考试铅笔数量改为 2")
    assert _requests_admin_user_asset_write("删除用户 tulubi 的第 2 个收货地址")
    assert _requests_admin_user_asset_write("把用户 tulubi 的第 2 个收货地址设为默认")
    assert _requests_admin_user_asset_write("取消用户 tulubi 收藏的商品考试铅笔")
    assert _requests_admin_user_asset_write("取消用户 tulubi 关注的店铺文具专卖店")
    assert not _requests_admin_user_asset_write("查看用户 tulubi 的购物车")
    assert not _requests_admin_user_asset_write("用户 tulubi 收藏了哪些商品")


def test_merchant_fulfillment_and_review_arguments_are_explicit() -> None:
    assert _requested_shipment_event("更新物流为派送中") == "out_for_delivery"
    assert _requested_shipment_event("这个订单什么时候到?") is None
    assert _requested_location("更新为运输中，当前位置: 杭州市余杭区") == "杭州市余杭区"
    assert _review_reply_content("回复评价: 感谢您的认可，我们会继续努力") == (
        "感谢您的认可，我们会继续努力"
    )
    assert _review_reply_content("有哪些评价需要回复?") is None
    assert _uses_recent_reference("把刚才那个包裹更新为已揽收")
    assert not _uses_recent_reference("把包裹 shp_01ABC 更新为已揽收")


def test_merchant_profile_changes_parse_name_description_and_clear() -> None:
    changes, error = _merchant_profile_changes(
        "把店铺名称改为文具严选，并把店铺简介改为专注考试文具"
    )
    assert error is None
    assert changes == {"store_name": "文具严选", "description": "专注考试文具"}

    restored, error = _merchant_profile_changes("把店铺简介改回欢迎来到我们的店铺。")
    assert error is None
    assert restored == {"description": "欢迎来到我们的店铺。"}

    cleared, error = _merchant_profile_changes("清空店铺简介")
    assert error is None
    assert cleared == {"description": ""}

    changes, error = _merchant_profile_changes("把店铺名称改为文")
    assert changes == {}
    assert error == "店铺名称需为 2 至 128 个字符。"


def test_merchant_image_description_change_is_explicit_and_bounded() -> None:
    assert _requested_image_description(
        "把绿杆铅笔第 2 张详情图片说明改为 HB 铅芯，适合考试书写"
    ) == ((2, "HB 铅芯，适合考试书写"), None)
    assert _requested_image_description("清空第 1 张详情图片说明") == ((1, ""), None)
    assert _requested_image_description("修改商品图片说明")[1] is not None
    assert _requested_image_description("介绍商品详情图片") == (None, None)


def test_admin_profile_delete_and_review_commands_have_typed_arguments() -> None:
    assert _requests_admin_user_profile("把用户 tulubi 的用户名改为 tulubi_new")
    assert _admin_user_profile_changes(
        "把用户 tulubi 的用户名改为 tulubi_new，并把用户邮箱改为 new@example.com"
    ) == ({"username": "tulubi_new", "email": "new@example.com"}, None)
    assert _requests_admin_user_delete("注销用户 tulubi")
    assert not _requests_admin_user_delete("如何删除用户?")
    assert _requests_admin_store_profile("把文具专卖店的店铺简介改为考试文具严选")
    assert _requests_admin_store_profile(
        "把文具专卖店的商家邮箱改为 owner@example.com"
    )
    assert _requests_admin_store_delete("删除店铺 文具专卖店")
    assert not _requests_admin_store_delete("删除店铺规则是什么?")
    assert _requests_admin_product_profile("把绿杆铅笔的商品描述改为考试书写专用")
    assert _admin_product_profile_changes("把绿杆铅笔的商品描述改为考试书写专用") == (
        {"description": "考试书写专用"},
        None,
    )
    assert _requests_admin_product_review("审核通过商品 绿杆铅笔")
    assert _admin_product_review_decision("审核通过商品 绿杆铅笔") == (
        "approve",
        "符合平台商品审核规则",
        None,
    )
    assert _admin_product_review_decision("驳回商品 绿杆铅笔")[2] is not None
    assert _admin_product_review_decision("驳回商品 绿杆铅笔，原因: 商品详情包含违禁内容") == (
        "reject",
        "商品详情包含违禁内容",
        None,
    )


def test_store_recovery_email_change_is_explicit_and_normalized() -> None:
    assert _requested_store_email_update("把商家邮箱改为 Owner@Example.COM") == (
        "owner@example.com",
        None,
    )
    assert _requested_store_email_update("把店铺邮箱设置为 invalid") == (
        None,
        "新的商家恢复邮箱格式不正确，请输入完整邮箱。",
    )
    assert _requested_store_email_update("查看商家邮箱") == (None, None)


def test_existing_sku_update_can_change_one_or_multiple_fields_atomically() -> None:
    assert _requested_product_sku_update("把蓝色款式名称改为深海蓝") == (
        {"sku_name": "深海蓝"},
        None,
    )
    assert _requested_product_sku_update(
        "把蓝色款式名称改为深海蓝，价格改为 12.90 元，库存设为 40 件"
    ) == (
        {"sku_name": "深海蓝", "price_minor": 1290, "stock_quantity": 40},
        None,
    )
    assert _requested_product_sku_update("把蓝色价格改为 9.90 元，库存设为 20 件") == (
        {"price_minor": 990, "stock_quantity": 20},
        None,
    )
    assert _requested_product_sku_update("把蓝色价格改为 9.90 元") == ({}, None)


@pytest.mark.asyncio
async def test_operations_service_access_uses_real_user_permission_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    permission = SimpleNamespace(permission_code="refunds:review")
    granted_store = SimpleNamespace(scope_type="store", scope_id=7)

    async def permissions_for_user(
        _repository: RbacRepository, user_id: int, _now: object
    ) -> list[tuple[object, object, object]]:
        assert user_id == 9
        return [(permission, granted_store, SimpleNamespace())]

    monkeypatch.setattr(RbacRepository, "permissions_for_user", permissions_for_user)

    class FakeSession:
        async def scalar(self, _statement: object) -> object:
            return SimpleNamespace(
                session_no="ses_TEST",
                expires_at=utc_now() + timedelta(hours=1),
                authenticated_at=utc_now(),
            )

    context = cast(
        TrustedOperationsContext,
        SimpleNamespace(
            user=SimpleNamespace(id=9, user_no="usr_TEST", permission_version=1),
            store=SimpleNamespace(id=7),
            audience="merchant",
        ),
    )

    access = await _operations_admin_access(
        cast(AsyncSession, FakeSession()), context, "refunds:review"
    )

    assert access.permission is cast(Permission, permission)
    assert access.scopes == (("store", 7),)


@pytest.mark.asyncio
async def test_operations_service_access_rejects_ungranted_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def permissions_for_user(
        _repository: RbacRepository, _user_id: int, _now: object
    ) -> list[tuple[object, object, object]]:
        return []

    monkeypatch.setattr(RbacRepository, "permissions_for_user", permissions_for_user)
    context = cast(
        TrustedOperationsContext,
        SimpleNamespace(
            user=SimpleNamespace(id=9, user_no="usr_TEST", permission_version=1),
            store=SimpleNamespace(id=7),
            audience="merchant",
        ),
    )

    class FakeSession:
        async def scalar(self, _statement: object) -> object:
            return SimpleNamespace(
                session_no="ses_TEST",
                expires_at=utc_now() + timedelta(hours=1),
                authenticated_at=utc_now(),
            )

    with pytest.raises(OperationsActionConflict) as exc_info:
        await _operations_admin_access(
            cast(AsyncSession, FakeSession()),
            context,
            "refunds:review",
        )

    assert exc_info.value.code == "AGENT_ACTION_AUTH_CONTEXT_UNAVAILABLE"
