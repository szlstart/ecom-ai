from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthContext
from app.core.china_regions import ChinaRegionResolutionError, region_label, resolve_china_region
from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import (
    SecurityService,
    TokenClaims,
    canonical_request_hash,
    normalize_target,
    normalize_username,
    utc_now,
)
from app.modules.after_sale.models import RefundApplication, RefundEvent
from app.modules.after_sale.schemas import AdminRefundDecisionRequest
from app.modules.after_sale.service import AfterSaleService
from app.modules.agent_runtime.models import (
    AgentDefinition,
    AgentToolAction,
    AgentToolApproval,
    AgentVersion,
)
from app.modules.cart.models import Cart, CartItem
from app.modules.cart.schemas import CartItemPatchRequest
from app.modules.cart.service import CartService
from app.modules.catalog.models import (
    Category,
    Product,
    ProductContentVersion,
    ProductFaq,
    ProductFaqVersion,
    ProductFavorite,
    ProductFulfillmentProfile,
    ProductImage,
    ProductSku,
    ProductStatusLog,
)
from app.modules.catalog.product_admin_schemas import (
    AdminContentVersionCreateRequest,
    AdminFaqReplaceItem,
    AdminFaqReplaceRequest,
    AdminProductCommandRequest,
    AdminProductFulfillmentRequest,
    AdminProductModerationRequest,
    AdminProductUpdateRequest,
    AdminSkuStatusRequest,
)
from app.modules.catalog.product_admin_service import ProductAdminService
from app.modules.catalog.service import CatalogService
from app.modules.evaluation.schemas import EvaluationRunCreate
from app.modules.evaluation.service import (
    BASELINE_TYPE,
    BASELINE_VERSION,
    CANDIDATE_TYPE,
    CANDIDATE_VERSION,
    DATASET_ID,
    DATASET_VERSION,
    EvaluationService,
)
from app.modules.events.schemas import DeadLetterReplayRequest
from app.modules.events.service import DeadLetterService
from app.modules.files.models import FileObject
from app.modules.finance.account_deletion import AccountDeletionService
from app.modules.finance.models import UserWallet, WalletTransaction
from app.modules.identity.models import (
    AuthSession,
    User,
    UserAddress,
    UserCredential,
    UserStatusRecord,
)
from app.modules.identity.schemas import AddressPatch, AddressWrite
from app.modules.identity.service import IdentityService
from app.modules.inventory.models import Inventory, InventoryLog
from app.modules.knowledge.document_service import KnowledgeDocumentService
from app.modules.knowledge.models import (
    KnowledgeDocument,
    SkillDefinition,
    SkillVersion,
    ToolDefinition,
    ToolVersion,
)
from app.modules.knowledge.publication_service import AiPublicationService
from app.modules.logistics.domain import SHIPMENT_TRANSITIONS
from app.modules.logistics.models import Shipment
from app.modules.logistics.schemas import (
    AdminShipmentCreateItem,
    AdminShipmentCreateRequest,
    AdminShipmentSimulationEventRequest,
)
from app.modules.logistics.service import LogisticsService
from app.modules.messaging.models import Conversation, HumanServiceTicket, Message
from app.modules.messaging.sequence import lock_conversation_for_append
from app.modules.messaging.support_schemas import SupportMessageRequest, SupportResolveRequest
from app.modules.messaging.support_service import SupportService
from app.modules.orders.models import Order, OrderItem
from app.modules.orders.schemas import AdminOrderCancellationRequest
from app.modules.orders.service import OrderService
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.models import AdminOperationLog
from app.modules.rbac.repository import RbacRepository
from app.modules.rbac.schemas import AdminUserCreateRequest, AdminUserUpdateRequest
from app.modules.rbac.service import RbacService
from app.modules.reviews.models import Review, ReviewReply
from app.modules.reviews.schemas import AdminReviewReplyRequest
from app.modules.reviews.service import ReviewService
from app.modules.stores.admin_schemas import (
    AdminPolicyCommandRequest,
    AdminStoreCreateRequest,
    AdminStoreDeleteRequest,
    AdminStorePolicyCreateRequest,
    AdminStorePolicyUpdateRequest,
    AdminStoreUpdateRequest,
)
from app.modules.stores.admin_service import AdminStoreService
from app.modules.stores.models import ShippingTemplate, Store, StoreFollow, StoreServicePolicy
from app.modules.stores.service import StoreService
from app.modules.system.models import DeadLetterEvent, OutboxEvent

if TYPE_CHECKING:
    from app.modules.agent_runtime.operations_context import TrustedOperationsContext


OPERATIONS_ACTION_TYPES = frozenset(
    {
        "merchant_store_status",
        "merchant_store_profile",
        "merchant_store_email_update",
        "merchant_store_logo_update",
        "merchant_inventory_set",
        "merchant_price_set",
        "merchant_product_status",
        "merchant_product_delete",
        "merchant_product_submit",
        "merchant_product_image_description",
        "merchant_product_fulfillment",
        "merchant_product_draft_create",
        "merchant_product_profile",
        "merchant_product_sku_create",
        "merchant_product_sku_update",
        "merchant_product_sku_disable",
        "merchant_product_sku_image_replace",
        "merchant_product_faq_upsert",
        "merchant_product_faq_delete",
        "merchant_product_detail_section_upsert",
        "merchant_product_detail_section_delete",
        "merchant_store_policy_manage",
        "merchant_refund_decision",
        "merchant_refund_more_info",
        "merchant_support_claim",
        "merchant_support_reply",
        "merchant_support_resolve",
        "merchant_shipment_create",
        "merchant_shipment_progress",
        "merchant_review_reply",
        "admin_user_status",
        "admin_user_force_logout",
        "admin_user_create",
        "admin_user_password_reset_requirement",
        "admin_user_wallet_adjust",
        "admin_user_profile",
        "admin_user_avatar_update",
        "admin_user_delete",
        "admin_user_address_create",
        "admin_user_address_update",
        "admin_user_address_delete",
        "admin_user_address_set_default",
        "admin_user_cart_item_update",
        "admin_user_cart_item_delete",
        "admin_user_cart_clear",
        "admin_user_favorite_product_remove",
        "admin_user_favorite_store_remove",
        "admin_store_status",
        "admin_store_create",
        "admin_store_profile",
        "admin_store_merchant_email_update",
        "admin_store_logo_update",
        "admin_store_delete",
        "admin_product_status",
        "admin_product_delete",
        "admin_product_profile",
        "admin_product_image_description",
        "admin_product_faq_upsert",
        "admin_product_faq_delete",
        "admin_product_sku_create",
        "admin_product_sku_update",
        "admin_product_sku_disable",
        "admin_product_sku_image_replace",
        "admin_product_detail_section_upsert",
        "admin_product_detail_section_delete",
        "admin_product_review",
        "admin_order_cancel",
        "admin_shipment_progress",
        "admin_refund_decision",
        "admin_refund_more_info",
        "admin_support_claim",
        "admin_support_reply",
        "admin_support_resolve",
        "admin_dead_letter_replay_request",
        "admin_knowledge_document_publish",
        "admin_knowledge_document_withdraw",
        "admin_ai_agent_prompt_draft_create",
        "admin_ai_agent_publish_request",
        "admin_ai_skill_publish_request",
        "admin_ai_tool_publish_request",
        "admin_ai_evaluation_run",
    }
)


@dataclass(frozen=True)
class PreparedOperationsAction:
    action_type: str
    title: str
    summary: str
    target_label: str
    payload: dict[str, object]
    resource_versions: dict[str, object]
    changes: tuple[dict[str, str], ...]
    tool_code: str


def appears_to_request_operations_write(value: str, audience: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何", "怎么", "流程")):
        return False
    if any(
        marker in compact for marker in ("哪些", "有多少", "查询", "查看", "列出", "统计")
    ) and not any(marker in compact for marker in ("把", "将", "帮我", "替我")):
        return False
    if audience == "merchant":
        return (
            any(
                marker in compact
                for marker in (
                    "暂停营业",
                    "恢复营业",
                    "库存改",
                    "库存设",
                    "库存调整",
                    "库存补到",
                    "改价",
                    "价格改",
                    "价格设",
                    "下架商品",
                    "商品下架",
                    "上架商品",
                    "商品上架",
                    "删除商品",
                    "永久删除商品",
                    "提交商品审核",
                    "提交审核",
                    "发布商品",
                    "同意退款",
                    "批准退款",
                    "拒绝退款",
                    "驳回售后",
                    "接入人工工单",
                    "领取人工工单",
                    "回复顾客",
                    "给顾客发消息",
                    "结束人工服务",
                    "订单发货",
                    "安排发货",
                    "更新物流",
                    "物流更新",
                    "推进物流",
                    "店铺名称改",
                    "店铺名称设",
                    "店名改",
                    "店名设",
                    "店铺简介改",
                    "店铺简介设",
                    "清空店铺简介",
                    "商家邮箱改",
                    "商家邮箱设",
                    "恢复邮箱改",
                    "恢复邮箱设",
                    "店铺邮箱改",
                    "店铺邮箱设",
                    "详情图片说明改",
                    "图片说明改",
                    "发货地改",
                    "发货地设",
                    "发货时效改",
                    "发货时效设",
                    "购买须知改",
                    "商品名称改",
                    "商品名称设",
                    "商品描述改",
                    "商品描述设",
                    "清空商品描述",
                    "创建商品草稿",
                    "新增商品草稿",
                    "新增款式",
                    "添加款式",
                    "创建款式",
                    "款式名称改",
                    "款式名称设",
                    "删除款式",
                    "移除款式",
                    "停用款式",
                    "新增常见问题",
                    "添加常见问题",
                    "创建常见问题",
                    "修改常见问题",
                    "删除常见问题",
                    "移除常见问题",
                    "购买须知设",
                    "清空购买须知",
                    "新建店铺政策",
                    "创建店铺政策",
                    "修改店铺政策",
                    "发布店铺政策",
                    "撤回店铺政策",
                )
            )
            or _requests_product_status_change(value)
            or _requests_product_delete(value)
            or _requests_product_submit(value)
            or _requests_refund_decision(value)
            or _requests_support_action(value)
            or _requests_merchant_fulfillment_write(value)
            or _requests_product_fulfillment_change(value)
            or bool(_requested_product_sku_update(value)[0])
            or _requests_product_sku_disable(value)
            or _requests_product_faq_write(value)
            or _requests_product_detail_section_write(value)
            or _requests_admin_product_profile(value)
            or _requests_merchant_policy_write(value)
        )
    return (
        any(
            marker in compact
            for marker in (
                "冻结用户",
                "冻结账号",
                "解冻用户",
                "解冻账号",
                "恢复账号",
                "强制下线",
                "创建用户",
                "新增用户",
                "重置用户密码",
                "重置账号密码",
                "余额增加",
                "余额充值",
                "余额扣减",
                "账户充值",
                "暂停营业",
                "恢复营业",
                "下架商品",
                "商品下架",
                "上架商品",
                "商品上架",
                "删除商品",
                "永久删除商品",
                "同意退款",
                "批准退款",
                "拒绝退款",
                "驳回售后",
                "接入人工工单",
                "领取人工工单",
                "回复用户",
                "回复商家",
                "给用户发消息",
                "给商家发消息",
                "结束人工服务",
                "重放死信",
                "死信重放",
                "重新投递死信",
                "修改用户资料",
                "用户名称改",
                "用户名改",
                "用户邮箱改",
                "邮箱改为",
                "删除用户",
                "注销用户",
                "删除收货地址",
                "新增收货地址",
                "添加收货地址",
                "创建收货地址",
                "修改收货地址",
                "编辑收货地址",
                "设为默认地址",
                "设成默认地址",
                "购物车数量改",
                "购物车数量设",
                "删除购物车商品",
                "移除购物车商品",
                "取消收藏",
                "修改店铺资料",
                "店铺名称改",
                "店名改",
                "店铺简介改",
                "商家邮箱改",
                "商家邮箱设",
                "恢复邮箱改",
                "恢复邮箱设",
                "店铺邮箱改",
                "店铺邮箱设",
                "删除店铺",
                "注销店铺",
                "创建店铺",
                "新增店铺",
                "商品名称改",
                "商品描述改",
                "新增款式",
                "添加款式",
                "创建款式",
                "款式名称改",
                "款式名称设",
                "删除款式",
                "移除款式",
                "停用款式",
                "审核通过商品",
                "批准商品",
                "驳回商品",
                "要求商品修改",
                "更新物流",
                "物流更新",
                "推进物流",
                "更新包裹",
                "物流改为",
                "取消订单",
                "关闭未付款订单",
                "发布知识文档",
                "重建知识索引",
                "重建索引",
                "撤回知识文档",
                "启动ai评估",
                "运行ai评估",
                "发起ai评估",
                "执行ai评估",
            )
        )
        or _requests_product_status_change(value)
        or _requests_product_delete(value)
        or _requests_refund_decision(value)
        or _requests_support_action(value)
        or _requests_admin_user_change(value)
        or _requests_admin_user_create(value)
        or _requests_admin_user_password_reset(value)
        or _requests_admin_wallet_adjustment(value)
        or _requests_admin_user_profile(value)
        or _requests_admin_user_delete(value)
        or _requests_admin_user_asset_write(value)
        or _requests_admin_store_profile(value)
        or _requests_admin_store_create(value)
        or _requests_admin_store_delete(value)
        or _requests_admin_product_profile(value)
        or _requests_product_image_description_write(value)
        or _requests_product_faq_write(value)
        or bool(_requested_product_sku_create(value)[0])
        or bool(_requested_product_sku_update(value)[0])
        or _requests_product_sku_disable(value)
        or _requests_product_detail_section_write(value)
        or _requests_admin_product_review(value)
        or _requests_admin_order_cancel(value)
        or _requests_admin_shipment_progress(value)
        or _requests_dead_letter_replay(value)
        or _requests_admin_knowledge_write(value)
        or _requests_admin_agent_prompt_draft(value)
        or _requests_admin_ai_publication(value)
        or _requests_admin_evaluation_run(value)
    )


async def prepare_operations_action(
    session: AsyncSession,
    context: TrustedOperationsContext,
    value: str,
) -> tuple[PreparedOperationsAction | None, str | None]:
    if context.trigger.message_type == "agent_asset":
        return await _prepare_agent_asset_action(session, context)
    if not appears_to_request_operations_write(value, context.audience):
        return None, None
    if context.audience == "merchant":
        return await _prepare_merchant_action(session, context, value)
    return await _prepare_admin_action(session, context, value)


async def _prepare_agent_asset_action(
    session: AsyncSession,
    context: TrustedOperationsContext,
) -> tuple[PreparedOperationsAction | None, str | None]:
    """Build a confirmation from a server-resolved AI asset message.

    The upload itself is not authorization to bind a file.  Store ownership,
    product/SKU relationships, file purpose and scan status are re-read here,
    then re-read once more while holding locks during execution.
    """

    payload = context.trigger.content_payload
    if not isinstance(payload, dict):
        return None, "图片操作缺少可信的结构化信息，请重新选择图片。"
    purpose = str(payload.get("purpose") or "")
    file_no = str(payload.get("file_id") or "")
    if purpose == "user_avatar":
        if context.audience != "admin":
            return None, "只有平台 AI 管家可以修改用户头像。"
        target_user_no = str(payload.get("user_id") or "")
        target_user = await session.scalar(
            select(User).where(User.user_no == target_user_no)
        )
        if target_user is None:
            return None, "目标用户已经不存在，请重新选择。"
        file = await session.scalar(select(FileObject).where(FileObject.file_no == file_no))
        if not _bindable_user_asset(file, target_user):
            return None, "头像尚未通过安全处理、用途不匹配，或已经不属于目标用户。"
        assert file is not None
        return PreparedOperationsAction(
            action_type="admin_user_avatar_update",
            title="确认更新用户头像",
            summary=(
                "确认后会把这张已通过安全扫描的图片设为该用户公开头像，"
                "用户端导航、个人中心和消息会话会读取最新结果。"
            ),
            target_label=target_user.username,
            payload={"user_no": target_user.user_no, "file_no": file.file_no},
            resource_versions={"user": target_user.version, "file": file.version},
            changes=(
                {"label": "用户", "value": target_user.username},
                {"label": "图片处理", "value": "安全扫描已通过"},
                {"label": "生效范围", "value": "用户公开头像"},
            ),
            tool_code="governance.users.avatar.update.commit",
        ), None
    store_no = str(payload.get("store_id") or "")
    store = await session.scalar(select(Store).where(Store.store_no == store_no))
    if store is None:
        return None, "目标店铺已经不存在，请重新选择。"
    if context.audience == "merchant" and (
        context.store is None or context.store.id != store.id
    ):
        return None, "只能修改当前商家自己的店铺图片。"
    file = await session.scalar(select(FileObject).where(FileObject.file_no == file_no))
    expected_purpose = "store_logo" if purpose == "store_logo" else "product"
    if not _bindable_agent_asset(file, store, expected_purpose):
        return None, "图片尚未通过安全处理、用途不匹配，或已经不属于目标店铺。"
    prefix = "store_ops" if context.audience == "merchant" else "governance"
    action_prefix = "merchant" if context.audience == "merchant" else "admin"
    assert file is not None
    if purpose == "store_logo":
        return PreparedOperationsAction(
            action_type=f"{action_prefix}_store_logo_update",
            title="确认更新店铺 Logo",
            summary=(
                "确认后会把这张已通过安全扫描的图片设为店铺公开 Logo，"
                "顾客端店铺页、商品卡片与消息头像会读取最新结果。"
            ),
            target_label=store.store_name,
            payload={"store_no": store.store_no, "file_no": file.file_no},
            resource_versions={"store": store.version, "file": file.version},
            changes=(
                {"label": "店铺", "value": store.store_name},
                {"label": "图片处理", "value": "安全扫描已通过"},
                {"label": "生效范围", "value": "店铺公开 Logo"},
            ),
            tool_code=f"{prefix}.stores.logo.update.commit"
            if context.audience == "admin"
            else "store_ops.profile.logo.update.commit",
        ), None
    if purpose != "product_sku_image":
        return None, "不支持该图片操作类型。"
    product_no = str(payload.get("product_id") or "")
    sku_no = str(payload.get("sku_id") or "")
    row = (
        await session.execute(
            select(Product, ProductSku).join(
                ProductSku,
                ProductSku.product_id == Product.id,
            ).where(
                Product.product_no == product_no,
                Product.store_id == store.id,
                Product.deleted_at.is_(None),
                ProductSku.sku_no == sku_no,
                ProductSku.sku_status == "active",
            )
        )
    ).one_or_none()
    if row is None:
        return None, "目标商品或款式已经变化，请重新选择。"
    product, sku = row
    image_count = int(
        await session.scalar(
            select(func.count(ProductImage.id)).where(
                ProductImage.product_id == product.id,
                ProductImage.sku_id == sku.id,
                ProductImage.image_status == "active",
            )
        )
        or 0
    )
    return PreparedOperationsAction(
        action_type=f"{action_prefix}_product_sku_image_replace",
        title="确认替换款式展示图片",
        summary=(
            "确认后只替换所选款式的展示图片，其他款式图片、商品详情图片和历史订单快照"
            "保持不变。执行前会再次校验商品版本、款式状态和文件归属。"
        ),
        target_label=f"{store.store_name} · {product.product_name} · {sku.sku_name}",
        payload={
            "store_no": store.store_no,
            "product_no": product.product_no,
            "sku_no": sku.sku_no,
            "file_no": file.file_no,
        },
        resource_versions={
            "store": store.version,
            "product": product.version,
            "sku": sku.version,
            "file": file.version,
        },
        changes=(
            {"label": "商品", "value": product.product_name},
            {"label": "款式", "value": sku.sku_name},
            {"label": "当前图片", "value": f"{image_count} 张"},
            {"label": "确认后", "value": "替换为本次选择的 1 张图片"},
        ),
        tool_code=(
            "governance.catalog.skus.image.replace.commit"
            if context.audience == "admin"
            else "store_ops.catalog.skus.image.replace.commit"
        ),
    ), None


def _bindable_agent_asset(file: FileObject | None, store: Store, purpose: str) -> bool:
    return bool(
        file is not None
        and file.purpose == purpose
        and file.owner_type == "store"
        and file.owner_no == store.store_no
        and file.file_status == "active"
        and file.scan_status == "safe"
        and file.visibility == "public_derivative"
    )


def _bindable_user_asset(file: FileObject | None, user: User) -> bool:
    return bool(
        file is not None
        and file.purpose == "user_avatar"
        and file.owner_type == "user"
        and file.owner_no == user.user_no
        and file.file_status == "active"
        and file.scan_status == "safe"
        and file.visibility == "public_derivative"
    )


async def build_operations_approval(
    session: AsyncSession,
    context: TrustedOperationsContext,
    action: PreparedOperationsAction,
    *,
    execution_trace: dict[str, object] | None = None,
) -> AgentToolApproval:
    now = utc_now()
    approval = AgentToolApproval(
        approval_no=new_prefixed_ulid("apr_"),
        run_id=context.run.id,
        user_id=context.user.id,
        conversation_id=context.conversation.id,
        draft_id=None,
        action_type=action.action_type,
        action_payload={
            **action.payload,
            "title": action.title,
            "summary": action.summary,
            "target_label": action.target_label,
            "changes": [dict(item) for item in action.changes],
            "tool_code": action.tool_code,
        },
        arguments_hash=canonical_request_hash(
            {
                **action.payload,
                "title": action.title,
                "summary": action.summary,
                "target_label": action.target_label,
                "changes": [dict(item) for item in action.changes],
                "tool_code": action.tool_code,
            }
        ),
        resource_versions={
            **action.resource_versions,
            "agent_version": context.agent_version.version_no,
        },
        approval_status="pending",
        decision=None,
        expires_at=now + timedelta(minutes=10),
    )
    session.add(approval)
    await session.flush()
    context.run.run_status = "waiting"
    context.run.current_phase = "waiting_confirmation"
    context.run.version += 1
    await _approval_message(session, context, approval, execution_trace=execution_trace)
    return approval


async def approval_for_run(
    session: AsyncSession, context: TrustedOperationsContext
) -> AgentToolApproval | None:
    return cast(
        AgentToolApproval | None,
        await session.scalar(
            select(AgentToolApproval)
            .where(
                AgentToolApproval.run_id == context.run.id,
                AgentToolApproval.action_type.in_(OPERATIONS_ACTION_TYPES),
            )
            .with_for_update()
        ),
    )


async def execute_operations_approval(
    session: AsyncSession,
    context: TrustedOperationsContext,
    approval: AgentToolApproval,
    *,
    postgres: AsyncSession | None = None,
) -> tuple[str, str, dict[str, object], str | None]:
    if approval.user_id != context.user.id or approval.conversation_id != context.conversation.id:
        return (
            "failed",
            "确认内容不属于当前管理会话，本次没有执行任何操作。",
            {},
            "AGENT_APPROVAL_SCOPE_MISMATCH",
        )
    if approval.approval_status == "rejected":
        return "rejected", "已取消这次操作，业务数据没有变化。", {}, None
    if approval.approval_status == "consumed":
        action = await session.scalar(
            select(AgentToolAction).where(AgentToolAction.approval_id == approval.id)
        )
        return (
            "succeeded",
            "该操作已经执行完成，无需重复处理。",
            {"resource_id": action.resource_no if action else None},
            None,
        )
    if approval.approval_status != "approved":
        return "waiting", "请先核对操作影响并选择确认或取消。", {}, None
    if approval.expires_at <= utc_now():
        approval.approval_status = "expired"
        approval.version += 1
        await _settle_approval_message(session, approval, "expired", "AGENT_APPROVAL_EXPIRED")
        return "expired", "确认已经过期，请重新发起操作。", {}, "AGENT_APPROVAL_EXPIRED"
    payload = approval.action_payload
    if not isinstance(payload, dict) or canonical_request_hash(payload) != approval.arguments_hash:
        approval.approval_status = "consumed"
        approval.consumed_at = utc_now()
        approval.version += 1
        await _settle_approval_message(
            session, approval, "failed", "AGENT_APPROVAL_ARGUMENTS_MISMATCH"
        )
        return (
            "failed",
            "确认内容校验失败，本次没有执行任何操作。",
            {},
            "AGENT_APPROVAL_ARGUMENTS_MISMATCH",
        )
    if approval.resource_versions.get("agent_version") != context.agent_version.version_no:
        approval.approval_status = "consumed"
        approval.consumed_at = utc_now()
        approval.version += 1
        await _settle_approval_message(
            session, approval, "failed", "AGENT_APPROVAL_RESOURCE_CHANGED"
        )
        return "failed", "Agent 版本已变化，请重新发起操作。", {}, "AGENT_APPROVAL_RESOURCE_CHANGED"

    action = await session.scalar(
        select(AgentToolAction).where(AgentToolAction.approval_id == approval.id).with_for_update()
    )
    if action is not None and action.action_status == "succeeded":
        approval.approval_status = "consumed"
        approval.consumed_at = utc_now()
        approval.version += 1
        await _settle_approval_message(session, approval, "succeeded", None)
        return (
            "succeeded",
            "该操作已经执行完成，无需重复处理。",
            {"resource_id": action.resource_no},
            None,
        )
    if action is None:
        action = AgentToolAction(
            action_no=new_prefixed_ulid("act_"),
            approval_id=approval.id,
            run_id=context.run.id,
            action_type=approval.action_type,
            arguments_hash=approval.arguments_hash,
            idempotency_key=f"agent-action-{approval.approval_no}",
            action_status="running",
            started_at=utc_now(),
        )
        session.add(action)
    else:
        action.action_status = "running"
        action.error_code = None
        action.started_at = utc_now()
        action.version += 1
    await session.flush()

    try:
        answer, result, resource_no = await _execute_action(
            session, context, approval, action, payload, postgres=postgres
        )
    except OperationsActionConflict as exc:
        action.action_status = "failed"
        action.error_code = exc.code
        action.finished_at = utc_now()
        action.version += 1
        approval.approval_status = "consumed"
        approval.consumed_at = utc_now()
        approval.version += 1
        await _settle_approval_message(session, approval, "failed", exc.code)
        return "failed", exc.message, {}, exc.code

    action.action_status = "succeeded"
    action.resource_no = resource_no
    action.finished_at = utc_now()
    action.version += 1
    approval.approval_status = "consumed"
    approval.consumed_at = utc_now()
    approval.version += 1
    await _settle_approval_message(session, approval, "succeeded", None)
    return "succeeded", answer, result, None


class OperationsActionConflict(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def _prepare_product_detail_section_action(
    session: AsyncSession,
    context: TrustedOperationsContext,
    product: Product,
    store: Store,
    value: str,
    *,
    admin: bool,
) -> tuple[PreparedOperationsAction | None, str | None]:
    change, error = _requested_product_detail_section_change(value)
    if error is not None or change is None:
        return None, error
    if product.product_status not in {"draft", "rejected", "off_shelf", "on_sale"}:
        return None, (
            f"“{product.product_name}”当前为“{_product_status_label(product.product_status)}”，"
            "不能修改商品详情。"
        )
    current = None
    if product.current_detail_content_version_id is not None:
        current = await session.scalar(
            select(ProductContentVersion).where(
                ProductContentVersion.id == product.current_detail_content_version_id,
                ProductContentVersion.product_id == product.id,
            )
        )
    if current is not None and current.safe_blocks is None:
        return None, (
            "当前商品详情是旧版 HTML 格式，Agent 不会破坏性转换。"
            "请先在商品编辑页保存一次结构化详情。"
        )
    blocks = [dict(item) for item in (current.safe_blocks or [])] if current else []
    title = str(change["title"])
    matches = _detail_section_heading_indexes(blocks, title)
    if len(matches) > 1:
        return None, "匹配到多个同名详情段落，请先在商品编辑页整理重复标题。"
    deleting = change["mode"] == "delete"
    if deleting and not matches:
        return None, f"“{product.product_name}”中没有详情段落“{title}”。"
    content = change.get("content")
    try:
        preview_blocks = _replace_detail_section_blocks(
            blocks,
            title=title,
            content=content,
            delete=deleting,
        )
    except (LookupError, ValueError) as exc:
        return None, str(exc)
    if not preview_blocks:
        return None, "详情内容不能全部删空，请至少保留一个可阅读内容块。"
    action_prefix = "admin" if admin else "merchant"
    verb = "删除" if deleting else ("修改" if matches else "新增")
    target = (
        f"{store.store_name} · {product.product_name} · {title}"
        if admin
        else (f"{product.product_name} · {title}")
    )
    return PreparedOperationsAction(
        action_type=f"{action_prefix}_product_detail_section_{'delete' if deleting else 'upsert'}",
        title=f"确认{verb}商品详情段落",
        summary=(
            "确认后会生成新的结构化详情版本；在售商品会立即发布，"
            "原有图片及其顺序保持不变，历史版本保留审计。"
        ),
        target_label=target,
        payload={
            "store_no": store.store_no,
            "product_no": product.product_no,
            "section_title": title,
            "content": content,
            "current_content_version_no": (
                current.content_version_no if current is not None else None
            ),
        },
        resource_versions={
            "store": store.version,
            "product": product.version,
            "content": current.version if current is not None else 0,
        },
        changes=(
            {"label": "详情段落", "value": title},
            {
                "label": "处理方式",
                "value": f"{verb}（不改变图片和其他章节）",
            },
            *(({"label": "新内容", "value": str(content)[:300]},) if content is not None else ()),
        ),
        tool_code=(
            f"governance.catalog.detail_sections.{'delete' if deleting else 'upsert'}.commit"
            if admin
            else f"store_ops.catalog.detail_sections.{'delete' if deleting else 'upsert'}.commit"
        ),
    ), None


async def _prepare_merchant_action(
    session: AsyncSession, context: TrustedOperationsContext, value: str
) -> tuple[PreparedOperationsAction | None, str | None]:
    assert context.store is not None
    compact = _compact(value)
    if _requests_merchant_policy_write(value):
        return await _prepare_merchant_policy_action(session, context, value)
    email, email_error = _requested_store_email_update(value)
    if email_error is not None:
        return None, email_error
    if email is not None:
        credential = await _email_credential(session, context.store.owner_user_id)
        current_email = _credential_email(credential)
        if current_email == email:
            return None, "商家恢复邮箱当前已经是这个地址，无需重复修改。"
        encrypted_email = SecurityService(get_settings()).encrypt(
            "agent-action:merchant-email", email
        )
        return PreparedOperationsAction(
            action_type="merchant_store_email_update",
            title="确认修改商家恢复邮箱",
            summary=(
                "确认后只更新当前店铺商家账号的密码找回邮箱，不改变店铺公开资料、"
                "顾客订单或其他店铺账号。新邮箱会标记为待验证。"
            ),
            target_label=context.store.store_name,
            payload={
                "store_no": context.store.store_no,
                "email_ciphertext": encrypted_email.hex(),
            },
            resource_versions={
                "store": context.store.version,
                "owner_user_id": context.store.owner_user_id,
                "credential": credential.credential_version if credential is not None else 0,
            },
            changes=(
                {"label": "当前恢复邮箱", "value": _mask_email(current_email or "")},
                {"label": "新的恢复邮箱", "value": _mask_email(email)},
                {"label": "验证状态", "value": "修改后待验证"},
            ),
            tool_code="store_ops.account.email.update.commit",
        ), None
    profile_changes, profile_error = _merchant_profile_changes(value)
    if profile_error is not None:
        return None, profile_error
    if profile_changes:
        changes: list[dict[str, str]] = []
        payload: dict[str, object] = {"store_no": context.store.store_no}
        new_name = profile_changes.get("store_name")
        if new_name is not None:
            normalized_name = _normalize_store_name(new_name)
            duplicate = await session.scalar(
                select(Store.id).where(
                    Store.store_name_normalized == normalized_name,
                    Store.id != context.store.id,
                )
            )
            if duplicate is not None:
                return None, "该店铺名称已经存在，请更换一个名称。"
            payload["store_name"] = new_name
            changes.append(
                {
                    "label": "店铺名称",
                    "value": f"{context.store.store_name} → {new_name}",
                }
            )
        new_description = profile_changes.get("description")
        if new_description is not None:
            payload["description"] = new_description
            before = context.store.description or "未填写"
            changes.append(
                {
                    "label": "店铺简介",
                    "value": f"{before[:60]} → {new_description[:80] or '清空'}",
                }
            )
        return PreparedOperationsAction(
            action_type="merchant_store_profile",
            title="确认修改店铺公开资料",
            summary="确认后会立即更新用户端看到的店铺名称或简介，并刷新店铺公开知识版本。",
            target_label=context.store.store_name,
            payload=payload,
            resource_versions={"store": context.store.version},
            changes=tuple(changes),
            tool_code="store_ops.profile.update.commit",
        ), None
    if _requests_support_action(value):
        return await _prepare_support_action(session, context, value)
    if _requests_refund_decision(value):
        return await _prepare_refund_decision(session, context, value)
    draft_fields, draft_error = _requested_product_draft(value)
    if draft_error is not None:
        return None, draft_error
    if draft_fields:
        if context.store.store_status != "active":
            return None, "店铺当前未营业，不能创建新的商品草稿。"
        category = await session.scalar(
            select(Category)
            .where(Category.category_status == "active")
            .order_by(Category.sort_order, Category.id)
            .limit(1)
        )
        if category is None:
            return None, "平台当前没有可用的商品分类，请先联系平台管理员。"
        shipping_template = await session.scalar(
            select(ShippingTemplate)
            .where(
                ShippingTemplate.store_id == context.store.id,
                ShippingTemplate.template_status == "effective",
            )
            .order_by(ShippingTemplate.updated_at.desc(), ShippingTemplate.id.desc())
            .limit(1)
        )
        if shipping_template is None:
            return None, "本店还没有生效的配送模板，请先在商品编辑页保存一次发货设置。"
        product_name = str(draft_fields["product_name"])
        sku_name = str(draft_fields["sku_name"])
        draft_price_minor = int(str(draft_fields["price_minor"]))
        draft_stock_quantity = int(str(draft_fields["stock_quantity"]))
        origin_region_code = str(draft_fields["origin_region_code"])
        dispatch_min_hours = int(str(draft_fields["dispatch_min_hours"]))
        dispatch_max_hours = int(str(draft_fields["dispatch_max_hours"]))
        return PreparedOperationsAction(
            action_type="merchant_product_draft_create",
            title="确认创建结构化商品草稿",
            summary=(
                "确认后会在本店创建商品、首个款式、库存和发货配置。"
                "它仍是草稿，需在编辑页补充款式图片和商品详情后才能提交审核。"
            ),
            target_label=product_name,
            payload={
                "product_name": product_name,
                "sku_name": sku_name,
                "price_minor": draft_price_minor,
                "stock_quantity": draft_stock_quantity,
                "origin_region_code": origin_region_code,
                "dispatch_min_hours": dispatch_min_hours,
                "dispatch_max_hours": dispatch_max_hours,
                "category_no": category.category_no,
                "shipping_template_no": shipping_template.template_no,
            },
            resource_versions={
                "store": context.store.version,
                "category": category.version,
                "shipping_template": shipping_template.version,
            },
            changes=(
                {"label": "商品名称", "value": product_name},
                {"label": "首个款式", "value": sku_name},
                {"label": "售价", "value": f"¥{Decimal(draft_price_minor) / 100:.2f}"},
                {"label": "库存", "value": f"{draft_stock_quantity} 件"},
                {"label": "发货地", "value": _region_label(origin_region_code)},
                {
                    "label": "发货时效",
                    "value": _dispatch_window_label(dispatch_min_hours, dispatch_max_hours),
                },
            ),
            tool_code="store_ops.catalog.save_draft.commit",
        ), None
    if "暂停营业" in compact or "恢复营业" in compact:
        target = "suspended" if "暂停营业" in compact else "active"
        current = context.store.store_status
        if current == target:
            return None, f"本店当前已经是“{_store_status_label(target)}”，无需重复操作。"
        if (current, target) not in {("active", "suspended"), ("suspended", "active")}:
            return (
                None,
                f"本店当前状态为“{_store_status_label(current)}”，不能直接切换为“{_store_status_label(target)}”。",
            )
        if target == "active" and context.store.suspension_source == "platform":
            return None, "本店由平台暂停营业，店铺运营人员不能自行恢复，请联系平台管理员。"
        return PreparedOperationsAction(
            action_type="merchant_store_status",
            title="确认变更店铺营业状态",
            summary="状态变更会立即影响用户能否浏览店铺并创建新订单。",
            target_label=context.store.store_name,
            payload={"store_no": context.store.store_no, "target_status": target},
            resource_versions={"store": context.store.version},
            changes=(
                {"label": "当前状态", "value": _store_status_label(current)},
                {"label": "变更后", "value": _store_status_label(target)},
            ),
            tool_code="store_ops.status.update.commit",
        ), None

    if _requests_review_reply(value):
        return await _prepare_merchant_review_reply(session, context, value)

    if _requests_shipment_progress(value):
        return await _prepare_merchant_shipment_progress(session, context, value)

    if _requests_shipment_create(value):
        return await _prepare_merchant_shipment_create(session, context, value)

    products = await _store_products(session, context.store.id)
    product, product_error = _match_product(products, value)
    if product is None:
        return None, product_error or "请明确要操作的本店商品名称。"

    if _requests_product_detail_section_write(value):
        return await _prepare_product_detail_section_action(
            session,
            context,
            product,
            context.store,
            value,
            admin=False,
        )

    if _requests_admin_product_profile(value):
        profile_fields, profile_error = _admin_product_profile_changes(value)
        if profile_error is not None:
            return None, profile_error
        if product.product_status not in {"draft", "rejected", "off_shelf", "on_sale"}:
            return None, (
                f"“{product.product_name}”当前为“{_product_status_label(product.product_status)}”，"
                "不能修改基础资料。"
            )
        profile_display_changes: list[dict[str, str]] = []
        if "product_name" in profile_fields:
            profile_display_changes.append(
                {
                    "label": "商品名称",
                    "value": f"{product.product_name} → {profile_fields['product_name']}",
                }
            )
        if "description" in profile_fields:
            profile_display_changes.append(
                {
                    "label": "商品描述",
                    "value": f"{(product.description or '未填写')[:60]} → "
                    f"{profile_fields['description'][:100] or '清空'}",
                }
            )
        return PreparedOperationsAction(
            action_type="merchant_product_profile",
            title="确认修改商品基础资料",
            summary=(
                "确认后会更新当前商品版本；在售商品的新页面和店铺 AI "
                "会读取新资料，历史订单快照不会被改写。"
            ),
            target_label=product.product_name,
            payload={
                "store_no": context.store.store_no,
                "product_no": product.product_no,
                **profile_fields,
            },
            resource_versions={"store": context.store.version, "product": product.version},
            changes=tuple(profile_display_changes),
            tool_code="store_ops.catalog.update.commit",
        ), None

    sku_create_fields, sku_create_error = _requested_product_sku_create(value)
    if sku_create_error is not None:
        return None, sku_create_error
    if sku_create_fields:
        if product.product_status not in {"draft", "rejected", "off_shelf", "on_sale"}:
            return None, (
                f"“{product.product_name}”当前为“{_product_status_label(product.product_status)}”，"
                "不能新增款式。"
            )
        sku_name = str(sku_create_fields["sku_name"])
        duplicate = await session.scalar(
            select(ProductSku.id).where(
                ProductSku.product_id == product.id,
                ProductSku.spec_signature == _sku_style_signature(sku_name),
            )
        )
        if duplicate is not None:
            return None, f"“{product.product_name}”已经存在款式“{sku_name}”，请更换款式名称。"
        new_sku_price_minor = int(str(sku_create_fields["price_minor"]))
        stock_quantity = int(str(sku_create_fields["stock_quantity"]))
        return PreparedOperationsAction(
            action_type="merchant_product_sku_create",
            title="确认新增商品款式",
            summary=(
                "确认后会为该商品新增可售款式和初始库存，并立即影响商品价格范围。"
                "新款式还没有图片，请随后打开商品编辑页补充该款式图片。"
            ),
            target_label=f"{product.product_name} · {sku_name}",
            payload={
                "product_no": product.product_no,
                "sku_name": sku_name,
                "price_minor": new_sku_price_minor,
                "stock_quantity": stock_quantity,
            },
            resource_versions={"product": product.version, "store": context.store.version},
            changes=(
                {"label": "新增款式", "value": sku_name},
                {"label": "售价", "value": _money(new_sku_price_minor)},
                {"label": "初始库存", "value": f"{stock_quantity} 件"},
                {"label": "待补资料", "value": "该款式图片"},
            ),
            tool_code="store_ops.catalog.skus.create.commit",
        ), None

    faq_change, faq_change_error = _requested_product_faq_change(value)
    if faq_change_error is not None:
        return None, faq_change_error
    if faq_change is not None:
        if product.product_status not in {"draft", "rejected", "off_shelf", "on_sale"}:
            return None, (
                f"“{product.product_name}”当前为“{_product_status_label(product.product_status)}”，"
                "不能修改常见问题。"
            )
        faq_rows = list(
            (
                await session.execute(
                    select(ProductFaq, ProductFaqVersion)
                    .outerjoin(
                        ProductFaqVersion,
                        ProductFaqVersion.id == ProductFaq.current_content_version_id,
                    )
                    .where(
                        ProductFaq.product_id == product.id,
                        ProductFaq.faq_status != "archived",
                    )
                    .order_by(ProductFaq.sort_order, ProductFaq.id)
                )
            ).all()
        )
        normalized_question = _normalized_faq_question(str(faq_change["question"]))
        matching_faqs = [
            (faq, version)
            for faq, version in faq_rows
            if _normalized_faq_question(faq.question) == normalized_question
        ]
        mode = str(faq_change["mode"])
        if mode == "delete":
            if not matching_faqs:
                return None, f"“{product.product_name}”中没有常见问题“{normalized_question}”。"
            if len(matching_faqs) > 1:
                return None, "匹配到多个同名常见问题，请先在商品编辑页整理重复项。"
            target_faq, target_version = matching_faqs[0]
            return PreparedOperationsAction(
                action_type="merchant_product_faq_delete",
                title="确认删除商品常见问题",
                summary=("确认后该问答将不再对顾客和店铺 AI 公开；旧版本仍保留供审计。"),
                target_label=f"{product.product_name} · {target_faq.question}",
                payload={
                    "product_no": product.product_no,
                    "faq_no": target_faq.faq_no,
                    "question": target_faq.question,
                },
                resource_versions={
                    "product": product.version,
                    "faq": target_faq.version,
                    "faq_content": target_version.version if target_version is not None else 0,
                    "store": context.store.version,
                },
                changes=(
                    {"label": "待删除问题", "value": target_faq.question[:180]},
                    {
                        "label": "当前回答",
                        "value": (target_version.safe_text if target_version else "缺少有效回答")[
                            :180
                        ],
                    },
                ),
                tool_code="store_ops.catalog.faqs.delete.commit",
            ), None

        answer = str(faq_change["answer"])
        target_faq = matching_faqs[0][0] if matching_faqs else None
        target_version = matching_faqs[0][1] if matching_faqs else None
        if len(matching_faqs) > 1:
            return None, "匹配到多个同名常见问题，请先在商品编辑页整理重复项。"
        if target_version is not None and target_version.safe_text.strip() == answer.strip():
            return None, "该常见问题的回答已与要求一致，无需重复修改。"
        return PreparedOperationsAction(
            action_type="merchant_product_faq_upsert",
            title="确认修改商品常见问题" if target_faq else "确认新增商品常见问题",
            summary=(
                "确认后会发布新的问答版本，立即用于商品详情与店铺 AI 回答。"
                "商品其他常见问题保持不变。"
            ),
            target_label=f"{product.product_name} · {normalized_question}",
            payload={
                "product_no": product.product_no,
                "faq_no": target_faq.faq_no if target_faq else None,
                "question": normalized_question,
                "answer": answer,
            },
            resource_versions={
                "product": product.version,
                "faq": target_faq.version if target_faq else 0,
                "faq_content": target_version.version if target_version is not None else 0,
                "store": context.store.version,
            },
            changes=(
                {"label": "问题", "value": normalized_question[:180]},
                {
                    "label": "原回答",
                    "value": (target_version.safe_text if target_version else "新增")[:180],
                },
                {"label": "新回答", "value": answer[:180]},
            ),
            tool_code="store_ops.catalog.faqs.upsert.commit",
        ), None

    fulfillment_changes, fulfillment_error = _requested_product_fulfillment_changes(value)
    if fulfillment_error is not None:
        return None, fulfillment_error
    if fulfillment_changes:
        fulfillment_row = (
            await session.execute(
                select(ProductFulfillmentProfile, ShippingTemplate)
                .join(
                    ShippingTemplate,
                    ShippingTemplate.id == ProductFulfillmentProfile.shipping_template_id,
                )
                .where(ProductFulfillmentProfile.product_id == product.id)
            )
        ).one_or_none()
        if fulfillment_row is None:
            return None, (
                f"“{product.product_name}”还没有完整的发货配置。"
                "请先在商品编辑页选择配送模板并保存一次，再让我调整发货地、时效或购买须知。"
            )
        fulfillment, shipping_template = fulfillment_row
        target_origin = str(
            fulfillment_changes.get("origin_region_code") or fulfillment.origin_region_code
        )
        target_min = int(
            str(fulfillment_changes.get("dispatch_min_hours", fulfillment.dispatch_min_hours))
        )
        target_max = int(
            str(fulfillment_changes.get("dispatch_max_hours", fulfillment.dispatch_max_hours))
        )
        target_notice = (
            fulfillment_changes["purchase_notice"]
            if "purchase_notice" in fulfillment_changes
            else fulfillment.purchase_notice
        )
        if target_min > target_max:
            return None, "最早发货时间不能晚于最晚发货时间，请重新说明时效。"
        fulfillment_preview_changes: list[dict[str, str]] = []
        if target_origin != fulfillment.origin_region_code:
            fulfillment_preview_changes.append(
                {
                    "label": "发货地",
                    "value": (
                        f"{_region_label(fulfillment.origin_region_code)} → "
                        f"{_region_label(target_origin)}"
                    ),
                }
            )
        if (
            target_min != fulfillment.dispatch_min_hours
            or target_max != fulfillment.dispatch_max_hours
        ):
            fulfillment_preview_changes.append(
                {
                    "label": "发货时效",
                    "value": (
                        f"{_dispatch_window_label(fulfillment.dispatch_min_hours, fulfillment.dispatch_max_hours)}"  # noqa: E501
                        f" → {_dispatch_window_label(target_min, target_max)}"
                    ),
                }
            )
        if target_notice != fulfillment.purchase_notice:
            fulfillment_preview_changes.append(
                {
                    "label": "购买须知",
                    "value": f"{(fulfillment.purchase_notice or '未填写')[:80]} → "
                    f"{(str(target_notice) if target_notice else '清空')[:100]}",
                }
            )
        if not fulfillment_preview_changes:
            return None, "商品当前发货配置已经与要求一致，无需重复修改。"
        return PreparedOperationsAction(
            action_type="merchant_product_fulfillment",
            title="确认修改商品发货与购买须知",
            summary=(
                "确认后会更新用户在商品详情、结算和客服咨询中看到的发货信息。"
                "已创建订单的地址和成交快照不会被改写。"
            ),
            target_label=product.product_name,
            payload={
                "product_no": product.product_no,
                "shipping_template_no": shipping_template.template_no,
                "origin_region_code": target_origin,
                "dispatch_min_hours": target_min,
                "dispatch_max_hours": target_max,
                "purchase_notice": target_notice,
            },
            resource_versions={
                "product": product.version,
                "fulfillment": fulfillment.version,
                "shipping_template": shipping_template.version,
                "store": context.store.version,
            },
            changes=tuple(fulfillment_preview_changes),
            tool_code="store_ops.catalog.fulfillment.update.commit",
        ), None

    image_change, image_error = _requested_image_description(value)
    if image_error is not None:
        return None, image_error
    if image_change is not None:
        content = (
            await session.scalar(
                select(ProductContentVersion).where(
                    ProductContentVersion.id == product.current_detail_content_version_id,
                    ProductContentVersion.product_id == product.id,
                )
            )
            if product.current_detail_content_version_id is not None
            else None
        )
        blocks = content.safe_blocks if content is not None else None
        image_blocks = [item for item in blocks or [] if item.get("type") == "image"]
        image_number, new_description = image_change
        if not image_blocks:
            return None, f"“{product.product_name}”当前没有可编辑的详情图片。"
        if image_number < 1 or image_number > len(image_blocks):
            return None, (
                f"“{product.product_name}”共有 {len(image_blocks)} 张详情图片，"
                f"没有第 {image_number} 张。"
            )
        selected_image = image_blocks[image_number - 1]
        before = str(selected_image.get("description") or "未填写")
        file_id = str(selected_image.get("file_id") or "")
        if not file_id:
            return None, "目标详情图片缺少可信文件引用，请先在商品编辑页修复。"
        return PreparedOperationsAction(
            action_type="merchant_product_image_description",
            title="确认修改详情图片说明",
            summary="确认后会创建新的商品详情版本。在售商品会同步更新公开详情和 AI 可检索文字。",
            target_label=f"{product.product_name} · 第 {image_number} 张详情图片",
            payload={
                "product_no": product.product_no,
                "image_number": image_number,
                "file_id": file_id,
                "description": new_description,
            },
            resource_versions={
                "product": product.version,
                "content": content.version if content is not None else 0,
                "store": context.store.version,
            },
            changes=(
                {"label": "原图片说明", "value": before[:180]},
                {"label": "新图片说明", "value": new_description[:180] or "清空"},
            ),
            tool_code="store_ops.catalog.update_image_description.commit",
        ), None

    if _requests_product_delete(value):
        has_transactions = bool(
            await session.scalar(
                select(OrderItem.id).where(OrderItem.product_id == product.id).limit(1)
            )
        )
        if has_transactions:
            if product.product_status != "on_sale":
                return (
                    None,
                    f"“{product.product_name}”已经产生过交易，不能永久删除。"
                    f"商品当前为“{_product_status_label(product.product_status)}”，无需再次下架。",
                )
            return PreparedOperationsAction(
                action_type="merchant_product_status",
                title="该商品有交易记录，只能确认下架",
                summary="永久删除已被阻止。确认后仅停止新的购买，历史订单、售后与审计数据会继续保留。",
                target_label=product.product_name,
                payload={"product_no": product.product_no, "target_status": "off_shelf"},
                resource_versions={"product": product.version, "store": context.store.version},
                changes=(
                    {"label": "删除资格", "value": "已有交易，不允许永久删除"},
                    {"label": "可执行操作", "value": "下架商品"},
                ),
                tool_code="store_ops.catalog.status.commit",
            ), None
        return PreparedOperationsAction(
            action_type="merchant_product_delete",
            title="确认永久删除商品",
            summary="该商品尚未产生交易。确认后会永久移出商家与用户商品列表，并停用全部款式。此操作不可恢复。",
            target_label=product.product_name,
            payload={"product_no": product.product_no},
            resource_versions={"product": product.version, "store": context.store.version},
            changes=(
                {"label": "交易记录", "value": "无"},
                {"label": "执行结果", "value": "永久删除商品并停用全部款式"},
            ),
            tool_code="store_ops.catalog.delete.commit",
        ), None

    if _requests_product_submit(value):
        if product.product_status not in {"draft", "rejected", "off_shelf"}:
            return (
                None,
                f"“{product.product_name}”当前为“{_product_status_label(product.product_status)}”，"
                "不能重复提交审核。",
            )
        return PreparedOperationsAction(
            action_type="merchant_product_submit",
            title="确认提交商品审核并自动上架",
            summary=(
                "确认后将重新校验款式图片、详情内容和发货资料，再执行毒品、杀人相关"
                "违禁词审核。通过后立即上架，不通过则保留为需修改。"
            ),
            target_label=product.product_name,
            payload={"product_no": product.product_no},
            resource_versions={"product": product.version, "store": context.store.version},
            changes=(
                {"label": "当前状态", "value": _product_status_label(product.product_status)},
                {"label": "目标", "value": "自动审核，通过后立即销售"},
            ),
            tool_code="store_ops.catalog.submit_review.commit",
        ), None

    if _requests_product_status_change(value):
        target = "off_shelf" if "下架" in compact else "on_sale"
        if product.product_status == target:
            return None, f"“{product.product_name}”当前已经是“{_product_status_label(target)}”。"
        if (product.product_status, target) not in {
            ("on_sale", "off_shelf"),
            ("off_shelf", "on_sale"),
        }:
            return (
                None,
                f"“{product.product_name}”当前为"
                f"“{_product_status_label(product.product_status)}”，不能直接切换。",
            )
        if target == "on_sale" and context.store.store_status != "active":
            return None, "店铺当前未营业，不能上架商品。"
        return PreparedOperationsAction(
            action_type="merchant_product_status",
            title="确认变更商品销售状态",
            summary="下架会停止新的购买，恢复上架后会重新公开展示。历史订单不受影响。",
            target_label=product.product_name,
            payload={"product_no": product.product_no, "target_status": target},
            resource_versions={"product": product.version, "store": context.store.version},
            changes=(
                {"label": "当前状态", "value": _product_status_label(product.product_status)},
                {"label": "变更后", "value": _product_status_label(target)},
            ),
            tool_code="store_ops.catalog.status.commit",
        ), None

    sku_update_fields, sku_update_error = _requested_product_sku_update(value)
    if sku_update_error is not None:
        return None, sku_update_error
    if sku_update_fields:
        return await _prepare_product_sku_update_action(
            session,
            context,
            value,
            product=product,
            store=context.store,
            fields=sku_update_fields,
            admin=False,
        )

    skus = await _product_skus(session, product.id)
    sku, sku_error = _match_sku(skus, value)
    if sku is None:
        return None, sku_error or f"“{product.product_name}”有多个款式，请明确款式名称。"
    inventory = await session.scalar(select(Inventory).where(Inventory.sku_id == sku.id))

    if _requests_product_sku_disable(value):
        if product.product_status == "on_sale" and len(skus) <= 1:
            return None, "在售商品必须保留至少一个有效款式，请先新增替代款式或下架商品。"
        return PreparedOperationsAction(
            action_type="merchant_product_sku_disable",
            title="确认移除商品款式",
            summary=(
                "确认后该款式不再向顾客展示或接受新的购买；历史订单、库存流水和审计记录仍会保留。"
            ),
            target_label=f"{product.product_name} · {sku.sku_name}",
            payload={"product_no": product.product_no, "sku_no": sku.sku_no},
            resource_versions={
                "product": product.version,
                "sku": sku.version,
                "store": context.store.version,
            },
            changes=(
                {"label": "当前状态", "value": "有效款式"},
                {"label": "确认后", "value": "停用并从顾客可选款式中移除"},
            ),
            tool_code="store_ops.catalog.skus.disable.commit",
        ), None

    if "库存" in compact:
        quantity = _target_integer(value, "库存")
        if quantity is None:
            return None, "请明确目标库存数量，例如“把 8 支款式库存设为 50 件”。"
        if quantity < (inventory.reserved_quantity if inventory else 0):
            return (
                None,
                f"目标库存不能低于已预占的 {inventory.reserved_quantity if inventory else 0} 件。",
            )
        current_quantity = inventory.on_hand_quantity if inventory else 0
        return PreparedOperationsAction(
            action_type="merchant_inventory_set",
            title="确认调整款式库存",
            summary="库存调整会立即影响商品可售数量，已预占库存不会被释放。",
            target_label=f"{product.product_name} · {sku.sku_name}",
            payload={
                "product_no": product.product_no,
                "sku_no": sku.sku_no,
                "target_quantity": quantity,
            },
            resource_versions={
                "product": product.version,
                "sku": sku.version,
                "inventory": inventory.version if inventory else 0,
                "store": context.store.version,
            },
            changes=(
                {"label": "当前库存", "value": f"{current_quantity} 件"},
                {"label": "调整后", "value": f"{quantity} 件"},
                {
                    "label": "已预占",
                    "value": f"{inventory.reserved_quantity if inventory else 0} 件",
                },
            ),
            tool_code="store_ops.inventory.adjust.commit",
        ), None

    if any(marker in compact for marker in ("改价", "价格改", "价格设", "售价改", "售价设")):
        price_minor = _target_price_minor(value)
        if price_minor is None:
            return None, "请明确目标价格，例如“把 8 支款式价格改为 9.90 元”。"
        return PreparedOperationsAction(
            action_type="merchant_price_set",
            title="确认修改款式售价",
            summary="新价格会供之后的购物车和结算读取，已创建订单的成交价不会改变。",
            target_label=f"{product.product_name} · {sku.sku_name}",
            payload={
                "product_no": product.product_no,
                "sku_no": sku.sku_no,
                "target_price_minor": price_minor,
            },
            resource_versions={
                "product": product.version,
                "sku": sku.version,
                "store": context.store.version,
            },
            changes=(
                {"label": "当前售价", "value": _money(sku.sale_price_amount)},
                {"label": "修改后", "value": _money(price_minor)},
            ),
            tool_code="store_ops.price.update.commit",
        ), None
    return None, "我识别到写操作，但还不能安全确定要修改的字段。请说明商品、款式和目标值。"


async def _prepare_product_sku_update_action(
    session: AsyncSession,
    context: TrustedOperationsContext,
    value: str,
    *,
    product: Product,
    store: Store,
    fields: dict[str, object],
    admin: bool,
) -> tuple[PreparedOperationsAction | None, str | None]:
    if product.product_status not in {"draft", "rejected", "off_shelf", "on_sale"}:
        return None, (
            f"“{product.product_name}”当前为“{_product_status_label(product.product_status)}”，"
            "不能修改款式。"
        )
    skus = await _product_skus(session, product.id)
    sku, sku_error = _match_sku(skus, value)
    if sku is None:
        return None, sku_error or f"“{product.product_name}”有多个款式，请明确款式名称。"
    inventory = await session.scalar(select(Inventory).where(Inventory.sku_id == sku.id))
    if inventory is None:
        return None, "该款式缺少库存记录，请先在商品编辑页修复。"
    new_sku_name = str(fields.get("sku_name") or sku.sku_name)
    new_price_minor = int(str(fields.get("price_minor", sku.sale_price_amount)))
    new_stock = int(str(fields.get("stock_quantity", inventory.on_hand_quantity)))
    if new_stock < inventory.reserved_quantity:
        return None, f"目标库存不能低于已预占的 {inventory.reserved_quantity} 件。"
    if new_sku_name != sku.sku_name:
        duplicate = await session.scalar(
            select(ProductSku.id).where(
                ProductSku.product_id == product.id,
                ProductSku.id != sku.id,
                ProductSku.sku_status == "active",
                ProductSku.spec_signature == _sku_style_signature(new_sku_name),
            )
        )
        if duplicate is not None:
            return None, f"“{product.product_name}”已经存在款式“{new_sku_name}”。"
    changes: list[dict[str, str]] = []
    if new_sku_name != sku.sku_name:
        changes.append({"label": "款式名称", "value": f"{sku.sku_name} → {new_sku_name}"})
    if new_price_minor != sku.sale_price_amount:
        changes.append(
            {
                "label": "售价",
                "value": f"{_money(sku.sale_price_amount)} → {_money(new_price_minor)}",
            }
        )
    if new_stock != inventory.on_hand_quantity:
        changes.append(
            {
                "label": "库存",
                "value": f"{inventory.on_hand_quantity} 件 → {new_stock} 件",
            }
        )
    if not changes:
        return None, "该款式当前资料已经与要求一致，无需重复修改。"
    prefix = "governance" if admin else "store_ops"
    return PreparedOperationsAction(
        action_type="admin_product_sku_update" if admin else "merchant_product_sku_update",
        title=("确认以平台管理员身份修改商品款式" if admin else "确认修改商品款式"),
        summary=(
            "确认后会原子更新指定款式的名称、售价或库存；新结算读取新值，"
            "历史订单快照保持不变。"
        ),
        target_label=(
            f"{store.store_name} · {product.product_name} · {sku.sku_name}"
            if admin
            else f"{product.product_name} · {sku.sku_name}"
        ),
        payload={
            "store_no": store.store_no,
            "product_no": product.product_no,
            "sku_no": sku.sku_no,
            "sku_name": new_sku_name,
            "price_minor": new_price_minor,
            "stock_quantity": new_stock,
        },
        resource_versions={
            "store": store.version,
            "product": product.version,
            "sku": sku.version,
            "inventory": inventory.version,
        },
        changes=tuple(changes),
        tool_code=f"{prefix}.catalog.skus.update.commit",
    ), None


async def _prepare_merchant_policy_action(
    session: AsyncSession, context: TrustedOperationsContext, value: str
) -> tuple[PreparedOperationsAction | None, str | None]:
    assert context.store is not None
    command = _merchant_policy_command(value)
    policy_type = _merchant_policy_type(value)
    title, content = _merchant_policy_fields(value)
    policies = list(
        (
            await session.scalars(
                select(StoreServicePolicy)
                .where(StoreServicePolicy.store_id == context.store.id)
                .order_by(StoreServicePolicy.policy_version.desc(), StoreServicePolicy.id.desc())
            )
        ).all()
    )
    if command == "create":
        if title is None or content is None:
            return (
                None,
                "新建政策请同时写明标题和内容，例如“新建售后政策，标题: 七天退换说明，内容: ……”。",
            )
        return PreparedOperationsAction(
            action_type="merchant_store_policy_manage",
            title="确认新建店铺政策草稿",
            summary="确认后只创建新的版本化草稿，不会立即成为用户端公开承诺。",
            target_label=f"{context.store.store_name} · {title}",
            payload={
                "command": command,
                "store_no": context.store.store_no,
                "policy_type": policy_type,
                # ``title`` is reserved by the approval envelope for the
                # human-facing confirmation title.  Keep the business field
                # namespaced so building the approval cannot overwrite it.
                "policy_title": title,
                "content": content,
            },
            resource_versions={"store": context.store.version},
            changes=(
                {"label": "政策类型", "value": _policy_type_label(policy_type)},
                {"label": "标题", "value": title},
                {"label": "内容摘要", "value": content[:160]},
                {"label": "创建后状态", "value": "草稿，尚未公开"},
            ),
            tool_code="store_ops.policy.manage.commit",
        ), None

    policy, error = _match_store_policy(policies, value, policy_type, command)
    if policy is None:
        return None, error
    if command == "update":
        if policy.policy_status != "draft":
            return None, "已发布或已撤回政策不可直接修改，请新建一个替代政策草稿。"
        if title is None and content is None:
            return None, "请明确要修改的标题或内容。"
        changes: list[dict[str, str]] = []
        if title is not None:
            changes.append({"label": "标题", "value": f"{policy.title} → {title}"})
        if content is not None:
            changes.append({"label": "内容摘要", "value": content[:160]})
        payload: dict[str, object] = {
            "command": command,
            "store_no": context.store.store_no,
            "policy_no": policy.policy_no,
        }
        if title is not None:
            payload["policy_title"] = title
        if content is not None:
            payload["content"] = content
        return PreparedOperationsAction(
            action_type="merchant_store_policy_manage",
            title="确认修改店铺政策草稿",
            summary="确认后只更新当前草稿版本，用户端仍不会读取该草稿。",
            target_label=f"{policy.title} · {policy.policy_no}",
            payload=payload,
            resource_versions={"store": context.store.version, "policy": policy.version},
            changes=tuple(changes),
            tool_code="store_ops.policy.manage.commit",
        ), None

    target_status = "published" if command == "publish" else "withdrawn"
    if command == "publish" and policy.policy_status != "draft":
        return None, f"该政策当前为“{_policy_status_label(policy.policy_status)}”，不能发布。"
    if command == "withdraw" and policy.policy_status != "published":
        return None, f"该政策当前为“{_policy_status_label(policy.policy_status)}”，不能撤回。"
    return PreparedOperationsAction(
        action_type="merchant_store_policy_manage",
        title="确认发布店铺政策" if command == "publish" else "确认撤回店铺政策",
        summary=(
            "发布后会成为用户端和店铺客服可引用的公开承诺。"
            if command == "publish"
            else "撤回后店铺客服不再把该政策作为当前公开承诺，历史审计仍保留。"
        ),
        target_label=f"{policy.title} · {policy.policy_no}",
        payload={
            "command": command,
            "store_no": context.store.store_no,
            "policy_no": policy.policy_no,
        },
        resource_versions={"store": context.store.version, "policy": policy.version},
        changes=(
            {"label": "当前状态", "value": _policy_status_label(policy.policy_status)},
            {"label": "确认后", "value": _policy_status_label(target_status)},
        ),
        tool_code="store_ops.policy.manage.commit",
    ), None


async def _prepare_admin_user_asset_action(
    session: AsyncSession, context: TrustedOperationsContext, value: str
) -> tuple[PreparedOperationsAction | None, str | None]:
    users = list(
        (
            await session.scalars(select(User).where(User.deleted_at.is_(None)).order_by(User.id))
        ).all()
    )
    target, error = _match_user(users, value)
    if target is None:
        return None, error or "请明确要管理的唯一用户名。"
    if target.id == context.user.id:
        return None, "AI 管家不能通过用户资产工具修改当前管理员账号。"

    compact = _compact(value)
    settings = get_settings()
    security = SecurityService(settings)
    if "购物车" in compact:
        cart = await session.scalar(select(Cart).where(Cart.user_id == target.id))
        if cart is None:
            return None, f"用户“{target.username}”的购物车当前为空。"
        rows = list(
            (
                await session.execute(
                    select(CartItem, ProductSku, Product)
                    .join(ProductSku, ProductSku.id == CartItem.sku_id)
                    .join(Product, Product.id == ProductSku.product_id)
                    .where(CartItem.cart_id == cart.id)
                    .order_by(CartItem.id)
                )
            ).all()
        )
        if _requests_admin_cart_clear(compact):
            if not rows:
                return None, f"用户“{target.username}”的购物车当前为空，无需清空。"
            total_quantity = sum(item.quantity for item, _, _ in rows)
            return PreparedOperationsAction(
                action_type="admin_user_cart_clear",
                title="确认清空用户购物车",
                summary=(
                    "确认后会移除该用户购物车中的全部条目；不会删除商品、订单、收藏或历史交易。"
                ),
                target_label=f"{target.username} · 全部购物车商品",
                payload={"user_no": target.user_no},
                resource_versions={
                    "user": target.version,
                    "cart": cart.version,
                    "cart_item_count": len(rows),
                },
                changes=(
                    {"label": "当前条目", "value": f"{len(rows)} 种商品"},
                    {"label": "当前总数量", "value": f"{total_quantity} 件"},
                    {"label": "确认后", "value": "购物车为空"},
                ),
                tool_code="governance.users.cart.clear.commit",
            ), None
        matches = [
            row
            for row in rows
            if row[0].cart_item_no in value
            or _compact(row[1].sku_name) in compact
            or _compact(row[2].product_name) in compact
        ]
        if len(matches) != 1:
            if not matches and len(rows) == 1:
                matches = rows
            else:
                return None, (
                    "匹配到多个购物车商品，请补充完整商品名、款式名或购物车条目编号。"
                    if len(matches) > 1
                    else "没有找到要操作的购物车商品，请先查看该用户购物车并指定商品。"
                )
        item, sku, product = matches[0]
        if any(marker in compact for marker in ("删除", "移除")):
            return PreparedOperationsAction(
                action_type="admin_user_cart_item_delete",
                title="确认移除用户购物车商品",
                summary="确认后仅移除该购物车条目，不会删除商品、订单或历史交易。",
                target_label=f"{target.username} · {product.product_name} · {sku.sku_name}",
                payload={"user_no": target.user_no, "cart_item_no": item.cart_item_no},
                resource_versions={
                    "user": target.version,
                    "cart": cart.version,
                    "cart_item": item.version,
                    "product": product.version,
                    "sku": sku.version,
                },
                changes=(
                    {"label": "当前数量", "value": f"{item.quantity} 件"},
                    {"label": "确认后", "value": "从该用户购物车移除"},
                ),
                tool_code="governance.users.cart.remove_item.commit",
            ), None
        quantity_match = re.search(
            r"(?:购物车.{0,80})?(?:数量|件数)\s*(?:改为|改成|设为|设置为|调整为)?\s*(\d{1,3})",
            value,
        )
        if quantity_match is None:
            return None, "请明确新的购物车数量，例如“把用户 tulubi 购物车里的铅笔数量改为 2”。"
        quantity = int(quantity_match.group(1))
        if not 1 <= quantity <= 99:
            return None, "购物车中同一款式的数量必须为 1 至 99 件。"
        if quantity == item.quantity:
            return None, f"该购物车条目当前已经是 {quantity} 件，无需重复修改。"
        return PreparedOperationsAction(
            action_type="admin_user_cart_item_update",
            title="确认修改用户购物车数量",
            summary="确认后会按购物车版本更新数量，并重新核对商品、库存和可购买状态。",
            target_label=f"{target.username} · {product.product_name} · {sku.sku_name}",
            payload={
                "user_no": target.user_no,
                "cart_item_no": item.cart_item_no,
                "quantity": quantity,
            },
            resource_versions={
                "user": target.version,
                "cart": cart.version,
                "cart_item": item.version,
                "product": product.version,
                "sku": sku.version,
            },
            changes=(
                {"label": "当前数量", "value": f"{item.quantity} 件"},
                {"label": "变更后", "value": f"{quantity} 件"},
            ),
            tool_code="governance.users.cart.update_quantity.commit",
        ), None

    if "收货地址" in compact or "默认地址" in compact:
        identity = IdentityService(session, cast(Any, None), security, settings)
        address_list = await identity.list_addresses(target.id)
        addresses = address_list.items
        create_address = any(
            marker in compact for marker in ("新增收货地址", "添加收货地址", "创建收货地址")
        )
        update_address = _requests_admin_address_update(value)
        if create_address:
            fields, field_error = _admin_address_fields(value, require_all=True)
            if field_error is not None:
                return None, field_error
            if not address_list.can_create:
                return None, "该用户已经保存 20 个有效收货地址，请先删除不再使用的地址。"
            return PreparedOperationsAction(
                action_type="admin_user_address_create",
                title="确认新增用户收货地址",
                summary=(
                    "确认后会为该用户新增一条收货地址。系统会再次检查用户版本和地址数量；"
                    "若这是第一条地址，会自动设为默认地址。"
                ),
                target_label=f"{target.username} · {fields['region_label']} {fields['address']}",
                payload={"user_no": target.user_no, **fields},
                resource_versions={"user": target.version, "address_count": len(addresses)},
                changes=(
                    {"label": "收货人", "value": str(fields["recipient_name"])},
                    {"label": "联系电话", "value": _mask_phone(str(fields["phone"]))},
                    {"label": "地区", "value": str(fields["region_label"])},
                    {"label": "详细地址", "value": str(fields["address"])},
                    {
                        "label": "默认地址",
                        "value": "是" if bool(fields["is_default"]) or not addresses else "否",
                    },
                ),
                tool_code="governance.users.addresses.create.commit",
            ), None
        address_match = re.search(r"adr_[0-9a-z]+", value, flags=re.IGNORECASE)
        selected_address = next(
            (
                item
                for item in addresses
                if address_match is not None
                and item.address_id.casefold() == address_match.group(0).casefold()
            ),
            None,
        )
        ordinal_match = re.search(r"第\s*(\d{1,2})\s*(?:个|条)?(?:收货)?地址", value)
        if selected_address is None and ordinal_match is not None:
            ordinal = int(ordinal_match.group(1))
            if 1 <= ordinal <= len(addresses):
                selected_address = addresses[ordinal - 1]
        if selected_address is None and len(addresses) == 1:
            selected_address = addresses[0]
        if selected_address is None:
            return None, (
                "该用户有多个收货地址，请提供完整地址编号或说明第几个地址。"
                if addresses
                else f"用户“{target.username}”当前没有收货地址。"
            )
        if update_address:
            fields, field_error = _admin_address_fields(value, require_all=False)
            if field_error is not None:
                return None, field_error
            changes: list[dict[str, str]] = []
            current_region = region_label(
                selected_address.province_code,
                selected_address.city_code,
                selected_address.district_code,
            )
            if "recipient_name" in fields:
                changes.append(
                    {
                        "label": "收货人",
                        "value": f"{selected_address.recipient_name} → {fields['recipient_name']}",
                    }
                )
            if "phone" in fields:
                changes.append(
                    {
                        "label": "联系电话",
                        "value": (
                            f"{selected_address.phone_masked} → {_mask_phone(str(fields['phone']))}"
                        ),
                    }
                )
            if "region_label" in fields:
                changes.append(
                    {
                        "label": "地区",
                        "value": f"{current_region} → {fields['region_label']}",
                    }
                )
            if "address" in fields:
                changes.append(
                    {
                        "label": "详细地址",
                        "value": f"{selected_address.address} → {fields['address']}",
                    }
                )
            if not changes:
                return None, "请明确要修改的收货人、联系电话、地区或详细地址。"
            return PreparedOperationsAction(
                action_type="admin_user_address_update",
                title="确认修改用户收货地址",
                summary="确认后会按地址版本更新指定字段，未提及的地址字段保持不变。",
                target_label=f"{target.username} · {selected_address.address}",
                payload={
                    "user_no": target.user_no,
                    "address_no": selected_address.address_id,
                    **fields,
                },
                resource_versions={
                    "user": target.version,
                    "address": selected_address.version,
                },
                changes=tuple(changes),
                tool_code="governance.users.addresses.update.commit",
            ), None
        set_default = any(marker in compact for marker in ("设为默认", "设成默认"))
        if set_default and selected_address.is_default:
            return None, "该地址当前已经是默认收货地址，无需重复设置。"
        action_type = (
            "admin_user_address_set_default" if set_default else "admin_user_address_delete"
        )
        return PreparedOperationsAction(
            action_type=action_type,
            title="确认设置默认收货地址" if set_default else "确认删除用户收货地址",
            summary=(
                "确认后会把该地址设为用户默认收货地址，其他地址将取消默认标记。"
                if set_default
                else "确认后会删除该地址；若它是默认地址，系统会从剩余地址中选择新的默认地址。"
            ),
            target_label=f"{target.username} · {selected_address.address}",
            payload={"user_no": target.user_no, "address_no": selected_address.address_id},
            resource_versions={
                "user": target.version,
                "address": selected_address.version,
                "address_count": len(addresses),
            },
            changes=(
                {"label": "收货人", "value": selected_address.recipient_name},
                {"label": "联系方式", "value": selected_address.phone_masked},
                {
                    "label": "确认后",
                    "value": "设为默认地址" if set_default else "从地址簿删除",
                },
            ),
            tool_code=(
                "governance.users.addresses.set_default.commit"
                if set_default
                else "governance.users.addresses.delete.commit"
            ),
        ), None

    if "收藏" in compact or "关注" in compact:
        product_rows = list(
            (
                await session.execute(
                    select(ProductFavorite, Product)
                    .join(Product, Product.id == ProductFavorite.product_id)
                    .where(
                        ProductFavorite.user_id == target.id,
                        ProductFavorite.deleted_at.is_(None),
                    )
                    .order_by(ProductFavorite.id)
                )
            ).all()
        )
        store_rows = list(
            (
                await session.execute(
                    select(StoreFollow, Store)
                    .join(Store, Store.id == StoreFollow.store_id)
                    .where(StoreFollow.user_id == target.id, StoreFollow.deleted_at.is_(None))
                    .order_by(StoreFollow.id)
                )
            ).all()
        )
        product_matches = [
            row
            for row in product_rows
            if row[1].product_no in value or _compact(row[1].product_name) in compact
        ]
        store_matches = [
            row
            for row in store_rows
            if row[1].store_no in value or _compact(row[1].store_name) in compact
        ]
        explicit_store = "店铺" in compact or "关注" in compact
        explicit_product = "商品" in compact
        candidates: list[tuple[str, object, object]] = []
        if not explicit_store or explicit_product:
            candidates.extend(
                ("product", favorite, product) for favorite, product in product_matches
            )
        if not explicit_product or explicit_store:
            candidates.extend(("store", follow, store) for follow, store in store_matches)
        if len(candidates) != 1:
            return None, (
                "匹配到多个收藏对象，请补充完整商品名、店铺名或公开编号。"
                if len(candidates) > 1
                else "没有在该用户的收藏中找到指定商品或店铺。"
            )
        kind, relation, entity = candidates[0]
        if kind == "product":
            favorite = cast(ProductFavorite, relation)
            product = cast(Product, entity)
            return PreparedOperationsAction(
                action_type="admin_user_favorite_product_remove",
                title="确认取消用户的商品收藏",
                summary="确认后只取消该用户与此商品的收藏关系，不影响商品本身。",
                target_label=f"{target.username} · {product.product_name}",
                payload={"user_no": target.user_no, "product_no": product.product_no},
                resource_versions={
                    "user": target.version,
                    "favorite": favorite.version,
                    "product": product.version,
                },
                changes=({"label": "确认后", "value": "从用户商品收藏中移除"},),
                tool_code="governance.users.favorites.remove_product.commit",
            ), None
        follow = cast(StoreFollow, relation)
        store = cast(Store, entity)
        return PreparedOperationsAction(
            action_type="admin_user_favorite_store_remove",
            title="确认取消用户的店铺收藏",
            summary="确认后只取消该用户与此店铺的关注关系，不影响店铺营业状态。",
            target_label=f"{target.username} · {store.store_name}",
            payload={"user_no": target.user_no, "store_no": store.store_no},
            resource_versions={
                "user": target.version,
                "favorite": follow.version,
                "store": store.version,
            },
            changes=({"label": "确认后", "value": "从用户店铺收藏中移除"},),
            tool_code="governance.users.favorites.remove_store.commit",
        ), None

    return None, "请明确要修改购物车、取消收藏，还是删除或设置默认收货地址。"


async def _prepare_admin_action(
    session: AsyncSession, context: TrustedOperationsContext, value: str
) -> tuple[PreparedOperationsAction | None, str | None]:
    compact = _compact(value)
    if _requests_admin_order_cancel(value):
        order_match = re.search(r"ord_[0-9a-z]+", value, flags=re.IGNORECASE)
        if order_match is None:
            return None, (
                "请提供要取消的完整订单编号（ord_…）。"
                "取消订单会影响交易状态，不能按模糊名称猜测。"
            )
        order_no = order_match.group(0)
        row = (
            await session.execute(
                select(Order, Store)
                .join(Store, Store.id == Order.store_id)
                .where(Order.order_no == order_no)
            )
        ).one_or_none()
        if row is None:
            return None, "没有找到该订单，请核对完整订单编号。"
        order, store = row
        if order.order_status != "pending_payment" or order.payment_status != "unpaid":
            return None, (
                "平台只允许取消仍处于待付款且未发起有效支付的交易。"
                f"该订单当前为“{_order_status_label(order.order_status)}”，本次没有修改订单。"
            )
        sibling_count = int(
            await session.scalar(
                select(func.count(Order.id)).where(Order.trade_order_id == order.trade_order_id)
            )
            or 0
        )
        reason_match = re.search(r"(?:原因|理由)\s*[:：]\s*(.{2,200})$", value, re.S)
        reason = (
            reason_match.group(1).strip()
            if reason_match
            else "管理员通过 AI 管家确认取消未付款交易"
        )
        return PreparedOperationsAction(
            action_type="admin_order_cancel",
            title="确认取消未付款交易",
            summary=(
                "订单取消接口以合并交易单为原子边界。确认后会再次检查全部店铺子订单和支付尝试，"
                "只有整笔交易仍未付款时才取消并释放库存；任何状态变化都会阻止执行。"
            ),
            target_label=f"{store.store_name} · {order.order_no}",
            payload={"order_no": order.order_no, "reason": reason[:500]},
            resource_versions={"order": order.version},
            changes=(
                {"label": "当前状态", "value": "待付款 · 未支付"},
                {"label": "交易范围", "value": f"{sibling_count} 个店铺子订单"},
                {"label": "确认后", "value": "取消整笔未付款交易并释放库存预占"},
            ),
            tool_code="governance.trade.orders.cancel.commit",
        ), None
    if _requests_admin_evaluation_run(value):
        require_significant_gain = any(
            marker in compact for marker in ("显著提升", "显著优于", "必须提升")
        )
        return PreparedOperationsAction(
            action_type="admin_ai_evaluation_run",
            title="确认启动 AI 发布准入评估",
            summary=(
                "确认后会用当前锁定的 Golden Dataset 比较已登记基线与候选策略，"
                "创建唯一评估任务并进入异步执行；不会因此自动发布模型、Prompt、Skill 或 Tool。"
            ),
            target_label=f"{CANDIDATE_TYPE} · {CANDIDATE_VERSION}",
            payload={
                "dataset_id": DATASET_ID,
                "dataset_version": DATASET_VERSION,
                "baseline_type": BASELINE_TYPE,
                "baseline_version": BASELINE_VERSION,
                "candidate_type": CANDIDATE_TYPE,
                "candidate_version": CANDIDATE_VERSION,
                "require_significant_gain": require_significant_gain,
            },
            resource_versions={
                "dataset_version": DATASET_VERSION,
                "baseline_version": BASELINE_VERSION,
                "candidate_version": CANDIDATE_VERSION,
            },
            changes=(
                {"label": "固定测试集", "value": f"{DATASET_ID} · {DATASET_VERSION}"},
                {"label": "生产基线", "value": f"{BASELINE_TYPE} · {BASELINE_VERSION}"},
                {"label": "候选策略", "value": f"{CANDIDATE_TYPE} · {CANDIDATE_VERSION}"},
                {
                    "label": "准入要求",
                    "value": "必须显著优于基线" if require_significant_gain else "满足发布门禁",
                },
            ),
            tool_code="governance.ai.evaluations.run.commit",
        ), None
    if _requests_admin_agent_prompt_draft(value):
        return await _prepare_admin_agent_prompt_draft(session, value)
    if _requests_admin_ai_publication(value):
        return await _prepare_admin_ai_publication(session, value)
    if _requests_admin_knowledge_write(value):
        document_match = re.search(r"kdoc_[0-9a-z]+", value, flags=re.IGNORECASE)
        if document_match is None:
            return None, "请提供要发布、重建索引或撤回的完整知识文档编号 (kdoc_…)。"
        document_no = document_match.group(0)
        document = await session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.document_no == document_no)
        )
        if document is None:
            return None, "没有找到该知识文档，请核对完整文档编号。"
        withdraw = "撤回知识文档" in compact
        if withdraw and document.document_status == "withdrawn":
            return None, "该知识文档已经撤回，无需重复操作。"
        if not withdraw and document.document_status == "withdrawn":
            return None, "已撤回文档不能直接重新发布，请先在知识库页面创建新的安全版本。"
        action_type = (
            "admin_knowledge_document_withdraw" if withdraw else "admin_knowledge_document_publish"
        )
        return PreparedOperationsAction(
            action_type=action_type,
            title="确认撤回知识文档" if withdraw else "确认发布并重建知识索引",
            summary=(
                "确认后会立即把该文档移出检索范围，并删除当前向量切片; 历史审计记录仍会保留。"
                if withdraw
                else "确认后会保留当前可用索引，同时创建新的影子索引任务; 只有新索引成功后才切换。"
            ),
            target_label=f"{document.title} · {document.document_no}",
            payload={
                "document_no": document.document_no,
                "operation": "withdraw" if withdraw else "publish",
            },
            resource_versions={"knowledge_document": document.version},
            changes=(
                {
                    "label": "当前状态",
                    "value": _knowledge_document_status_label(document.document_status),
                },
                {
                    "label": "确认后",
                    "value": "撤回并停止检索" if withdraw else "发布并创建影子索引任务",
                },
                {"label": "内容版本", "value": document.content_version},
            ),
            tool_code=(
                "governance.knowledge.documents.withdraw.commit"
                if withdraw
                else "governance.knowledge.documents.publish.commit"
            ),
        ), None
    if _requests_admin_shipment_progress(value):
        return await _prepare_admin_shipment_progress(session, context, value)
    if _requests_dead_letter_replay(value):
        dead_letter_rows = list(
            (
                await session.scalars(
                    select(DeadLetterEvent)
                    .where(DeadLetterEvent.dead_status == "open")
                    .order_by(DeadLetterEvent.last_failed_at.desc(), DeadLetterEvent.id.desc())
                    .limit(100)
                )
            ).all()
        )
        matches = [
            item for item in dead_letter_rows if item.dead_letter_no.casefold() in value.casefold()
        ]
        if len(matches) > 1:
            return None, "匹配到多个待处理死信，请提供完整死信编号。"
        if len(matches) == 1:
            item = matches[0]
        elif len(dead_letter_rows) == 1:
            item = dead_letter_rows[0]
        elif not dead_letter_rows:
            return None, "当前没有可申请重放的待处理死信。"
        else:
            return None, "当前有多个待处理死信，请提供完整死信编号。"
        source = (
            await session.scalar(select(OutboxEvent).where(OutboxEvent.event_no == item.source_no))
            if item.source_type == "outbox"
            else None
        )
        blockers: list[str] = []
        if item.schema_version != 1:
            blockers.append("事件 Schema 版本暂不支持")
        if source is None:
            blockers.append("原始 Outbox 事件不存在")
        elif source.event_status != "failed":
            blockers.append("原始事件状态已变化")
        elif source.event_type != item.event_type:
            blockers.append("事件类型不一致")
        elif canonical_request_hash(source.payload) != item.payload_hash:
            blockers.append("不可变 Payload 校验失败")
        if blockers:
            return None, "该死信当前不能申请重放: " + "; ".join(blockers) + "。"
        reason = _dead_letter_replay_reason(value)
        return PreparedOperationsAction(
            action_type="admin_dead_letter_replay_request",
            title="确认提交死信重放双人审批",
            summary=(
                "本次确认只创建重放审批申请，不会立即重新投递事件。之后仍需两名不同管理员批准，"
                "发起人不能自批; 执行器会再次校验事件版本和不可变 Payload。"
            ),
            target_label=f"{item.event_type} · {item.dead_letter_no}",
            payload={
                "dead_letter_no": item.dead_letter_no,
                "reason_code": "AGENT_ASSISTED_REPLAY",
                "reason": reason,
            },
            resource_versions={
                "dead_letter": item.version,
                "source_aggregate": source.aggregate_version if source is not None else None,
            },
            changes=(
                {"label": "当前状态", "value": "待处理死信"},
                {"label": "失败次数", "value": f"{item.failure_count} 次"},
                {"label": "第一步", "value": "创建双人复核审批申请"},
                {"label": "不会立即发生", "value": "不会直接重放或修改原 Payload"},
            ),
            tool_code="observability.dead_letters.replay_request.commit",
        ), None
    if _requests_support_action(value):
        return await _prepare_support_action(session, context, value)
    if _requests_refund_decision(value):
        return await _prepare_refund_decision(session, context, value)
    if _requests_admin_user_create(value):
        fields, field_error = _admin_user_create_fields(value)
        if field_error is not None or fields is None:
            return None, field_error or "请提供完整的用户创建资料。"
        normalized_username = normalize_username(str(fields["username"]))
        duplicate = await session.scalar(
            select(User.id).where(User.username_normalized == normalized_username)
        )
        if duplicate is not None:
            return None, "该用户名已经被使用，请输入其他用户名。"
        security = SecurityService(get_settings())
        password_ciphertext = security.encrypt(
            "agent-action:user-password", security.new_opaque_token(32)
        ).hex()
        return PreparedOperationsAction(
            action_type="admin_user_create",
            title="确认创建普通用户",
            summary=(
                "确认后会创建独立普通用户身份、不可见随机初始凭证、恢复邮箱、默认用户角色和零余额钱包。"
                "账号会被标记为必须通过恢复邮箱重置密码，聊天中不收集或显示密码。"
            ),
            target_label=str(fields["username"]),
            payload={
                "new_username": fields["username"],
                "new_email": fields["email"],
                "password_ciphertext": password_ciphertext,
            },
            resource_versions={"username_available": normalized_username},
            changes=(
                {"label": "用户名", "value": str(fields["username"])},
                {"label": "恢复邮箱", "value": _mask_email(str(fields["email"]))},
                {"label": "账号类型", "value": "普通用户"},
                {"label": "首次使用", "value": "通过恢复邮箱重置密码"},
                {"label": "初始余额", "value": "¥0.00"},
            ),
            tool_code="governance.users.create.commit",
        ), None
    if _requests_admin_user_password_reset(value):
        users = list(
            (
                await session.scalars(
                    select(User).where(User.deleted_at.is_(None)).order_by(User.id)
                )
            ).all()
        )
        target, error = _match_user(users, value)
        if target is None:
            return None, error or "请明确要重置密码的唯一用户名。"
        if target.id == context.user.id:
            return None, "AI 管家不能重置当前正在操作的管理员账号密码。"
        credential = await session.scalar(
            select(UserCredential).where(
                UserCredential.user_id == target.id,
                UserCredential.credential_type == "password",
                UserCredential.credential_status == "active",
            )
        )
        if credential is None:
            return None, "该用户当前没有可重置的有效密码凭证。"
        return PreparedOperationsAction(
            action_type="admin_user_password_reset_requirement",
            title="确认要求用户重置密码",
            summary=(
                "确认后会立即撤销该用户的全部登录会话，并把账号标记为必须通过已登记邮箱完成密码重置。"
                "聊天中不会收集、生成或显示用户的新密码。"
            ),
            target_label=target.username,
            payload={"user_no": target.user_no},
            resource_versions={
                "user": target.version,
                "credential": credential.credential_version,
            },
            changes=(
                {"label": "目标用户", "value": target.username},
                {"label": "密码凭证", "value": "标记为必须重置"},
                {"label": "登录会话", "value": "全部撤销"},
            ),
            tool_code="governance.users.require_password_reset.commit",
        ), None
    if _requests_admin_user_asset_write(value):
        return await _prepare_admin_user_asset_action(session, context, value)
    if _requests_admin_user_profile(value) or _requests_admin_user_delete(value):
        users = list(
            (
                await session.scalars(
                    select(User).where(User.deleted_at.is_(None)).order_by(User.id)
                )
            ).all()
        )
        target, error = _match_user(users, value)
        if target is None:
            return None, error or "请明确要操作的唯一用户名。"
        if target.id == context.user.id:
            return None, "AI 管家不能修改或注销当前正在操作的管理员账号。"
        if _requests_admin_user_delete(value):
            return PreparedOperationsAction(
                action_type="admin_user_delete",
                title="确认注销用户账号",
                summary=(
                    "确认后会先复核该账号是否存在交易或管理身份。符合条件时进入可恢复审计的注销任务，"
                    "并立即撤销全部会话。存在历史订单时会阻止删除。"
                ),
                target_label=target.username,
                payload={"user_no": target.user_no},
                resource_versions={"user": target.version},
                changes=(
                    {"label": "当前状态", "value": _user_status_label(target.user_status)},
                    {"label": "影响", "value": "撤销登录并进入账户注销编排"},
                ),
                tool_code="governance.users.delete.commit",
            ), None
        changes, parse_error = _admin_user_profile_changes(value)
        if parse_error is not None:
            return None, parse_error
        if not changes:
            return None, "请明确新的用户名或邮箱。"
        display_changes: list[dict[str, str]] = []
        payload: dict[str, object] = {"user_no": target.user_no}
        new_username = changes.get("username")
        if new_username is not None:
            normalized = normalize_username(new_username)
            duplicate = await session.scalar(
                select(User.id).where(
                    User.username_normalized == normalized,
                    User.id != target.id,
                )
            )
            if duplicate is not None:
                return None, "新的用户名已经被使用，请更换后再试。"
            payload["username"] = new_username
            display_changes.append(
                {"label": "用户名", "value": f"{target.username} → {new_username}"}
            )
        new_email = changes.get("email")
        if new_email is not None:
            try:
                normalized_email = normalize_target("email", new_email)
            except ValueError:
                return None, "新的邮箱格式不正确，请输入完整邮箱。"
            payload["email"] = normalized_email
            display_changes.append(
                {"label": "邮箱", "value": f"更新为 {_mask_email(normalized_email)}"}
            )
        return PreparedOperationsAction(
            action_type="admin_user_profile",
            title="确认修改用户账号资料",
            summary="确认后会按用户版本更新资料。修改邮箱会重新标记为待验证，历史订单不会改变。",
            target_label=target.username,
            payload=payload,
            resource_versions={"user": target.version},
            changes=tuple(display_changes),
            tool_code="governance.users.update_profile.commit",
        ), None
    if _requests_admin_wallet_adjustment(value):
        users = list(
            (
                await session.scalars(
                    select(User).where(User.deleted_at.is_(None)).order_by(User.id)
                )
            ).all()
        )
        wallet_user, error = _match_user(users, value)
        if wallet_user is None:
            return None, error or "请明确要调整余额的唯一用户名。"
        if wallet_user.id == context.user.id:
            return None, "AI 管家不能通过聊天调整当前管理员账号的余额。"
        direction, amount_minor, amount_error = _parse_admin_wallet_adjustment(value)
        if amount_error is not None or direction is None or amount_minor is None:
            return None, amount_error or "请明确增加或扣减的金额。"
        wallet = await session.scalar(
            select(UserWallet).where(
                UserWallet.user_id == wallet_user.id,
                UserWallet.currency == "CNY",
            )
        )
        balance = wallet.balance_amount if wallet else 0
        if wallet is not None and wallet.wallet_status != "active":
            return None, "该用户钱包当前不可调整。"
        if direction == "debit" and amount_minor > balance:
            return None, f"扣减金额不能超过当前余额 {_money(balance)}。"
        after = balance + amount_minor if direction == "credit" else balance - amount_minor
        return PreparedOperationsAction(
            action_type="admin_user_wallet_adjust",
            title="确认调整用户账户余额",
            summary="确认后会生成不可变资金流水并回读最新余额，不会覆盖或删除历史流水。",
            target_label=wallet_user.username,
            payload={
                "user_no": wallet_user.user_no,
                "direction": direction,
                "amount_minor": amount_minor,
            },
            resource_versions={
                "user": wallet_user.version,
                "wallet": wallet.version if wallet else 0,
            },
            changes=(
                {"label": "调整方向", "value": "增加" if direction == "credit" else "扣减"},
                {"label": "调整金额", "value": _money(amount_minor)},
                {"label": "当前余额", "value": _money(balance)},
                {"label": "调整后余额", "value": _money(after)},
            ),
            tool_code="governance.users.wallet.adjust.commit",
        ), None
    if _requests_admin_user_change(value) or any(
        marker in compact
        for marker in ("冻结用户", "冻结账号", "解冻用户", "解冻账号", "恢复账号", "强制下线")
    ):
        users = list(
            (
                await session.scalars(
                    select(User).where(User.deleted_at.is_(None)).order_by(User.id)
                )
            ).all()
        )
        target, error = _match_user(users, value)
        if target is None:
            return None, error or "请明确要操作的用户名。"
        if target.id == context.user.id:
            return None, "AI 管家不能冻结或强制下线当前操作中的管理员账号。"
        if "强制下线" in compact:
            active_sessions = int(
                await session.scalar(
                    select(func.count(AuthSession.id)).where(
                        AuthSession.user_id == target.id,
                        AuthSession.revoked_at.is_(None),
                        AuthSession.expires_at > utc_now(),
                    )
                )
                or 0
            )
            return PreparedOperationsAction(
                action_type="admin_user_force_logout",
                title="确认强制下线用户",
                summary="将撤销该用户当前全部有效会话，但不会删除账号或业务数据。",
                target_label=target.username,
                payload={"user_no": target.user_no},
                resource_versions={"user": target.version},
                changes=(
                    {"label": "当前有效会话", "value": f"{active_sessions} 个"},
                    {"label": "执行后", "value": "全部会话失效"},
                ),
                tool_code="governance.users.force_logout.commit",
            ), None
        target_status = "suspended" if "冻结" in compact else "active"
        if target.user_status == target_status:
            return None, f"“{target.username}”当前已经是“{_user_status_label(target_status)}”。"
        if (target.user_status, target_status) not in {
            ("active", "suspended"),
            ("suspended", "active"),
        }:
            return (
                None,
                f"“{target.username}”当前为“{_user_status_label(target.user_status)}”，"
                "不能直接切换。",
            )
        return PreparedOperationsAction(
            action_type="admin_user_status",
            title="确认变更用户状态",
            summary="冻结会立即撤销该用户有效会话，解冻后用户可以重新登录。",
            target_label=target.username,
            payload={"user_no": target.user_no, "target_status": target_status},
            resource_versions={"user": target.version},
            changes=(
                {"label": "当前状态", "value": _user_status_label(target.user_status)},
                {"label": "变更后", "value": _user_status_label(target_status)},
            ),
            tool_code="governance.users.status.commit",
        ), None

    if _requests_admin_store_create(value):
        fields, field_error = _admin_store_create_fields(value)
        if field_error is not None or fields is None:
            return None, field_error or "请提供完整的店铺创建资料。"
        normalized_name = _normalize_store_name(str(fields["store_name"]))
        normalized_username = normalize_username(str(fields["merchant_username"]))
        if (
            await session.scalar(
                select(Store.id).where(Store.store_name_normalized == normalized_name)
            )
            is not None
        ):
            return None, "该店铺名称已经存在，请输入其他名称。"
        if (
            await session.scalar(
                select(User.id).where(User.username_normalized == normalized_username)
            )
            is not None
        ):
            return None, "该商家用户名已经存在，请输入其他用户名。"
        security = SecurityService(get_settings())
        password_ciphertext = security.encrypt(
            "agent-action:merchant-password", security.new_opaque_token(32)
        ).hex()
        payload: dict[str, object] = {
            "new_store_name": fields["store_name"],
            "new_merchant_username": fields["merchant_username"],
            "new_merchant_email": fields["merchant_email"],
            "password_ciphertext": password_ciphertext,
        }
        if fields.get("description") is not None:
            payload["new_description"] = fields["description"]
        return PreparedOperationsAction(
            action_type="admin_store_create",
            title="确认创建店铺与商家账号",
            summary=(
                "确认后会创建相互独立的商家身份、店铺、商家角色和不可见随机初始凭证。"
                "商家首次使用前必须通过登记邮箱重置密码，聊天中不收集或显示密码。"
            ),
            target_label=str(fields["store_name"]),
            payload=payload,
            resource_versions={
                "store_name_available": normalized_name,
                "merchant_username_available": normalized_username,
            },
            changes=(
                {"label": "店铺名称", "value": str(fields["store_name"])},
                {"label": "商家用户名", "value": str(fields["merchant_username"])},
                {
                    "label": "商家恢复邮箱",
                    "value": _mask_email(str(fields["merchant_email"])),
                },
                {"label": "首次使用", "value": "通过恢复邮箱重置密码"},
            ),
            tool_code="governance.stores.create.commit",
        ), None
    if _requests_admin_store_profile(value) or _requests_admin_store_delete(value):
        stores = list((await session.scalars(select(Store).order_by(Store.id))).all())
        store, error = _match_store(stores, value)
        if store is None:
            return None, error or "请明确要操作的店铺名称。"
        if _requests_admin_store_delete(value):
            return PreparedOperationsAction(
                action_type="admin_store_delete",
                title="确认注销店铺与商家账号",
                summary=(
                    "确认后会复核交易、售后和关联数据。无交易才进入店铺注销编排。"
                    "存在历史交易时会阻止删除并保留审计数据。"
                ),
                target_label=store.store_name,
                payload={"store_no": store.store_no},
                resource_versions={"store": store.version},
                changes=(
                    {"label": "当前状态", "value": _store_status_label(store.store_status)},
                    {"label": "影响", "value": "注销商家身份、店铺及无交易关联数据"},
                ),
                tool_code="governance.stores.delete.commit",
            ), None
        email, email_error = _requested_store_email_update(value)
        if email_error is not None:
            return None, email_error
        if email is not None:
            credential = await _email_credential(session, store.owner_user_id)
            current_email = _credential_email(credential)
            if current_email == email:
                return None, "该商家账号的恢复邮箱当前已经是这个地址，无需重复修改。"
            encrypted_email = SecurityService(get_settings()).encrypt(
                "agent-action:merchant-email", email
            )
            return PreparedOperationsAction(
                action_type="admin_store_merchant_email_update",
                title="确认修改店铺商家恢复邮箱",
                summary=(
                    "确认后只更新该店铺所有者账号的密码找回邮箱，不改变店铺公开资料、"
                    "历史订单或其他商家账号。新邮箱会标记为待验证。"
                ),
                target_label=store.store_name,
                payload={
                    "store_no": store.store_no,
                    "email_ciphertext": encrypted_email.hex(),
                },
                resource_versions={
                    "store": store.version,
                    "owner_user_id": store.owner_user_id,
                    "credential": credential.credential_version if credential is not None else 0,
                },
                changes=(
                    {"label": "当前恢复邮箱", "value": _mask_email(current_email or "")},
                    {"label": "新的恢复邮箱", "value": _mask_email(email)},
                    {"label": "验证状态", "value": "修改后待验证"},
                ),
                tool_code="governance.stores.merchant_email.update.commit",
            ), None
        changes, parse_error = _admin_store_profile_changes(value)
        if parse_error is not None:
            return None, parse_error
        payload = {"store_no": store.store_no, **changes}
        display_changes = []
        if "store_name" in changes:
            new_name = changes["store_name"]
            duplicate = await session.scalar(
                select(Store.id).where(
                    Store.store_name_normalized == _normalize_store_name(new_name),
                    Store.id != store.id,
                )
            )
            if duplicate is not None:
                return None, "新的店铺名称已经存在，请更换后再试。"
            display_changes.append(
                {"label": "店铺名称", "value": f"{store.store_name} → {new_name}"}
            )
        if "description" in changes:
            display_changes.append(
                {
                    "label": "店铺简介",
                    "value": f"{(store.description or '未填写')[:60]} → "
                    f"{changes['description'][:100] or '清空'}",
                }
            )
        return PreparedOperationsAction(
            action_type="admin_store_profile",
            title="确认修改店铺公开资料",
            summary="确认后用户端与商家端都会读取新的店铺名称或简介，并保留平台操作审计。",
            target_label=store.store_name,
            payload=payload,
            resource_versions={"store": store.version},
            changes=tuple(display_changes),
            tool_code="governance.stores.update.commit",
        ), None

    if "暂停营业" in compact or "恢复营业" in compact:
        stores = list((await session.scalars(select(Store).order_by(Store.id))).all())
        store, error = _match_store(stores, value)
        if store is None:
            return None, error or "请明确要操作的店铺名称。"
        store_target_status = "suspended" if "暂停营业" in compact else "active"
        if store.store_status == store_target_status:
            return None, (
                f"“{store.store_name}”当前已经是“{_store_status_label(store_target_status)}”。"
            )
        if (store.store_status, store_target_status) not in {
            ("active", "suspended"),
            ("suspended", "active"),
        }:
            return (
                None,
                f"“{store.store_name}”当前为“{_store_status_label(store.store_status)}”，"
                "不能直接切换。",
            )
        return PreparedOperationsAction(
            action_type="admin_store_status",
            title="确认变更店铺营业状态",
            summary="平台暂停会阻止商家自行恢复，恢复营业后店铺重新面向用户开放。",
            target_label=store.store_name,
            payload={"store_no": store.store_no, "target_status": store_target_status},
            resource_versions={"store": store.version},
            changes=(
                {"label": "当前状态", "value": _store_status_label(store.store_status)},
                {"label": "变更后", "value": _store_status_label(store_target_status)},
            ),
            tool_code="governance.stores.status.commit",
        ), None

    if (
        _requests_product_delete(value)
        or _requests_product_status_change(value)
        or _requests_admin_product_profile(value)
        or _requests_product_image_description_write(value)
        or _requests_product_faq_write(value)
        or bool(_requested_product_sku_create(value)[0])
        or bool(_requested_product_sku_update(value)[0])
        or _requests_product_sku_disable(value)
        or _requests_product_detail_section_write(value)
        or _requests_admin_product_review(value)
    ):
        rows = (
            await session.execute(
                select(Product, Store)
                .join(Store, Store.id == Product.store_id)
                .where(Product.deleted_at.is_(None))
                .order_by(Product.id)
            )
        ).all()
        product, store, error = _match_admin_product([(row[0], row[1]) for row in rows], value)
        if product is None or store is None:
            return None, error or "请明确店铺名称和商品名称。"
        sku_create_fields, sku_create_error = _requested_product_sku_create(value)
        if sku_create_error is not None:
            return None, sku_create_error
        if sku_create_fields:
            if product.product_status not in {"draft", "rejected", "off_shelf", "on_sale"}:
                return None, (
                    f"“{product.product_name}”当前为“{_product_status_label(product.product_status)}”，"
                    "不能新增款式。"
                )
            sku_name = str(sku_create_fields["sku_name"])
            duplicate = await session.scalar(
                select(ProductSku.id).where(
                    ProductSku.product_id == product.id,
                    ProductSku.spec_signature == _sku_style_signature(sku_name),
                )
            )
            if duplicate is not None:
                return None, f"“{product.product_name}”已经存在款式“{sku_name}”。"
            price_minor = int(str(sku_create_fields["price_minor"]))
            stock_quantity = int(str(sku_create_fields["stock_quantity"]))
            return PreparedOperationsAction(
                action_type="admin_product_sku_create",
                title="确认以平台管理员身份新增商品款式",
                summary=(
                    "确认后会创建新 SKU、初始库存和库存流水，并更新商品价格范围。"
                    "新款式图片仍需在商品治理弹窗中上传。"
                ),
                target_label=f"{store.store_name} · {product.product_name} · {sku_name}",
                payload={
                    "store_no": store.store_no,
                    "product_no": product.product_no,
                    "sku_name": sku_name,
                    "price_minor": price_minor,
                    "stock_quantity": stock_quantity,
                },
                resource_versions={"store": store.version, "product": product.version},
                changes=(
                    {"label": "新增款式", "value": sku_name},
                    {"label": "售价", "value": _money(price_minor)},
                    {"label": "初始库存", "value": f"{stock_quantity} 件"},
                    {"label": "待补资料", "value": "该款式图片"},
                ),
                tool_code="governance.catalog.skus.create.commit",
            ), None
        sku_update_fields, sku_update_error = _requested_product_sku_update(value)
        if sku_update_error is not None:
            return None, sku_update_error
        if sku_update_fields:
            return await _prepare_product_sku_update_action(
                session,
                context,
                value,
                product=product,
                store=store,
                fields=sku_update_fields,
                admin=True,
            )
        if _requests_product_sku_disable(value):
            skus = await _product_skus(session, product.id)
            sku, sku_error = _match_sku(skus, value)
            if sku is None:
                return None, sku_error or f"“{product.product_name}”有多个款式，请明确款式名称。"
            if product.product_status == "on_sale" and len(skus) <= 1:
                return None, "在售商品必须保留至少一个有效款式，请先新增替代款式或下架商品。"
            return PreparedOperationsAction(
                action_type="admin_product_sku_disable",
                title="确认以平台管理员身份移除商品款式",
                summary=("确认后该款式不再接受新购买；历史订单、库存流水和平台审计继续保留。"),
                target_label=f"{store.store_name} · {product.product_name} · {sku.sku_name}",
                payload={
                    "store_no": store.store_no,
                    "product_no": product.product_no,
                    "sku_no": sku.sku_no,
                },
                resource_versions={
                    "store": store.version,
                    "product": product.version,
                    "sku": sku.version,
                },
                changes=(
                    {"label": "当前状态", "value": "有效款式"},
                    {"label": "确认后", "value": "停用并从顾客可选款式中移除"},
                ),
                tool_code="governance.catalog.skus.disable.commit",
            ), None
        if _requests_product_detail_section_write(value):
            return await _prepare_product_detail_section_action(
                session,
                context,
                product,
                store,
                value,
                admin=True,
            )
        if _requests_product_faq_write(value):
            faq_change, faq_change_error = _requested_product_faq_change(value)
            if faq_change_error is not None or faq_change is None:
                return None, faq_change_error or "请明确常见问题与回答。"
            faq_rows = list(
                (
                    await session.execute(
                        select(ProductFaq, ProductFaqVersion)
                        .outerjoin(
                            ProductFaqVersion,
                            ProductFaqVersion.id == ProductFaq.current_content_version_id,
                        )
                        .where(
                            ProductFaq.product_id == product.id,
                            ProductFaq.faq_status != "archived",
                        )
                        .order_by(ProductFaq.sort_order, ProductFaq.id)
                    )
                ).all()
            )
            normalized_question = _normalized_faq_question(str(faq_change["question"]))
            matching_faqs = [
                (faq, version)
                for faq, version in faq_rows
                if _normalized_faq_question(faq.question) == normalized_question
            ]
            if len(matching_faqs) > 1:
                return None, "匹配到多个同名常见问题，请先在商品治理页整理重复项。"
            target_faq = matching_faqs[0][0] if matching_faqs else None
            target_version = matching_faqs[0][1] if matching_faqs else None
            if faq_change["mode"] == "delete":
                if target_faq is None:
                    return None, f"“{product.product_name}”中没有常见问题“{normalized_question}”。"
                return PreparedOperationsAction(
                    action_type="admin_product_faq_delete",
                    title="确认以平台管理员身份删除常见问题",
                    summary="确认后问答不再对顾客和店铺 AI 公开，旧版本保留审计。",
                    target_label=(
                        f"{store.store_name} · {product.product_name} · {target_faq.question}"
                    ),
                    payload={
                        "store_no": store.store_no,
                        "product_no": product.product_no,
                        "faq_no": target_faq.faq_no,
                        "question": target_faq.question,
                    },
                    resource_versions={
                        "store": store.version,
                        "product": product.version,
                        "faq": target_faq.version,
                        "faq_content": target_version.version if target_version is not None else 0,
                    },
                    changes=({"label": "待删除问题", "value": target_faq.question[:180]},),
                    tool_code="governance.catalog.faqs.delete.commit",
                ), None
            answer = str(faq_change["answer"])
            if target_version is not None and target_version.safe_text.strip() == answer.strip():
                return None, "该常见问题的回答已与要求一致，无需重复修改。"
            return PreparedOperationsAction(
                action_type="admin_product_faq_upsert",
                title=(
                    "确认以平台管理员身份修改常见问题"
                    if target_faq
                    else "确认以平台管理员身份新增常见问题"
                ),
                summary="确认后会发布新问答版本，并写入平台操作审计。",
                target_label=f"{store.store_name} · {product.product_name} · {normalized_question}",
                payload={
                    "store_no": store.store_no,
                    "product_no": product.product_no,
                    "faq_no": target_faq.faq_no if target_faq else None,
                    "question": normalized_question,
                    "answer": answer,
                },
                resource_versions={
                    "store": store.version,
                    "product": product.version,
                    "faq": target_faq.version if target_faq else 0,
                    "faq_content": target_version.version if target_version is not None else 0,
                },
                changes=(
                    {"label": "问题", "value": normalized_question[:180]},
                    {"label": "新回答", "value": answer[:180]},
                ),
                tool_code="governance.catalog.faqs.upsert.commit",
            ), None
        if _requests_product_image_description_write(value):
            image_change, image_error = _requested_image_description(value)
            if image_error is not None or image_change is None:
                return None, image_error or "请明确详情图片序号和新的图片说明。"
            content = (
                await session.scalar(
                    select(ProductContentVersion).where(
                        ProductContentVersion.id == product.current_detail_content_version_id,
                        ProductContentVersion.product_id == product.id,
                    )
                )
                if product.current_detail_content_version_id is not None
                else None
            )
            image_blocks = [
                item
                for item in (content.safe_blocks if content is not None else []) or []
                if item.get("type") == "image"
            ]
            image_number, new_description = image_change
            if not image_blocks:
                return None, f"“{product.product_name}”当前没有可编辑的详情图片。"
            if image_number < 1 or image_number > len(image_blocks):
                return None, (
                    f"“{product.product_name}”共有 {len(image_blocks)} 张详情图片，"
                    f"没有第 {image_number} 张。"
                )
            selected_image = image_blocks[image_number - 1]
            file_id = str(selected_image.get("file_id") or "")
            if not file_id:
                return None, "目标详情图片缺少可信文件引用，请先在商品治理页面修复。"
            before = str(selected_image.get("description") or "未填写")
            return PreparedOperationsAction(
                action_type="admin_product_image_description",
                title="确认以平台管理员身份修改详情图片说明",
                summary=(
                    "确认后会创建新的商品详情版本，并写入平台操作审计。"
                    "在售商品的公开详情与 AI 可检索文字会同步更新，历史订单快照不受影响。"
                ),
                target_label=(
                    f"{store.store_name} · {product.product_name} · 第 {image_number} 张详情图片"
                ),
                payload={
                    "store_no": store.store_no,
                    "product_no": product.product_no,
                    "image_number": image_number,
                    "file_id": file_id,
                    "description": new_description,
                },
                resource_versions={
                    "store": store.version,
                    "product": product.version,
                    "content": content.version if content is not None else 0,
                },
                changes=(
                    {"label": "原图片说明", "value": before[:180]},
                    {"label": "新图片说明", "value": new_description[:180] or "清空"},
                ),
                tool_code="governance.catalog.update_image_description.commit",
            ), None
        if _requests_admin_product_profile(value):
            changes, parse_error = _admin_product_profile_changes(value)
            if parse_error is not None:
                return None, parse_error
            display_changes = []
            if "product_name" in changes:
                display_changes.append(
                    {
                        "label": "商品名称",
                        "value": f"{product.product_name} → {changes['product_name']}",
                    }
                )
            if "description" in changes:
                display_changes.append(
                    {
                        "label": "商品描述",
                        "value": f"{(product.description or '未填写')[:60]} → "
                        f"{changes['description'][:100] or '清空'}",
                    }
                )
            return PreparedOperationsAction(
                action_type="admin_product_profile",
                title="确认修改商品基础资料",
                summary=(
                    "确认后会更新商品当前版本并写平台审计。在售商品的历史订单快照不会被改写。"
                ),
                target_label=f"{store.store_name} · {product.product_name}",
                payload={
                    "store_no": store.store_no,
                    "product_no": product.product_no,
                    **changes,
                },
                resource_versions={"store": store.version, "product": product.version},
                changes=tuple(display_changes),
                tool_code="governance.catalog.update.commit",
            ), None
        if _requests_admin_product_review(value):
            decision, reason, parse_error = _admin_product_review_decision(value)
            if parse_error is not None or decision is None:
                return None, parse_error or "请明确审核决定。"
            if product.product_status != "pending_review":
                return None, (
                    f"“{product.product_name}”当前为“{_product_status_label(product.product_status)}”，"
                    "只有审核中的商品可以作出平台审核决定。"
                )
            decision_label = {
                "approve": "审核通过并发布",
                "reject": "驳回",
                "request_changes": "要求修改",
            }[decision]
            return PreparedOperationsAction(
                action_type="admin_product_review",
                title=f"确认{decision_label}商品",
                summary=(
                    "确认后会按商品版本写审核证据。审核通过将继续执行发布前完整性复核并上架。"
                    "驳回或要求修改会回到商家整改状态。"
                ),
                target_label=f"{store.store_name} · {product.product_name}",
                payload={
                    "store_no": store.store_no,
                    "product_no": product.product_no,
                    "decision": decision,
                    "reason": reason,
                },
                resource_versions={"store": store.version, "product": product.version},
                changes=(
                    {"label": "当前状态", "value": "审核中"},
                    {"label": "审核决定", "value": decision_label},
                    {"label": "审核说明", "value": reason},
                ),
                tool_code="governance.catalog.review.commit",
            ), None
        if _requests_product_delete(value):
            has_transactions = bool(
                await session.scalar(
                    select(OrderItem.id).where(OrderItem.product_id == product.id).limit(1)
                )
            )
            if has_transactions:
                if product.product_status != "on_sale":
                    return (
                        None,
                        f"“{product.product_name}”已经产生过交易，不能永久删除。"
                        f"商品当前为“{_product_status_label(product.product_status)}”，无需再次下架。",
                    )
                return PreparedOperationsAction(
                    action_type="admin_product_status",
                    title="该商品有交易记录，只能确认下架",
                    summary="永久删除已被阻止。确认后平台仅停止新的购买，并保留历史订单、售后和审计数据。",
                    target_label=f"{store.store_name} · {product.product_name}",
                    payload={
                        "store_no": store.store_no,
                        "product_no": product.product_no,
                        "target_status": "off_shelf",
                    },
                    resource_versions={"store": store.version, "product": product.version},
                    changes=(
                        {"label": "删除资格", "value": "已有交易，不允许永久删除"},
                        {"label": "可执行操作", "value": "平台下架商品"},
                    ),
                    tool_code="governance.catalog.status.commit",
                ), None
            return PreparedOperationsAction(
                action_type="admin_product_delete",
                title="确认永久删除平台商品",
                summary="该商品尚未产生交易。确认后会永久移出商品列表并停用全部款式，同时记录平台审计。",
                target_label=f"{store.store_name} · {product.product_name}",
                payload={"store_no": store.store_no, "product_no": product.product_no},
                resource_versions={"store": store.version, "product": product.version},
                changes=(
                    {"label": "交易记录", "value": "无"},
                    {"label": "执行结果", "value": "永久删除商品并停用全部款式"},
                ),
                tool_code="governance.catalog.delete.commit",
            ), None
        product_target_status = "off_shelf" if "下架" in compact else "on_sale"
        if product.product_status == product_target_status:
            return None, (
                f"“{product.product_name}”当前已经是"
                f"“{_product_status_label(product_target_status)}”。"
            )
        if (product.product_status, product_target_status) not in {
            ("on_sale", "off_shelf"),
            ("off_shelf", "on_sale"),
        }:
            return None, f"“{product.product_name}”当前状态不能直接切换。"
        if product_target_status == "on_sale" and store.store_status != "active":
            return None, "目标店铺当前未营业，不能上架商品。"
        return PreparedOperationsAction(
            action_type="admin_product_status",
            title="确认变更平台商品销售状态",
            summary="平台操作会写入商品状态流水和审计记录，历史订单不受影响。",
            target_label=f"{store.store_name} · {product.product_name}",
            payload={
                "store_no": store.store_no,
                "product_no": product.product_no,
                "target_status": product_target_status,
            },
            resource_versions={"store": store.version, "product": product.version},
            changes=(
                {"label": "当前状态", "value": _product_status_label(product.product_status)},
                {"label": "变更后", "value": _product_status_label(product_target_status)},
            ),
            tool_code="governance.catalog.status.commit",
        ), None
    return None, "我识别到管理写操作，但目标不够明确。请提供唯一的用户名、店铺名或商品名。"


async def _prepare_refund_decision(
    session: AsyncSession, context: TrustedOperationsContext, value: str
) -> tuple[PreparedOperationsAction | None, str | None]:
    statement = (
        select(RefundApplication, Order, User, Store)
        .join(Order, Order.id == RefundApplication.order_id)
        .join(User, User.id == RefundApplication.user_id)
        .join(Store, Store.id == RefundApplication.store_id)
        .where(RefundApplication.refund_status.in_(("submitted", "merchant_review")))
        .order_by(RefundApplication.submitted_at.desc(), RefundApplication.id.desc())
    )
    if context.audience == "merchant":
        if context.store is None:
            return None, "当前商家店铺身份已经失效。"
        statement = statement.where(RefundApplication.store_id == context.store.id)
    rows = list((await session.execute(statement.limit(30))).all())
    compact = _compact(value)
    matched = [
        row
        for row in rows
        if row[0].refund_no in value
        or row[1].order_no in value
        or _mentions_username(row[2].username, value)
        or _compact(row[3].store_name) in compact
    ]
    selected = (
        matched[0]
        if len(matched) == 1
        else rows[0]
        if len(rows) == 1 or (_uses_recent_reference(value) and rows)
        else None
    )
    if selected is None:
        if not rows:
            return None, "当前授权范围内没有待审核的售后申请。"
        if len(matched) > 1:
            return None, "匹配到多条待审核售后，请提供售后申请 ID 或订单 ID。"
        return None, "请提供售后申请 ID、订单 ID，或唯一的顾客用户名。"
    refund, order, customer, store = selected
    request_more_info = any(
        marker in compact
        for marker in ("补充材料", "补材料", "补充凭证", "补凭证", "提供材料", "提供凭证")
    )
    decision = (
        "request_more_info"
        if request_more_info
        else "reject"
        if any(marker in compact for marker in ("拒绝", "驳回"))
        else "approve"
    )
    if decision == "request_more_info":
        requirements = _refund_more_info_requirements(value)
        if requirements is None:
            return None, (
                "请明确顾客需要补充的材料，例如“要求该售后补充材料：商品问题照片和包装照片”。"
            )
        action_prefix = "merchant" if context.audience == "merchant" else "admin"
        scope_label = "本店" if context.audience == "merchant" else "平台"
        return PreparedOperationsAction(
            action_type=f"{action_prefix}_refund_more_info",
            title="确认要求顾客补充售后材料",
            summary=(
                f"确认后会以{scope_label}审核身份把具体补充要求写入售后不可变事件，"
                "并在顾客与本店的消息会话中发送系统通知；不会批准、拒绝或退款。"
            ),
            target_label=f"{customer.username} · {store.store_name}",
            payload={
                "refund_no": refund.refund_no,
                "required_materials": requirements,
                "order_no": order.order_no,
            },
            resource_versions={
                "refund": refund.version,
                "order": order.version,
                "store": store.version,
            },
            changes=(
                {"label": "顾客", "value": customer.username},
                {"label": "订单", "value": order.order_no},
                {"label": "售后申请", "value": refund.refund_no},
                {"label": "需要补充", "value": requirements},
                {"label": "本次不会发生", "value": "不会批准、拒绝或发起退款"},
            ),
            tool_code=(
                "store_ops.after_sale.request_more_info.commit"
                if context.audience == "merchant"
                else "governance.after_sale.request_more_info.commit"
            ),
        ), None
    reason = _refund_decision_reason(value)
    if decision == "reject" and reason is None:
        return None, "拒绝售后必须说明具体原因，例如“拒绝退款，原因: 商品已超过售后期限”。"
    reason = reason or "审核资料符合当前售后规则，同意顾客申请"
    action_prefix = "merchant" if context.audience == "merchant" else "admin"
    scope_label = "本店" if context.audience == "merchant" else "平台"
    decision_label = "同意" if decision == "approve" else "拒绝"
    return PreparedOperationsAction(
        action_type=f"{action_prefix}_refund_decision",
        title=f"确认{decision_label}售后申请",
        summary=(
            f"确认后将以{scope_label}审核身份处理该申请，并写入售后状态流水。"
            "若金额达到双人复核阈值，只会创建平台复核任务，不会直接退款。"
        ),
        target_label=f"{customer.username} · {store.store_name}",
        payload={
            "refund_no": refund.refund_no,
            "decision": decision,
            "reason": reason,
            "order_no": order.order_no,
        },
        resource_versions={
            "refund": refund.version,
            "order": order.version,
            "store": store.version,
        },
        changes=(
            {"label": "顾客", "value": customer.username},
            {"label": "订单", "value": order.order_no},
            {"label": "申请金额", "value": _money(refund.requested_amount)},
            {"label": "处理决定", "value": decision_label},
            {"label": "处理说明", "value": reason},
        ),
        tool_code=(
            "store_ops.after_sale.decide.commit"
            if context.audience == "merchant"
            else "governance.after_sale.decide.commit"
        ),
    ), None


async def _prepare_support_action(
    session: AsyncSession, context: TrustedOperationsContext, value: str
) -> tuple[PreparedOperationsAction | None, str | None]:
    compact = _compact(value)
    action_kind = (
        "resolve"
        if _requests_support_resolve(value)
        else "claim"
        if _requests_support_claim(value)
        else "reply"
    )
    statement = (
        select(HumanServiceTicket, Conversation, User, Store)
        .join(Conversation, Conversation.id == HumanServiceTicket.conversation_id)
        .join(User, User.id == HumanServiceTicket.user_id)
        .outerjoin(Store, Store.id == Conversation.store_id)
        .where(
            HumanServiceTicket.ticket_status.in_(("queued", "assigned", "active", "waiting_user"))
        )
        .order_by(HumanServiceTicket.created_at.desc(), HumanServiceTicket.id.desc())
    )
    if context.audience == "merchant":
        if context.store is None:
            return None, "当前商家店铺身份已经失效。"
        statement = statement.where(Conversation.store_id == context.store.id)
    else:
        statement = statement.where(Conversation.conversation_type == "exclusive")
    rows = list((await session.execute(statement.limit(40))).all())
    matched = [
        row
        for row in rows
        if row[0].ticket_no.casefold() in compact
        or row[1].conversation_no.casefold() in compact
        or _mentions_username(row[2].username, value)
        or (row[3] is not None and _compact(row[3].store_name) in compact)
    ]
    if not matched and len(rows) == 1:
        matched = rows
    if len(matched) != 1:
        if not rows:
            return None, "当前权限范围内没有可处理的人工服务工单。"
        return None, "请提供唯一的顾客用户名、商家名称、工单 ID 或会话 ID。"
    ticket, conversation, participant, store = matched[0]
    changes: tuple[dict[str, str], ...]
    if action_kind == "claim":
        if ticket.ticket_status not in {"queued", "assigned"}:
            return None, "该工单当前不需要领取或接入。"
        if (
            ticket.ticket_status == "assigned"
            and ticket.current_assignee_user_id != context.user.id
        ):
            return None, "该工单已经分配给其他客服，不能由当前账号直接接入。"
        title = "确认接入人工服务"
        summary = "确认后当前运营人员将成为该工单处理人，AI 会继续保留会话上下文供人工核对。"
        tool_code = (
            "store_ops.support.claim.commit"
            if context.audience == "merchant"
            else "governance.support.claim.commit"
        )
        changes = (
            {"label": "当前状态", "value": ticket.ticket_status},
            {"label": "执行后", "value": "人工服务处理中"},
        )
    elif action_kind == "resolve":
        if ticket.current_assignee_user_id != context.user.id or ticket.ticket_status not in {
            "active",
            "waiting_user",
        }:
            return None, "只有当前工单处理人可以结束正在进行的人工服务。"
        title = "确认结束人工服务"
        summary = "确认后会结束人工接管、恢复 AI 服务，并向对方展示问题是否解决的反馈按钮。"
        tool_code = (
            "store_ops.support.resolve.commit"
            if context.audience == "merchant"
            else "governance.support.resolve.commit"
        )
        changes = (
            {"label": "当前状态", "value": ticket.ticket_status},
            {"label": "执行后", "value": "人工服务已结束，AI 恢复"},
        )
    else:
        if ticket.current_assignee_user_id != context.user.id or ticket.ticket_status != "active":
            return None, "请先领取并接入该人工工单，再让 AI 准备发送回复。"
        attachment_kind = _support_attachment_kind(value)
        attachment_payload: dict[str, object] = {}
        attachment_versions: dict[str, object] = {}
        if attachment_kind == "order":
            order_statement = (
                select(Order)
                .where(Order.user_id == participant.id)
                .order_by(Order.created_at.desc(), Order.id.desc())
            )
            if context.audience == "merchant":
                assert context.store is not None
                order_statement = order_statement.where(Order.store_id == context.store.id)
            orders = list((await session.scalars(order_statement.limit(30))).all())
            selected_order = _match_support_order(orders, value)
            if selected_order is None:
                return None, (
                    "没有找到唯一可发送的订单。请写明订单 ID。如果该顾客只有一笔本店订单，"
                    "也可以说“给顾客发送最近订单卡片”。"
                )
            attachment_payload = {
                "reply_kind": "order_card",
                "order_no": selected_order.order_no,
            }
            attachment_versions = {"order": selected_order.version}
            title = "确认发送订单卡片"
            summary = "确认后会把该顾客本人的订单卡片发送到当前人工会话，不会改变订单状态。"
            changes = (
                {"label": "消息类型", "value": "可点击订单卡片"},
                {"label": "订单", "value": selected_order.order_no},
            )
        elif attachment_kind == "product":
            product_statement = select(Product).where(
                Product.product_status == "on_sale",
                Product.deleted_at.is_(None),
            )
            if context.audience == "merchant":
                assert context.store is not None
                product_statement = product_statement.where(Product.store_id == context.store.id)
            products = list(
                (
                    await session.scalars(
                        product_statement.order_by(Product.sales_count.desc(), Product.id).limit(50)
                    )
                ).all()
            )
            selected_product = _match_support_product(products, value)
            if selected_product is None:
                return None, "没有找到唯一可发送的在售商品。请写明商品名称或商品 ID。"
            attachment_payload = {
                "reply_kind": "product_card",
                "product_no": selected_product.product_no,
            }
            attachment_versions = {"product": selected_product.version}
            title = "确认发送商品卡片"
            summary = "确认后会把仍在售的商品卡片发送到当前人工会话，不会创建订单或加入购物车。"
            changes = (
                {"label": "消息类型", "value": "可点击商品卡片"},
                {"label": "商品", "value": selected_product.product_name},
            )
        else:
            reply_text = _support_reply_content(value)
            if reply_text is None:
                return None, (
                    "请用“回复内容: ……”明确写出要发送给对方的文字，"
                    "或明确要求发送某个商品/订单卡片。"
                )
            attachment_payload = {"reply_kind": "text", "reply_text": reply_text}
            title = "确认发送人工客服消息"
            summary = "这是代表当前店铺或平台发送给对方的真实消息，确认前可以取消并重新编辑。"
            changes = ({"label": "回复内容", "value": reply_text},)
        tool_code = (
            "store_ops.conversations.send_message.commit"
            if context.audience == "merchant"
            else "governance.support.send_message.commit"
        )
    target_label = (
        f"{participant.username} · {store.store_name}"
        if store is not None
        else participant.username
    )
    payload: dict[str, object] = {
        "ticket_no": ticket.ticket_no,
        "conversation_no": conversation.conversation_no,
        "participant_no": participant.user_no,
        "target_label": target_label,
    }
    if action_kind == "reply":
        payload.update(attachment_payload)
    return PreparedOperationsAction(
        action_type=f"{context.audience}_support_{action_kind}",
        title=title,
        summary=summary,
        target_label=target_label,
        payload=payload,
        resource_versions={
            "ticket": ticket.version,
            "conversation": conversation.version,
            **(attachment_versions if action_kind == "reply" else {}),
        },
        changes=changes,
        tool_code=tool_code,
    ), None


async def _execute_action(
    session: AsyncSession,
    context: TrustedOperationsContext,
    approval: AgentToolApproval,
    action: AgentToolAction,
    payload: dict[str, object],
    *,
    postgres: AsyncSession | None = None,
) -> tuple[str, dict[str, object], str]:
    expected = approval.resource_versions
    action_type = approval.action_type
    now = utc_now()
    if action_type == "admin_ai_agent_prompt_draft_create":
        if context.audience != "admin":
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "只有平台 AI 管家可以创建 Agent Prompt 草稿。"
            )
        agent_no = payload.get("agent_no")
        base_version_no = payload.get("base_version_no")
        next_version_no = payload.get("next_version_no")
        system_prompt = payload.get("system_prompt")
        model_profile = payload.get("model_profile")
        tool_allowlist = payload.get("tool_allowlist")
        policy_config = payload.get("policy_config")
        if (
            not isinstance(agent_no, str)
            or not isinstance(base_version_no, int)
            or not isinstance(next_version_no, int)
            or next_version_no != base_version_no + 1
            or not isinstance(system_prompt, str)
            or not 20 <= len(system_prompt) <= 50000
            or not isinstance(model_profile, str)
            or not isinstance(tool_allowlist, list)
            or not all(isinstance(item, str) for item in tool_allowlist)
            or not isinstance(policy_config, dict)
        ):
            raise OperationsActionConflict(
                "AGENT_APPROVAL_ARGUMENTS_MISMATCH", "Agent Prompt 草稿参数不完整。"
            )
        await _operations_admin_access(session, context, "ai_agents:manage")
        definition = await session.scalar(
            select(AgentDefinition)
            .where(AgentDefinition.agent_no == agent_no)
            .with_for_update()
        )
        if definition is None:
            raise OperationsActionConflict("RESOURCE_NOT_FOUND", "目标 Agent 已经不存在。")
        _require_version(definition.version, expected.get("definition"))
        base_version = await session.scalar(
            select(AgentVersion)
            .where(
                AgentVersion.agent_id == definition.id,
                AgentVersion.version_no == base_version_no,
            )
            .with_for_update()
        )
        if base_version is None:
            raise OperationsActionConflict("RESOURCE_NOT_FOUND", "Agent 基准版本已经不存在。")
        _require_version(base_version.version, expected.get("base_version"))
        latest_version_no = int(
            await session.scalar(
                select(func.max(AgentVersion.version_no)).where(
                    AgentVersion.agent_id == definition.id
                )
            )
            or 0
        )
        if (
            latest_version_no != base_version_no
            or expected.get("base_version_no") != base_version_no
            or expected.get("next_version_no") != next_version_no
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED",
                "Agent 已产生更新版本，请重新发起以基于最新版本生成草稿。",
            )
        created = AgentVersion(
            agent_id=definition.id,
            version_no=next_version_no,
            version_status="draft",
            system_prompt=system_prompt,
            model_profile=model_profile,
            tool_allowlist=list(tool_allowlist),
            policy_config=dict(policy_config),
            published_at=None,
        )
        session.add(created)
        definition.version += 1
        _admin_audit(
            session,
            context,
            action_type,
            "agent_version",
            f"{definition.agent_no}:v{next_version_no}",
            {
                "base_version_no": base_version_no,
                "prompt_sha256": hashlib.sha256(
                    base_version.system_prompt.encode("utf-8")
                ).hexdigest(),
            },
            {
                "version_no": next_version_no,
                "version_status": "draft",
                "prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
                "evaluation_required": True,
                "published_version_changed": False,
            },
        )
        await session.flush()
        return (
            f"{definition.display_name} 的 Prompt 草稿 v{next_version_no} 已创建；"
            "线上版本未改变。请先完成评估，再单独发起发布审批。",
            {
                "agent_id": definition.agent_no,
                "agent_code": definition.agent_code,
                "version_no": next_version_no,
                "status": "draft",
                "evaluation_required": True,
                "published_version_changed": False,
            },
            f"{definition.agent_no}:v{next_version_no}",
        )
    if action_type == "admin_ai_evaluation_run":
        if context.audience != "admin":
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "只有平台 AI 管家可以启动发布准入评估。"
            )
        if (
            expected.get("dataset_version") != DATASET_VERSION
            or expected.get("baseline_version") != BASELINE_VERSION
            or expected.get("candidate_version") != CANDIDATE_VERSION
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "评估数据集或对比版本已经变化，请重新发起。"
            )
        evaluation_payload = _evaluation_run_payload(payload)
        access = await _operations_admin_access(session, context, "ai_evaluations:run")
        try:
            evaluation = await EvaluationService(session).create(
                access, evaluation_payload, action.idempotency_key
            )
        except Exception as exc:
            raise _service_conflict(exc, "AI 评估任务创建失败，未产生重复任务。") from exc
        return (
            f"AI 评估任务 {evaluation.evaluation_id} 已进入队列；评估完成前不会改变发布状态。",
            {
                "evaluation_id": evaluation.evaluation_id,
                "status": evaluation.status,
                "dataset_id": evaluation.dataset_id,
                "dataset_version": evaluation.dataset_version,
                "baseline_version": evaluation.baseline_version,
                "candidate_version": evaluation.candidate_version,
                "release_gate": evaluation.release_gate,
            },
            evaluation.evaluation_id,
        )
    if action_type in {
        "admin_ai_agent_publish_request",
        "admin_ai_skill_publish_request",
        "admin_ai_tool_publish_request",
    }:
        if context.audience != "admin":
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "只有平台 AI 管家可以发起 AI 版本发布审批。"
            )
        version_no = payload.get("version_no")
        entity_no = payload.get("entity_no")
        if not isinstance(version_no, int) or version_no < 1 or not isinstance(entity_no, str):
            raise OperationsActionConflict(
                "AGENT_APPROVAL_ARGUMENTS_MISMATCH", "AI 版本发布目标不完整。"
            )
        publication_service = AiPublicationService(
            session, get_settings(), SecurityService(get_settings())
        )
        idempotency_key = f"agent-ai-publication-{approval.approval_no}"
        try:
            if action_type == "admin_ai_agent_publish_request":
                agent_publication_row = (
                    await session.execute(
                        select(AgentDefinition, AgentVersion)
                        .join(AgentVersion, AgentVersion.agent_id == AgentDefinition.id)
                        .where(
                            AgentDefinition.agent_no == entity_no,
                            AgentVersion.version_no == version_no,
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if agent_publication_row is None:
                    raise OperationsActionConflict(
                        "RESOURCE_NOT_FOUND", "目标 Agent 版本已经不存在。"
                    )
                _require_version(agent_publication_row[0].version, expected.get("definition"))
                _require_version(agent_publication_row[1].version, expected.get("publish_version"))
                access = await _operations_admin_access(session, context, "ai_agents:publish")
                request = await publication_service.request_agent(
                    access, entity_no, version_no, idempotency_key
                )
                entity_code = agent_publication_row[0].agent_code
                entity_kind = "Agent"
            elif action_type == "admin_ai_skill_publish_request":
                skill_publication_row = (
                    await session.execute(
                        select(SkillDefinition, SkillVersion)
                        .join(SkillVersion, SkillVersion.skill_id == SkillDefinition.id)
                        .where(
                            SkillDefinition.skill_no == entity_no,
                            SkillVersion.version_no == version_no,
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if skill_publication_row is None:
                    raise OperationsActionConflict(
                        "RESOURCE_NOT_FOUND", "目标 Skill 版本已经不存在。"
                    )
                _require_version(skill_publication_row[0].version, expected.get("definition"))
                _require_version(skill_publication_row[1].version, expected.get("publish_version"))
                access = await _operations_admin_access(session, context, "ai_skills:publish")
                request = await publication_service.request_skill(
                    access, entity_no, version_no, idempotency_key
                )
                entity_code = skill_publication_row[0].skill_code
                entity_kind = "Skill"
            else:
                tool_publication_row = (
                    await session.execute(
                        select(ToolDefinition, ToolVersion)
                        .join(ToolVersion, ToolVersion.tool_id == ToolDefinition.id)
                        .where(
                            ToolDefinition.tool_code == entity_no,
                            ToolVersion.version_no == version_no,
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if tool_publication_row is None:
                    raise OperationsActionConflict(
                        "RESOURCE_NOT_FOUND", "目标 Tool 版本已经不存在。"
                    )
                _require_version(tool_publication_row[0].version, expected.get("definition"))
                _require_version(tool_publication_row[1].version, expected.get("publish_version"))
                access = await _operations_admin_access(session, context, "ai_tools:publish")
                request = await publication_service.request_tool(
                    access, entity_no, version_no, idempotency_key
                )
                entity_code = tool_publication_row[0].tool_code
                entity_kind = "Tool"
        except OperationsActionConflict:
            raise
        except Exception as exc:
            raise _service_conflict(exc, "创建 AI 版本发布审批失败，线上版本没有变化。") from exc
        return (
            (
                f"{entity_kind} 版本发布申请已创建，但尚未发布。"
                "请由两名不同管理员完成复核，发起人不能自批。"
            ),
            {
                "status": "approval_required",
                "ai_entity_type": entity_kind,
                "ai_entity_code": entity_code,
                "version_no": version_no,
                "approval_request_id": request.approval_request_id,
                "required_approval_count": request.required_approval_count,
                "approved_count": request.approved_count,
            },
            request.approval_request_id,
        )
    if action_type in {
        "admin_knowledge_document_publish",
        "admin_knowledge_document_withdraw",
    }:
        if context.audience != "admin":
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "只有平台 AI 管家可以治理知识文档。"
            )
        if postgres is None:
            raise OperationsActionConflict(
                "KNOWLEDGE_POSTGRES_UNAVAILABLE",
                "知识索引数据库当前不可用，本次没有发布、重建或撤回文档。",
            )
        document_no = payload.get("document_no")
        document = await session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.document_no == document_no)
            .with_for_update()
        )
        if document is None:
            raise OperationsActionConflict("RESOURCE_NOT_FOUND", "知识文档已经不存在。")
        _require_version(document.version, expected.get("knowledge_document"))
        permission = (
            "knowledge:publish"
            if action_type == "admin_knowledge_document_publish"
            else "knowledge:manage"
        )
        access = await _operations_admin_access(session, context, permission)
        knowledge_service = KnowledgeDocumentService(session, postgres)
        try:
            if action_type == "admin_knowledge_document_publish":
                view = await knowledge_service.publish(
                    access, document.document_no, action.idempotency_key
                )
                return (
                    "知识文档已发布，影子索引任务已经创建。旧索引会继续服务到新索引成功切换。",
                    {
                        "status": view.status,
                        "knowledge_document_id": view.document_id,
                        "title": document.title,
                        "content_version": view.content_version,
                        "index_job_id": view.index_job_no,
                        "index_status": view.index_status,
                    },
                    view.document_id,
                )
            view = await knowledge_service.withdraw(access, document.document_no)
            return (
                "知识文档已撤回，并已从当前检索范围移除。历史审计记录仍然保留。",
                {
                    "status": view.status,
                    "knowledge_document_id": view.document_id,
                    "title": document.title,
                    "content_version": view.content_version,
                },
                view.document_id,
            )
        except OperationsActionConflict:
            raise
        except Exception as exc:
            raise _service_conflict(
                exc, "知识文档治理操作失败，本次没有产生新的有效检索版本。"
            ) from exc
    if action_type == "admin_dead_letter_replay_request":
        if context.audience != "admin":
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "只有平台 AI 管家可以提交死信重放审批。"
            )
        dead_letter = await session.scalar(
            select(DeadLetterEvent)
            .where(DeadLetterEvent.dead_letter_no == payload.get("dead_letter_no"))
            .with_for_update()
        )
        if dead_letter is None:
            raise OperationsActionConflict("RESOURCE_NOT_FOUND", "目标死信已经不存在。")
        _require_version(dead_letter.version, expected.get("dead_letter"))
        source = await session.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.event_no == dead_letter.source_no)
            .with_for_update()
        )
        if source is None or source.aggregate_version != expected.get("source_aggregate"):
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "原始事件在确认前已经变化，请重新发起。"
            )
        reason = payload.get("reason")
        reason_code = payload.get("reason_code")
        if not isinstance(reason, str) or len(reason) < 5 or not isinstance(reason_code, str):
            raise OperationsActionConflict(
                "AGENT_APPROVAL_ARGUMENTS_MISMATCH", "重放申请原因不完整，本次没有执行。"
            )
        access = await _operations_admin_access(session, context, "events:operate")
        dead_letter_service = DeadLetterService(session, SecurityService(get_settings()))
        try:
            preview = await dead_letter_service.preview(
                access, dead_letter.dead_letter_no, commit=False
            )
            if not preview.replayable:
                raise OperationsActionConflict(
                    "DEAD_LETTER_NOT_REPLAYABLE", "死信当前不满足安全重放条件。"
                )
            request = await dead_letter_service.request_replay(
                access,
                dead_letter.dead_letter_no,
                DeadLetterReplayRequest(
                    preview_token=preview.preview_token,
                    reason_code=reason_code,
                    reason=reason,
                ),
                dead_letter.version,
                f"agent-dead-letter-replay-{approval.approval_no}",
                commit=False,
            )
        except OperationsActionConflict:
            raise
        except Exception as exc:
            raise _service_conflict(exc, "创建死信重放审批失败，本次没有重新投递事件。") from exc
        return (
            "死信重放申请已创建，但事件尚未重放。请由两名不同管理员完成复核，发起人不能自批。",
            {
                "status": "approval_required",
                "dead_letter_id": dead_letter.dead_letter_no,
                "event_type": dead_letter.event_type,
                "approval_request_id": request.approval_request_id,
                "required_approval_count": request.required_approval_count,
                "approved_count": request.approved_count,
            },
            request.approval_request_id,
        )
    if action_type == "merchant_store_policy_manage":
        if context.audience != "merchant" or context.store is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "当前店铺经营身份已经失效。"
            )
        store = await session.scalar(
            select(Store).where(Store.store_no == payload.get("store_no")).with_for_update()
        )
        if store is None or store.id != context.store.id:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标政策不再属于当前店铺。"
            )
        _require_version(store.version, expected.get("store"))
        command = str(payload.get("command") or "")
        if command not in {"create", "update", "publish", "withdraw"}:
            raise OperationsActionConflict(
                "AGENT_APPROVAL_ARGUMENTS_MISMATCH", "政策操作类型无效。"
            )
        permission_code = {
            "create": "store_policies:create",
            "update": "store_policies:update",
            "publish": "store_policies:publish",
            "withdraw": "store_policies:publish",
        }[command]
        access = await _operations_admin_access(session, context, permission_code)
        policy_service = AdminStoreService(session, get_settings())
        try:
            if command == "create":
                policy_type = payload.get("policy_type")
                title = payload.get("policy_title")
                content = payload.get("content")
                if not all(
                    isinstance(item, str) and item for item in (policy_type, title, content)
                ):
                    raise OperationsActionConflict(
                        "AGENT_APPROVAL_ARGUMENTS_MISMATCH", "政策草稿字段不完整。"
                    )
                policy_result = await policy_service.create_policy(
                    access,
                    store.store_no,
                    AdminStorePolicyCreateRequest(
                        policy_type=str(policy_type),
                        title=str(title),
                        content=str(content),
                        effective_at=now,
                    ),
                    f"agent-policy-create-{approval.approval_no}",
                    commit=False,
                )
            else:
                policy = await session.scalar(
                    select(StoreServicePolicy)
                    .where(
                        StoreServicePolicy.store_id == store.id,
                        StoreServicePolicy.policy_no == payload.get("policy_no"),
                    )
                    .with_for_update()
                )
                if policy is None:
                    raise OperationsActionConflict("RESOURCE_NOT_FOUND", "目标政策已经不存在。")
                _require_version(policy.version, expected.get("policy"))
                if command == "update":
                    policy_title = payload.get("policy_title")
                    policy_content = payload.get("content")
                    if isinstance(policy_title, str) and isinstance(policy_content, str):
                        update_request = AdminStorePolicyUpdateRequest(
                            title=policy_title, content=policy_content
                        )
                    elif isinstance(policy_title, str):
                        update_request = AdminStorePolicyUpdateRequest(title=policy_title)
                    elif isinstance(policy_content, str):
                        update_request = AdminStorePolicyUpdateRequest(content=policy_content)
                    else:
                        raise OperationsActionConflict(
                            "AGENT_APPROVAL_ARGUMENTS_MISMATCH", "政策草稿没有可修改的字段。"
                        )
                    policy_result = await policy_service.update_policy(
                        access,
                        store.store_no,
                        policy.policy_no,
                        update_request,
                        policy.version,
                        commit=False,
                    )
                elif command == "publish":
                    policy_result = await policy_service.publish_policy(
                        access,
                        store.store_no,
                        policy.policy_no,
                        AdminPolicyCommandRequest(reason="由店铺运营人员通过 AI 经营助理确认发布"),
                        policy.version,
                        f"agent-policy-publish-{approval.approval_no}",
                        commit=False,
                    )
                else:
                    policy_result = await policy_service.withdraw_policy(
                        access,
                        store.store_no,
                        policy.policy_no,
                        AdminPolicyCommandRequest(reason="由店铺运营人员通过 AI 经营助理确认撤回"),
                        policy.version,
                        f"agent-policy-withdraw-{approval.approval_no}",
                        commit=False,
                    )
        except OperationsActionConflict:
            raise
        except Exception as exc:
            raise _service_conflict(exc, "店铺政策操作失败，公开承诺没有变化。") from exc
        return (
            f"政策《{policy_result.title}》已{_policy_command_result_label(command)}，"
            "我已回读最新版本。",
            {
                "status": policy_result.status,
                "policy_id": policy_result.policy_id,
                "policy_type": policy_result.policy_type,
                "policy_title": policy_result.title,
                "policy_version": policy_result.policy_version,
                "version": policy_result.version,
            },
            policy_result.policy_id,
        )
    if action_type == "admin_user_avatar_update":
        if context.audience != "admin":
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "只有平台 AI 管家可以修改用户头像。"
            )
        target_user = await session.scalar(
            select(User)
            .where(User.user_no == payload.get("user_no"))
            .with_for_update()
        )
        if target_user is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标用户已经不存在。"
            )
        _require_version(target_user.version, expected.get("user"))
        file = await session.scalar(
            select(FileObject)
            .where(FileObject.file_no == payload.get("file_no"))
            .with_for_update()
        )
        if not _bindable_user_asset(file, target_user):
            raise OperationsActionConflict(
                "AGENT_ACTION_FILE_CHANGED",
                "头像状态、用途或用户归属已经变化，请重新上传或选择。",
            )
        assert file is not None
        _require_version(file.version, expected.get("file"))
        access = await _operations_admin_access(session, context, "users:manage")
        try:
            result = await RbacService(
                session, SecurityService(get_settings())
            ).update_user(
                access,
                target_user.user_no,
                AdminUserUpdateRequest(avatar_file_id=file.file_no),
                target_user.version,
            )
        except Exception as exc:
            raise _service_conflict(exc, "用户或头像状态已经变化，本次没有更新。") from exc
        return (
            f"用户“{result.username}”的头像已更新，我已回读最新版本。",
            {
                "user_id": result.user_id,
                "username": result.username,
                "image_url": f"/api/v1/files/{file.file_no}",
                "version": result.version,
                "status": result.account_status,
            },
            result.user_id,
        )
    if action_type in {"merchant_store_logo_update", "admin_store_logo_update"}:
        store = await session.scalar(
            select(Store).where(Store.store_no == payload.get("store_no")).with_for_update()
        )
        if (
            store is None
            or (
                action_type == "merchant_store_logo_update"
                and (
                    context.audience != "merchant"
                    or context.store is None
                    or context.store.id != store.id
                )
            )
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标店铺不再属于当前操作范围。"
            )
        if action_type == "admin_store_logo_update":
            await _operations_admin_access(session, context, "stores:manage")
        _require_version(store.version, expected.get("store"))
        file = await session.scalar(
            select(FileObject)
            .where(FileObject.file_no == payload.get("file_no"))
            .with_for_update()
        )
        if not _bindable_agent_asset(file, store, "store_logo"):
            raise OperationsActionConflict(
                "AGENT_ACTION_FILE_CHANGED",
                "图片状态、用途或店铺归属已经变化，请重新上传或选择。",
            )
        assert file is not None
        _require_version(file.version, expected.get("file"))
        before = {"logo_object_key": store.logo_object_key, "version": store.version}
        store.logo_object_key = file.object_key
        store.version += 1
        _outbox(
            session,
            context,
            "store.profile_updated.v1",
            "store",
            store.store_no,
            store.version,
            {"changed_fields": ["logo_object_key"], "file_id": file.file_no},
        )
        _admin_audit(
            session,
            context,
            action_type,
            "store",
            store.store_no,
            before,
            {"logo_file_id": file.file_no, "version": store.version},
        )
        return (
            f"“{store.store_name}”的店铺 Logo 已更新，我已回读最新版本。",
            {
                "store_id": store.store_no,
                "store_name": store.store_name,
                "logo_file_id": file.file_no,
                "image_url": f"/api/v1/files/{file.file_no}",
                "version": store.version,
                "status": store.store_status,
            },
            store.store_no,
        )
    if action_type in {
        "merchant_product_sku_image_replace",
        "admin_product_sku_image_replace",
    }:
        row = (
            await session.execute(
                select(Product, Store, ProductSku)
                .join(Store, Store.id == Product.store_id)
                .join(ProductSku, ProductSku.product_id == Product.id)
                .where(
                    Product.product_no == payload.get("product_no"),
                    Store.store_no == payload.get("store_no"),
                    ProductSku.sku_no == payload.get("sku_no"),
                    Product.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标店铺、商品或款式已经不存在。"
            )
        product, store, sku = row
        if (
            action_type == "merchant_product_sku_image_replace"
            and (
                context.audience != "merchant"
                or context.store is None
                or context.store.id != store.id
            )
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "只能修改当前店铺自己的商品款式图片。"
            )
        if action_type == "admin_product_sku_image_replace":
            await _operations_admin_access(session, context, "products:update")
        _require_version(store.version, expected.get("store"))
        _require_version(product.version, expected.get("product"))
        _require_version(sku.version, expected.get("sku"))
        if product.product_status not in {"draft", "rejected", "off_shelf", "on_sale"}:
            raise OperationsActionConflict(
                "AGENT_ACTION_STATE_CHANGED", "商品当前状态不允许修改款式图片。"
            )
        if sku.sku_status != "active":
            raise OperationsActionConflict(
                "AGENT_ACTION_STATE_CHANGED", "所选款式已经停用，不能继续绑定图片。"
            )
        file = await session.scalar(
            select(FileObject)
            .where(FileObject.file_no == payload.get("file_no"))
            .with_for_update()
        )
        if not _bindable_agent_asset(file, store, "product"):
            raise OperationsActionConflict(
                "AGENT_ACTION_FILE_CHANGED",
                "图片状态、用途或店铺归属已经变化，请重新上传或选择。",
            )
        assert file is not None
        _require_version(file.version, expected.get("file"))
        existing_images = list(
            await session.scalars(
                select(ProductImage)
                .where(ProductImage.product_id == product.id, ProductImage.sku_id == sku.id)
                .with_for_update()
            )
        )
        before_file_ids = [item.file_id for item in existing_images]
        for image in existing_images:
            await session.delete(image)
        await session.flush()
        image = ProductImage(
            product_id=product.id,
            sku_id=sku.id,
            file_id=file.id,
            object_key=file.object_key,
            image_type="spec",
            alt_text=f"{product.product_name} {sku.sku_name}"[:255],
            width=file.width or 0,
            height=file.height or 0,
            sort_order=0,
            image_status="active",
        )
        session.add(image)
        product.version += 1
        _outbox(
            session,
            context,
            "product.images_replaced.v1",
            "product",
            product.product_no,
            product.version,
            {"store_id": store.store_no, "sku_id": sku.sku_no, "file_id": file.file_no},
        )
        _admin_audit(
            session,
            context,
            action_type,
            "product_sku",
            sku.sku_no,
            {"file_row_ids": before_file_ids, "product_version": product.version - 1},
            {"file_id": file.file_no, "product_version": product.version},
        )
        return (
            f"“{product.product_name}”的“{sku.sku_name}”款式图片已替换，我已回读最新版本。",
            {
                "store_id": store.store_no,
                "store_name": store.store_name,
                "product_id": product.product_no,
                "product_name": product.product_name,
                "sku_id": sku.sku_no,
                "sku_name": sku.sku_name,
                "image_count": 1,
                "image_url": f"/api/v1/files/{file.file_no}",
                "file_id": file.file_no,
                "version": product.version,
                "status": product.product_status,
            },
            sku.sku_no,
        )
    if action_type == "merchant_store_profile":
        store = await session.scalar(
            select(Store).where(Store.store_no == payload.get("store_no")).with_for_update()
        )
        if (
            store is None
            or context.store is None
            or context.audience != "merchant"
            or store.id != context.store.id
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标店铺不再属于当前商家账号。"
            )
        _require_version(store.version, expected.get("store"))
        profile_before: dict[str, object] = {
            "store_name": store.store_name,
            "description": store.description,
        }
        requested_name = payload.get("store_name")
        if isinstance(requested_name, str):
            normalized_name = _normalize_store_name(requested_name)
            duplicate = await session.scalar(
                select(Store.id).where(
                    Store.store_name_normalized == normalized_name,
                    Store.id != store.id,
                )
            )
            if duplicate is not None:
                raise OperationsActionConflict(
                    "STORE_NAME_CONFLICT", "该店铺名称已经被使用，请重新选择名称。"
                )
            store.store_name = requested_name
            store.store_name_normalized = normalized_name
            store.store_name_changed_at = now
        if "description" in payload:
            description = payload.get("description")
            store.description = str(description) if description is not None else None
        store.version += 1
        profile_after: dict[str, object] = {
            "store_name": store.store_name,
            "description": store.description,
        }
        _outbox(
            session,
            context,
            "store.profile_updated.v1",
            "store",
            store.store_no,
            store.version,
            {
                "changed_fields": sorted(
                    key for key in profile_after if profile_before[key] != profile_after[key]
                )
            },
        )
        _admin_audit(
            session,
            context,
            action_type,
            "store",
            store.store_no,
            profile_before,
            profile_after,
        )
        return (
            f"“{store.store_name}”的公开资料已更新，我已回读最新版本。",
            {
                "store_id": store.store_no,
                "store_name": store.store_name,
                "description": store.description,
                "status": store.store_status,
                "version": store.version,
            },
            store.store_no,
        )

    if action_type in {
        "merchant_store_email_update",
        "admin_store_merchant_email_update",
    }:
        store = await session.scalar(
            select(Store).where(Store.store_no == payload.get("store_no")).with_for_update()
        )
        if store is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标店铺已经不存在或不再可操作。"
            )
        _require_version(store.version, expected.get("store"))
        expected_owner_id = expected.get("owner_user_id")
        if not isinstance(expected_owner_id, int) or store.owner_user_id != expected_owner_id:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "店铺所有者已经变化，请重新发起操作。"
            )
        if action_type.startswith("merchant_"):
            if (
                context.audience != "merchant"
                or context.store is None
                or context.store.id != store.id
                or context.user.id != store.owner_user_id
            ):
                raise OperationsActionConflict(
                    "AGENT_ACTION_SCOPE_CHANGED", "当前商家账号不能修改该店铺恢复邮箱。"
                )
        else:
            await _operations_admin_access(session, context, "stores:manage")
        credential = await _email_credential(
            session, store.owner_user_id, for_update=True
        )
        _require_version(
            credential.credential_version if credential is not None else 0,
            expected.get("credential"),
        )
        ciphertext_hex = str(payload.get("email_ciphertext") or "")
        try:
            new_email = SecurityService(get_settings()).decrypt(
                "agent-action:merchant-email", bytes.fromhex(ciphertext_hex)
            )
            normalized_email = normalize_target("email", new_email)
        except (TypeError, ValueError) as exc:
            raise OperationsActionConflict(
                "AGENT_APPROVAL_ARGUMENTS_MISMATCH", "恢复邮箱确认内容无法校验。"
            ) from exc
        current_email = _credential_email(credential)
        if credential is None:
            credential = UserCredential(
                user_id=store.owner_user_id,
                credential_type="email",
                is_primary=True,
                is_verified=False,
                credential_status="active",
                credential_version=1,
            )
            session.add(credential)
        else:
            credential.credential_version += 1
        security = SecurityService(get_settings())
        credential.identifier_ciphertext = security.encrypt(
            "user-credential:email", normalized_email
        )
        credential.identifier_hash = security.keyed_hash(
            "credential-identifier", normalized_email
        )
        credential.key_version = 1
        credential.is_primary = True
        credential.is_verified = False
        credential.verified_at = None
        await session.flush()
        if action_type.startswith("admin_"):
            _admin_audit(
                session,
                context,
                action_type,
                "store_merchant_email",
                store.store_no,
                {"email_masked": _mask_email(current_email or "")},
                {
                    "email_masked": _mask_email(normalized_email),
                    "verified": False,
                    "credential_version": credential.credential_version,
                },
            )
        _outbox(
            session,
            context,
            "store.merchant_email_updated.v1",
            "store",
            store.store_no,
            store.version,
            {
                "credential_version": credential.credential_version,
                "source": "agent_confirmed",
            },
        )
        return (
            f"“{store.store_name}”商家账号的恢复邮箱已更新为 {_mask_email(normalized_email)}。",
            {
                "store_id": store.store_no,
                "store_name": store.store_name,
                "merchant_email_masked": _mask_email(normalized_email),
                "email_verification_status": "待验证",
                "credential_version": credential.credential_version,
            },
            store.store_no,
        )

    if action_type in {"merchant_store_status", "admin_store_status"}:
        store = await session.scalar(
            select(Store).where(Store.store_no == payload.get("store_no")).with_for_update()
        )
        if store is None or (
            context.audience == "merchant"
            and (context.store is None or store.id != context.store.id)
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标店铺不再可访问，本次没有执行操作。"
            )
        _require_version(store.version, expected.get("store"))
        target = str(payload.get("target_status"))
        if (store.store_status, target) not in {("active", "suspended"), ("suspended", "active")}:
            raise OperationsActionConflict(
                "AGENT_ACTION_STATE_CHANGED", "店铺状态已经变化，请重新发起操作。"
            )
        if (
            context.audience == "merchant"
            and target == "active"
            and store.suspension_source == "platform"
        ):
            raise OperationsActionConflict(
                "STORE_PLATFORM_SUSPENSION_ACTIVE", "店铺仍由平台暂停，商家不能自行恢复。"
            )
        previous = store.store_status
        store.store_status = target
        store.suspended_at = now if target == "suspended" else None
        store.suspension_source = (
            ("merchant" if context.audience == "merchant" else "platform")
            if target == "suspended"
            else None
        )
        store.version += 1
        _outbox(
            session,
            context,
            f"store.{'suspended' if target == 'suspended' else 'resumed'}.v1",
            "store",
            store.store_no,
            store.version,
            {"from_status": previous, "to_status": target, "source": "agent_confirmed"},
        )
        _admin_audit(
            session,
            context,
            action_type,
            "store",
            store.store_no,
            {"status": previous},
            {"status": target},
        )
        return (
            f"“{store.store_name}”已切换为“{_store_status_label(target)}”。我已回读最新状态。",
            {
                "store_id": store.store_no,
                "store_name": store.store_name,
                "status": target,
                "version": store.version,
            },
            store.store_no,
        )

    if action_type == "merchant_product_draft_create":
        if context.store is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "当前商家店铺身份已经失效。"
            )
        store = await session.scalar(
            select(Store).where(Store.id == context.store.id).with_for_update()
        )
        category = await session.scalar(
            select(Category)
            .where(
                Category.category_no == payload.get("category_no"),
                Category.category_status == "active",
            )
            .with_for_update()
        )
        shipping_template = await session.scalar(
            select(ShippingTemplate)
            .where(
                ShippingTemplate.template_no == payload.get("shipping_template_no"),
                ShippingTemplate.store_id == context.store.id,
                ShippingTemplate.template_status == "effective",
            )
            .with_for_update()
        )
        if store is None or store.store_status != "active":
            raise OperationsActionConflict(
                "STORE_NOT_ACTIVE", "店铺状态已经变化，当前不能创建商品草稿。"
            )
        if category is None or shipping_template is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "平台分类或配送模板已经变化，请重新发起操作。"
            )
        _require_version(store.version, expected.get("store"))
        _require_version(category.version, expected.get("category"))
        _require_version(shipping_template.version, expected.get("shipping_template"))
        await _operations_admin_access(session, context, "products:create")
        product_name = str(payload.get("product_name") or "").strip()
        sku_name = str(payload.get("sku_name") or "").strip()
        price_minor = int(str(payload.get("price_minor") or 0))
        stock_quantity = int(str(payload.get("stock_quantity") or 0))
        origin_region_code = str(payload.get("origin_region_code") or "")
        dispatch_min_hours = int(str(payload.get("dispatch_min_hours") or 0))
        dispatch_max_hours = int(str(payload.get("dispatch_max_hours") or 0))
        if (
            not 1 <= len(product_name) <= 255
            or not 1 <= len(sku_name) <= 255
            or not 1 <= price_minor <= 999_999_999_999
            or not 0 <= stock_quantity <= 999_999_999
            or _region_code(origin_region_code) is None
            or not 0 <= dispatch_min_hours <= dispatch_max_hours <= 8760
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "草稿字段校验失败，本次没有创建商品。"
            )
        created_product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=store.id,
            category_id=category.id,
            brand_id=None,
            product_name=product_name,
            subtitle=None,
            description=None,
            product_status="draft",
            min_price_amount=price_minor,
            max_price_amount=price_minor,
            currency="CNY",
            version=1,
        )
        session.add(created_product)
        await session.flush()
        spec_values = [{"name": "款式", "value": sku_name}]
        normalized_specs = [{"name": "款式", "value": sku_name.strip().casefold()}]
        sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=created_product.id,
            store_id=store.id,
            merchant_sku_code=None,
            sku_name=sku_name,
            spec_values=spec_values,
            spec_signature=hashlib.sha256(
                json.dumps(normalized_specs, ensure_ascii=False, separators=(",", ":")).encode()
            ).digest(),
            sale_price_amount=price_minor,
            market_price_amount=price_minor,
            currency="CNY",
            weight_grams=None,
            barcode=None,
            sku_status="active",
            version=1,
        )
        session.add(sku)
        await session.flush()
        created_product.default_sku_id = sku.id
        inventory = Inventory(
            sku_id=sku.id,
            on_hand_quantity=stock_quantity,
            reserved_quantity=0,
            safety_stock_quantity=0,
            sold_quantity=0,
            inventory_status="active",
            version=1,
        )
        fulfillment = ProductFulfillmentProfile(
            product_id=created_product.id,
            shipping_template_id=shipping_template.id,
            origin_region_code=origin_region_code,
            dispatch_min_hours=dispatch_min_hours,
            dispatch_max_hours=dispatch_max_hours,
            purchase_notice=None,
            profile_version=1,
            version=1,
        )
        session.add_all((inventory, fulfillment))
        session.add(
            ProductStatusLog(
                product_id=created_product.id,
                from_status=None,
                to_status="draft",
                event_type="created",
                actor_type="merchant",
                actor_id=context.user.id,
                reason_code="MERCHANT_AGENT_DRAFT_CREATE",
                reason="商家通过 AI 经营助理确认创建结构化商品草稿",
                product_version=created_product.version,
                request_id=context.run.trace_id,
                trace_id=context.run.trace_id,
            )
        )
        _outbox(
            session,
            context,
            "product.created.v1",
            "product",
            created_product.product_no,
            created_product.version,
            {
                "store_id": store.store_no,
                "status": "draft",
                "sku_id": sku.sku_no,
                "source": "agent_confirmed",
            },
        )
        await session.flush()
        return (
            f"“{created_product.product_name}”已创建为草稿，首个款式和库存已保存。"
            "请打开编辑页补充款式图片与商品详情，再提交审核。",
            {
                "product_id": created_product.product_no,
                "product_name": created_product.product_name,
                "sku_id": sku.sku_no,
                "sku_name": sku.sku_name,
                "price_minor": price_minor,
                "on_hand_quantity": inventory.on_hand_quantity,
                "status": created_product.product_status,
                "origin_region_code": fulfillment.origin_region_code,
                "dispatch_min_hours": fulfillment.dispatch_min_hours,
                "dispatch_max_hours": fulfillment.dispatch_max_hours,
                "version": created_product.version,
            },
            created_product.product_no,
        )

    if action_type in {
        "merchant_product_detail_section_upsert",
        "merchant_product_detail_section_delete",
        "admin_product_detail_section_upsert",
        "admin_product_detail_section_delete",
    }:
        is_merchant_detail_action = action_type.startswith("merchant_")
        if is_merchant_detail_action and context.store is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "当前商家店铺身份已经失效。"
            )
        store = await session.scalar(
            select(Store)
            .where(
                Store.store_no
                == (
                    context.store.store_no
                    if is_merchant_detail_action and context.store is not None
                    else payload.get("store_no")
                )
            )
            .with_for_update()
        )
        product = await session.scalar(
            select(Product)
            .where(
                Product.product_no == payload.get("product_no"),
                Product.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if store is None or product is None or product.store_id != store.id:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标商品已经不在当前可管理范围内。"
            )
        _require_version(store.version, expected.get("store"))
        _require_version(product.version, expected.get("product"))
        if product.product_status not in {"draft", "rejected", "off_shelf", "on_sale"}:
            raise OperationsActionConflict(
                "PRODUCT_NOT_EDITABLE", "商品状态已经变化，当前不能修改详情。"
            )
        current_content = None
        if product.current_detail_content_version_id is not None:
            current_content = await session.scalar(
                select(ProductContentVersion)
                .where(
                    ProductContentVersion.id == product.current_detail_content_version_id,
                    ProductContentVersion.product_id == product.id,
                )
                .with_for_update()
            )
        _require_version(
            current_content.version if current_content is not None else 0,
            expected.get("content"),
        )
        expected_content_no = payload.get("current_content_version_no")
        if (
            current_content.content_version_no if current_content is not None else None
        ) != expected_content_no:
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "商品详情版本已变化，请重新发起操作。"
            )
        if current_content is not None and current_content.safe_blocks is None:
            raise OperationsActionConflict(
                "PRODUCT_CONTENT_FORMAT_CHANGED", "商品详情已变为非结构化格式，本次没有修改。"
            )
        blocks = (
            [dict(item) for item in (current_content.safe_blocks or [])] if current_content else []
        )
        title = _normalized_detail_section_title(str(payload.get("section_title") or ""))
        content_value = payload.get("content")
        content = str(content_value).strip() if isinstance(content_value, str) else None
        deleting = action_type.endswith("_delete")
        try:
            updated_blocks = _replace_detail_section_blocks(
                blocks,
                title=title,
                content=content,
                delete=deleting,
            )
        except LookupError as exc:
            raise OperationsActionConflict(
                "DETAIL_SECTION_NOT_FOUND", "目标详情段落已被删除，请重新查询。"
            ) from exc
        except ValueError as exc:
            raise OperationsActionConflict(
                "DETAIL_SECTION_AMBIGUOUS", "详情中出现多个同名段落，本次没有修改。"
            ) from exc
        if not updated_blocks:
            raise OperationsActionConflict("PRODUCT_CONTENT_EMPTY", "详情内容不能全部删空。")
        access = await _operations_admin_access(session, context, "products:update")
        try:
            content_result = await ProductAdminService(
                session, get_settings()
            ).create_content_version(
                access,
                product.product_no,
                AdminContentVersionCreateRequest(
                    source_format="structured",
                    source_content=json.dumps(updated_blocks, ensure_ascii=False),
                ),
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "商品详情已变化，请重新核对。") from exc
        if not is_merchant_detail_action:
            _admin_audit(
                session,
                context,
                action_type,
                "product",
                product.product_no,
                {
                    "content_version_id": expected_content_no,
                    "section_title": title,
                },
                {
                    "content_version_id": content_result.version_id,
                    "section_title": title,
                    "operation": "delete" if deleting else "upsert",
                },
            )
        verb = "已删除" if deleting else "已保存并发布新版本"
        return (
            f"商品详情段落“{title}”{verb}，原有图片顺序未改变。",
            {
                "store_id": store.store_no,
                "product_id": product.product_no,
                "product_name": product.product_name,
                "section_title": title,
                "section_content": content,
                "block_count": len(updated_blocks),
                "content_version_id": content_result.version_id,
                "content_version": content_result.content_version,
                "status": "deleted" if deleting else "published",
            },
            content_result.version_id,
        )

    if action_type in {
        "merchant_product_faq_upsert",
        "merchant_product_faq_delete",
        "admin_product_faq_upsert",
        "admin_product_faq_delete",
    }:
        is_merchant_faq_action = action_type.startswith("merchant_")
        if is_merchant_faq_action and context.store is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "当前商家店铺身份已经失效。"
            )
        store = await session.scalar(
            select(Store)
            .where(
                Store.store_no
                == (
                    context.store.store_no
                    if is_merchant_faq_action and context.store is not None
                    else payload.get("store_no")
                )
            )
            .with_for_update()
        )
        product = await session.scalar(
            select(Product)
            .where(
                Product.product_no == payload.get("product_no"),
                Product.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if store is None or product is None or product.store_id != store.id:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标商品已经不在当前店铺经营范围内。"
            )
        _require_version(store.version, expected.get("store"))
        _require_version(product.version, expected.get("product"))
        if product.product_status not in {"draft", "rejected", "off_shelf", "on_sale"}:
            raise OperationsActionConflict(
                "PRODUCT_NOT_EDITABLE", "商品状态已经变化，当前不能修改常见问题。"
            )
        access = await _operations_admin_access(session, context, "products:update")
        faq_rows = list(
            (
                await session.scalars(
                    select(ProductFaq)
                    .where(
                        ProductFaq.product_id == product.id,
                        ProductFaq.faq_status != "archived",
                    )
                    .order_by(ProductFaq.sort_order, ProductFaq.id)
                    .with_for_update()
                )
            ).all()
        )
        version_ids = [
            faq.current_content_version_id
            for faq in faq_rows
            if faq.current_content_version_id is not None
        ]
        faq_versions = {
            version.id: version
            for version in (
                await session.scalars(
                    select(ProductFaqVersion).where(ProductFaqVersion.id.in_(version_ids))
                )
            ).all()
        }
        target_faq_no = str(payload.get("faq_no") or "")
        selected_faq = next((faq for faq in faq_rows if faq.faq_no == target_faq_no), None)
        if target_faq_no:
            if selected_faq is None:
                raise OperationsActionConflict(
                    "FAQ_NOT_FOUND", "目标常见问题已经被删除或不属于该商品。"
                )
            _require_version(selected_faq.version, expected.get("faq"))
            target_version = (
                faq_versions.get(selected_faq.current_content_version_id)
                if selected_faq.current_content_version_id is not None
                else None
            )
            _require_version(
                target_version.version if target_version is not None else 0,
                expected.get("faq_content"),
            )

        replace_items: list[AdminFaqReplaceItem] = []
        for sort_order, faq in enumerate(faq_rows):
            if action_type.endswith("_faq_delete") and faq is selected_faq:
                continue
            current_version = (
                faq_versions.get(faq.current_content_version_id)
                if faq.current_content_version_id is not None
                else None
            )
            if current_version is None:
                raise OperationsActionConflict(
                    "FAQ_CONTENT_INVALID",
                    f"常见问题“{faq.question}”缺少可保存的当前回答，请先在商品编辑页修复。",
                )
            question = faq.question
            answer = current_version.safe_text
            if action_type.endswith("_faq_upsert") and faq is selected_faq:
                question = str(payload.get("question") or "").strip()
                answer = str(payload.get("answer") or "").strip()
            replace_items.append(
                AdminFaqReplaceItem(
                    faq_id=faq.faq_no,
                    question=question,
                    answer=answer,
                    sort_order=sort_order,
                )
            )

        if action_type.endswith("_faq_upsert") and selected_faq is None:
            question = _normalized_faq_question(str(payload.get("question") or ""))
            answer = str(payload.get("answer") or "").strip()
            if not question or not answer:
                raise OperationsActionConflict(
                    "AGENT_ACTION_ARGUMENT_INVALID", "常见问题或回答校验失败，本次没有保存。"
                )
            if any(_normalized_faq_question(faq.question) == question for faq in faq_rows):
                raise OperationsActionConflict(
                    "FAQ_ALREADY_EXISTS", "同名常见问题已经存在，请重新发起修改。"
                )
            replace_items.append(
                AdminFaqReplaceItem(
                    faq_id=None,
                    question=question,
                    answer=answer,
                    sort_order=len(replace_items),
                )
            )
        try:
            updated_faqs = await ProductAdminService(session, get_settings()).replace_faqs(
                access,
                product.product_no,
                AdminFaqReplaceRequest(items=replace_items),
                product.version,
            )
        except Exception as exc:
            raise _service_conflict(exc, "常见问题或商品版本已变化，请重新发起操作。") from exc
        target_question = str(payload.get("question") or "")
        updated_target = next(
            (
                item
                for item in updated_faqs
                if _normalized_faq_question(item.question)
                == _normalized_faq_question(target_question)
            ),
            None,
        )
        if action_type.endswith("_faq_delete"):
            return (
                f"常见问题“{target_question}”已从商品详情和店铺 AI "
                "公开知识中移除，旧版本仍保留供审计。",
                {
                    "product_id": product.product_no,
                    "store_id": store.store_no,
                    "product_name": product.product_name,
                    "question": target_question,
                    "faq_count": len(updated_faqs),
                    "status": "archived",
                    "version": product.version,
                },
                target_faq_no,
            )
        return (
            f"常见问题“{target_question}”已发布，顾客商品详情和店铺 AI 将使用新回答。",
            {
                "product_id": product.product_no,
                "store_id": store.store_no,
                "product_name": product.product_name,
                "faq_id": updated_target.faq_id if updated_target is not None else None,
                "question": target_question,
                "answer": str(payload.get("answer") or ""),
                "faq_count": len(updated_faqs),
                "status": updated_target.status if updated_target is not None else "published",
                "version": product.version,
            },
            updated_target.faq_id if updated_target is not None else product.product_no,
        )

    if action_type == "admin_product_sku_create":
        product_store_row = (
            await session.execute(
                select(Product, Store)
                .join(Store, Store.id == Product.store_id)
                .where(
                    Product.product_no == payload.get("product_no"),
                    Store.store_no == payload.get("store_no"),
                    Product.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).one_or_none()
        if product_store_row is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标商品或店铺已经不在平台可管理范围内。"
            )
        product, store = product_store_row
        _require_version(store.version, expected.get("store"))
        _require_version(product.version, expected.get("product"))
        if product.product_status not in {"draft", "rejected", "off_shelf", "on_sale"}:
            raise OperationsActionConflict(
                "PRODUCT_NOT_EDITABLE", "商品状态已经变化，当前不能新增款式。"
            )
        await _operations_admin_access(session, context, "products:update")
        sku_name = str(payload.get("sku_name") or "").strip()
        price_minor = int(str(payload.get("price_minor") or 0))
        stock_quantity = int(str(payload.get("stock_quantity") or -1))
        if (
            not 1 <= len(sku_name) <= 255
            or not 1 <= price_minor <= 999_999_999_999
            or not 0 <= stock_quantity <= 999_999_999
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "款式名称、价格或库存校验失败，本次没有新增款式。"
            )
        signature = _sku_style_signature(sku_name)
        duplicate = await session.scalar(
            select(ProductSku.id).where(
                ProductSku.product_id == product.id,
                ProductSku.spec_signature == signature,
            )
        )
        if duplicate is not None:
            raise OperationsActionConflict(
                "SKU_ALREADY_EXISTS", f"款式“{sku_name}”已经存在，本次没有重复创建。"
            )
        sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=product.id,
            store_id=store.id,
            merchant_sku_code=None,
            sku_name=sku_name,
            spec_values=[{"name": "款式", "value": sku_name}],
            spec_signature=signature,
            sale_price_amount=price_minor,
            market_price_amount=price_minor,
            currency="CNY",
            weight_grams=None,
            barcode=None,
            sku_status="active",
            version=1,
        )
        session.add(sku)
        await session.flush()
        inventory = Inventory(
            sku_id=sku.id,
            on_hand_quantity=stock_quantity,
            reserved_quantity=0,
            safety_stock_quantity=0,
            sold_quantity=0,
            inventory_status="active",
            version=1,
        )
        session.add(inventory)
        await session.flush()
        if product.default_sku_id is None:
            product.default_sku_id = sku.id
        product.min_price_amount = min(product.min_price_amount, price_minor)
        product.max_price_amount = max(product.max_price_amount, price_minor)
        product.version += 1
        session.add(
            InventoryLog(
                inventory_id=inventory.id,
                sku_id=sku.id,
                operation_type="agent_sku_create",
                on_hand_delta=stock_quantity,
                reserved_delta=0,
                on_hand_before=0,
                on_hand_after=stock_quantity,
                reserved_before=0,
                reserved_after=0,
                reference_type="agent_action",
                reference_no=action.action_no,
                idempotency_key=action.idempotency_key,
                actor_type="admin",
                actor_id=context.user.id,
                reason="平台管理员通过 AI 管家确认新增商品款式和初始库存",
                inventory_version=inventory.version,
            )
        )
        _outbox(
            session,
            context,
            "product.sku_changed.v1",
            "product",
            product.product_no,
            product.version,
            {
                "store_id": store.store_no,
                "sku_id": sku.sku_no,
                "change": "created",
                "source": "admin_agent_confirmed",
            },
        )
        _admin_audit(
            session,
            context,
            action_type,
            "product_sku",
            sku.sku_no,
            {},
            {
                "product_no": product.product_no,
                "sku_name": sku_name,
                "price_minor": price_minor,
                "stock_quantity": stock_quantity,
            },
        )
        return (
            f"“{store.store_name}”的“{product.product_name}”已新增款式“{sku.sku_name}”，"
            f"售价 {_money(price_minor)}，初始库存 {stock_quantity} 件。请补充该款式图片。",
            {
                "store_id": store.store_no,
                "store_name": store.store_name,
                "product_id": product.product_no,
                "product_name": product.product_name,
                "sku_id": sku.sku_no,
                "sku_name": sku.sku_name,
                "price_minor": price_minor,
                "on_hand_quantity": inventory.on_hand_quantity,
                "available_quantity": inventory.on_hand_quantity,
                "status": product.product_status,
                "version": product.version,
            },
            sku.sku_no,
        )

    if action_type == "merchant_product_sku_create":
        if context.store is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "当前商家店铺身份已经失效。"
            )
        store = await session.scalar(
            select(Store).where(Store.id == context.store.id).with_for_update()
        )
        product = await session.scalar(
            select(Product)
            .where(
                Product.product_no == payload.get("product_no"),
                Product.store_id == context.store.id,
                Product.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if store is None or product is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标商品或店铺已经不在当前经营范围内。"
            )
        _require_version(store.version, expected.get("store"))
        _require_version(product.version, expected.get("product"))
        if product.product_status not in {"draft", "rejected", "off_shelf", "on_sale"}:
            raise OperationsActionConflict(
                "PRODUCT_NOT_EDITABLE", "商品状态已经变化，当前不能新增款式。"
            )
        await _operations_admin_access(session, context, "products:update")
        sku_name = str(payload.get("sku_name") or "").strip()
        price_minor = int(str(payload.get("price_minor") or 0))
        stock_quantity = int(str(payload.get("stock_quantity") or -1))
        if (
            not 1 <= len(sku_name) <= 255
            or not 1 <= price_minor <= 999_999_999_999
            or not 0 <= stock_quantity <= 999_999_999
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "款式名称、价格或库存校验失败，本次没有新增款式。"
            )
        signature = _sku_style_signature(sku_name)
        duplicate = await session.scalar(
            select(ProductSku.id).where(
                ProductSku.product_id == product.id,
                ProductSku.spec_signature == signature,
            )
        )
        if duplicate is not None:
            raise OperationsActionConflict(
                "SKU_ALREADY_EXISTS", f"款式“{sku_name}”已经存在，本次没有重复创建。"
            )
        sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=product.id,
            store_id=store.id,
            merchant_sku_code=None,
            sku_name=sku_name,
            spec_values=[{"name": "款式", "value": sku_name}],
            spec_signature=signature,
            sale_price_amount=price_minor,
            market_price_amount=price_minor,
            currency="CNY",
            weight_grams=None,
            barcode=None,
            sku_status="active",
            version=1,
        )
        session.add(sku)
        await session.flush()
        inventory = Inventory(
            sku_id=sku.id,
            on_hand_quantity=stock_quantity,
            reserved_quantity=0,
            safety_stock_quantity=0,
            sold_quantity=0,
            inventory_status="active",
            version=1,
        )
        session.add(inventory)
        await session.flush()
        if product.default_sku_id is None:
            product.default_sku_id = sku.id
        product.min_price_amount = min(product.min_price_amount, price_minor)
        product.max_price_amount = max(product.max_price_amount, price_minor)
        product.version += 1
        session.add(
            InventoryLog(
                inventory_id=inventory.id,
                sku_id=sku.id,
                operation_type="agent_sku_create",
                on_hand_delta=stock_quantity,
                reserved_delta=0,
                on_hand_before=0,
                on_hand_after=stock_quantity,
                reserved_before=0,
                reserved_after=0,
                reference_type="agent_action",
                reference_no=action.action_no,
                idempotency_key=action.idempotency_key,
                actor_type="merchant",
                actor_id=context.user.id,
                reason="由 AI 经营助理确认卡新增商品款式和初始库存",
                inventory_version=inventory.version,
            )
        )
        _outbox(
            session,
            context,
            "product.sku_changed.v1",
            "product",
            product.product_no,
            product.version,
            {
                "store_id": store.store_no,
                "sku_id": sku.sku_no,
                "change": "created",
                "source": "agent_confirmed",
            },
        )
        return (
            f"“{product.product_name}”已新增款式“{sku.sku_name}”，售价 {_money(price_minor)}，"
            f"初始库存 {stock_quantity} 件。请打开商品编辑页补充该款式图片。",
            {
                "product_id": product.product_no,
                "product_name": product.product_name,
                "sku_id": sku.sku_no,
                "sku_name": sku.sku_name,
                "price_minor": price_minor,
                "on_hand_quantity": inventory.on_hand_quantity,
                "available_quantity": inventory.on_hand_quantity,
                "status": product.product_status,
                "version": product.version,
            },
            sku.sku_no,
        )

    if action_type == "admin_product_sku_disable":
        product_store_row = (
            await session.execute(
                select(Product, Store)
                .join(Store, Store.id == Product.store_id)
                .where(
                    Product.product_no == payload.get("product_no"),
                    Store.store_no == payload.get("store_no"),
                    Product.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).one_or_none()
        sku_to_disable = await session.scalar(
            select(ProductSku).where(ProductSku.sku_no == payload.get("sku_no")).with_for_update()
        )
        if (
            product_store_row is None
            or sku_to_disable is None
            or sku_to_disable.product_id != product_store_row[0].id
            or sku_to_disable.store_id != product_store_row[1].id
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标商品款式已经不在平台可管理范围内。"
            )
        product, store = product_store_row
        _require_version(store.version, expected.get("store"))
        _require_version(product.version, expected.get("product"))
        _require_version(sku_to_disable.version, expected.get("sku"))
        access = await _operations_admin_access(session, context, "products:update")
        try:
            sku_status_result = await ProductAdminService(
                session, get_settings()
            ).change_sku_status(
                access,
                product.product_no,
                sku_to_disable.sku_no,
                AdminSkuStatusRequest(
                    action="disable",
                    reason_code="PLATFORM_AGENT_STYLE_REMOVE",
                    reason="平台管理员通过 AI 管家确认移除商品款式",
                ),
                sku_to_disable.version,
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "款式状态已经变化，请重新发起操作。") from exc
        _admin_audit(
            session,
            context,
            action_type,
            "product_sku",
            sku_status_result.sku_id,
            {"status": "active"},
            {"status": sku_status_result.status},
        )
        return (
            f"“{store.store_name}”的款式“{sku_status_result.sku_name}”已从顾客可选项中移除；"
            "历史交易和库存流水仍保留。",
            {
                "store_id": store.store_no,
                "store_name": store.store_name,
                "product_id": sku_status_result.product_id,
                "sku_id": sku_status_result.sku_id,
                "sku_name": sku_status_result.sku_name,
                "status": sku_status_result.status,
                "version": sku_status_result.version,
            },
            sku_status_result.sku_id,
        )

    if action_type == "merchant_product_sku_disable":
        if context.store is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "当前商家店铺身份已经失效。"
            )
        product = await session.scalar(
            select(Product)
            .where(
                Product.product_no == payload.get("product_no"),
                Product.store_id == context.store.id,
                Product.deleted_at.is_(None),
            )
            .with_for_update()
        )
        sku_to_disable = await session.scalar(
            select(ProductSku)
            .where(
                ProductSku.sku_no == payload.get("sku_no"),
                ProductSku.store_id == context.store.id,
            )
            .with_for_update()
        )
        if product is None or sku_to_disable is None or sku_to_disable.product_id != product.id:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标商品款式已经不在当前店铺经营范围内。"
            )
        _require_version(product.version, expected.get("product"))
        _require_version(sku_to_disable.version, expected.get("sku"))
        access = await _operations_admin_access(session, context, "products:update")
        try:
            sku_status_result = await ProductAdminService(
                session, get_settings()
            ).change_sku_status(
                access,
                product.product_no,
                sku_to_disable.sku_no,
                AdminSkuStatusRequest(
                    action="disable",
                    reason_code="MERCHANT_AGENT_STYLE_REMOVE",
                    reason="商家通过 AI 经营助理确认移除商品款式",
                ),
                sku_to_disable.version,
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "款式状态已经变化，请重新发起操作。") from exc
        return (
            f"“{sku_status_result.sku_name}”已从顾客可选款式中移除；历史交易和库存记录仍保留。",
            {
                "product_id": sku_status_result.product_id,
                "sku_id": sku_status_result.sku_id,
                "sku_name": sku_status_result.sku_name,
                "status": sku_status_result.status,
                "version": sku_status_result.version,
            },
            sku_status_result.sku_id,
        )

    if action_type in {"merchant_product_status", "admin_product_status"}:
        product = await session.scalar(
            select(Product)
            .where(Product.product_no == payload.get("product_no"), Product.deleted_at.is_(None))
            .with_for_update()
        )
        store = (
            await session.scalar(select(Store).where(Store.store_no == payload.get("store_no")))
            if action_type == "admin_product_status"
            else context.store
        )
        if (
            product is None
            or store is None
            or product.store_id != store.id
            or (
                context.audience == "merchant"
                and (context.store is None or product.store_id != context.store.id)
            )
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标商品不再属于当前授权范围。"
            )
        _require_version(product.version, expected.get("product"))
        _require_version(store.version, expected.get("store"))
        target = str(payload.get("target_status"))
        if (product.product_status, target) not in {
            ("on_sale", "off_shelf"),
            ("off_shelf", "on_sale"),
        }:
            raise OperationsActionConflict(
                "AGENT_ACTION_STATE_CHANGED", "商品状态已经变化，请重新发起操作。"
            )
        if target == "on_sale" and store.store_status != "active":
            raise OperationsActionConflict("STORE_NOT_ACTIVE", "店铺当前未营业，不能上架商品。")
        previous = product.product_status
        product.product_status = target
        product.off_shelf_at = now if target == "off_shelf" else None
        product.published_at = product.published_at or (now if target == "on_sale" else None)
        product.version += 1
        session.add(
            ProductStatusLog(
                product_id=product.id,
                from_status=previous,
                to_status=target,
                event_type=f"product.{'off_shelf' if target == 'off_shelf' else 'published'}.v1",
                actor_type="admin" if context.audience == "admin" else "merchant",
                actor_id=context.user.id,
                reason_code="AGENT_CONFIRMED_ACTION",
                reason="由对话确认卡执行",
                product_version=product.version,
                request_id=context.run.trace_id,
                trace_id=context.run.trace_id,
            )
        )
        _outbox(
            session,
            context,
            f"product.{'off_shelf' if target == 'off_shelf' else 'published'}.v1",
            "product",
            product.product_no,
            product.version,
            {
                "store_id": store.store_no,
                "from_status": previous,
                "to_status": target,
                "source": "agent_confirmed",
            },
        )
        _admin_audit(
            session,
            context,
            action_type,
            "product",
            product.product_no,
            {"status": previous},
            {"status": target},
        )
        return (
            f"“{product.product_name}”已切换为“{_product_status_label(target)}”。",
            {
                "product_id": product.product_no,
                "product_name": product.product_name,
                "status": target,
                "version": product.version,
            },
            product.product_no,
        )

    if action_type in {"merchant_product_delete", "admin_product_delete"}:
        product = await session.scalar(
            select(Product)
            .where(Product.product_no == payload.get("product_no"), Product.deleted_at.is_(None))
            .with_for_update()
        )
        store = (
            await session.scalar(select(Store).where(Store.store_no == payload.get("store_no")))
            if action_type == "admin_product_delete"
            else context.store
        )
        if (
            product is None
            or store is None
            or product.store_id != store.id
            or (
                context.audience == "merchant"
                and (context.store is None or product.store_id != context.store.id)
            )
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标商品不再属于当前授权范围。"
            )
        _require_version(product.version, expected.get("product"))
        _require_version(store.version, expected.get("store"))
        await _operations_admin_access(session, context, "products:update")
        has_transactions = bool(
            await session.scalar(
                select(OrderItem.id).where(OrderItem.product_id == product.id).limit(1)
            )
        )
        if has_transactions:
            raise OperationsActionConflict(
                "PRODUCT_HAS_TRANSACTIONS",
                "该商品在确认后产生了交易，已停止永久删除。请重新发起下架操作。",
            )
        previous = product.product_status
        skus = (
            await session.scalars(
                select(ProductSku).where(ProductSku.product_id == product.id).with_for_update()
            )
        ).all()
        for sku in skus:
            if sku.sku_status != "disabled":
                sku.sku_status = "disabled"
                sku.version += 1
        product.product_status = "deleted"
        product.deleted_at = now
        product.off_shelf_at = now if previous == "on_sale" else product.off_shelf_at
        product.version += 1
        session.add(
            ProductStatusLog(
                product_id=product.id,
                from_status=previous,
                to_status="deleted",
                event_type="product.deleted.v1",
                actor_type="admin" if context.audience == "admin" else "merchant",
                actor_id=context.user.id,
                reason_code=(
                    "PLATFORM_AGENT_PERMANENT_DELETE"
                    if context.audience == "admin"
                    else "MERCHANT_AGENT_PERMANENT_DELETE"
                ),
                reason="由对话确认卡永久删除无交易商品",
                product_version=product.version,
                request_id=context.run.trace_id,
                trace_id=context.run.trace_id,
            )
        )
        _outbox(
            session,
            context,
            "product.deleted.v1",
            "product",
            product.product_no,
            product.version,
            {
                "store_id": store.store_no,
                "from_status": previous,
                "to_status": "deleted",
                "source": "agent_confirmed",
            },
        )
        _admin_audit(
            session,
            context,
            action_type,
            "product",
            product.product_no,
            {"status": previous},
            {"status": "deleted", "deleted_at": now.isoformat()},
        )
        return (
            f"“{product.product_name}”已永久删除，全部款式已停用。我已回读确认它不再出现在商品列表中。",
            {
                "product_id": product.product_no,
                "product_name": product.product_name,
                "status": "deleted",
                "deleted_at": now.isoformat(),
                "version": product.version,
            },
            product.product_no,
        )

    if action_type == "merchant_product_submit":
        if context.store is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "当前商家店铺身份已经失效。"
            )
        product = await session.scalar(
            select(Product).where(
                Product.product_no == payload.get("product_no"),
                Product.store_id == context.store.id,
                Product.deleted_at.is_(None),
            )
        )
        if product is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标商品不再属于当前店铺。"
            )
        _require_version(product.version, expected.get("product"))
        _require_version(context.store.version, expected.get("store"))
        access = await _operations_admin_access(session, context, "products:publish")
        service = ProductAdminService(session, get_settings())
        try:
            result = await service.submit_review(
                access,
                product.product_no,
                AdminProductCommandRequest(
                    reason_code="MERCHANT_AGENT_SUBMIT",
                    reason="商家通过 AI 经营助理确认提交自动审核",
                ),
                product.version,
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "商品资料或审核状态已经变化，请重新核对。") from exc
        status = str(result.status)
        answer = (
            f"“{result.product_name}”已通过自动审核并上架销售。"
            if status == "on_sale"
            else f"“{result.product_name}”自动审核后状态为"
            f"“{_product_status_label(status)}”，请按提示修改后重新提交。"
        )
        return (
            answer,
            {
                "product_id": result.product_id,
                "product_name": result.product_name,
                "status": status,
                "missing_requirements": result.completeness.missing_requirements,
                "version": result.version,
            },
            result.product_id,
        )

    if action_type == "merchant_product_fulfillment":
        if context.store is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "当前商家店铺身份已经失效。"
            )
        fulfillment_row_exec = (
            await session.execute(
                select(Product, ProductFulfillmentProfile, ShippingTemplate)
                .join(
                    ProductFulfillmentProfile,
                    ProductFulfillmentProfile.product_id == Product.id,
                )
                .join(
                    ShippingTemplate,
                    ShippingTemplate.id == ProductFulfillmentProfile.shipping_template_id,
                )
                .where(
                    Product.product_no == payload.get("product_no"),
                    Product.store_id == context.store.id,
                    Product.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).one_or_none()
        if fulfillment_row_exec is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "商品或其发货配置已不存在。"
            )
        product, fulfillment, shipping_template = fulfillment_row_exec
        if shipping_template.template_no != payload.get("shipping_template_no"):
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "商品配送模板已经变化，请重新发起操作。"
            )
        _require_version(product.version, expected.get("product"))
        _require_version(fulfillment.version, expected.get("fulfillment"))
        _require_version(shipping_template.version, expected.get("shipping_template"))
        _require_version(context.store.version, expected.get("store"))
        access = await _operations_admin_access(session, context, "products:update")
        fulfillment_request = AdminProductFulfillmentRequest(
            shipping_template_id=shipping_template.template_no,
            origin_region_code=str(payload.get("origin_region_code") or ""),
            dispatch_min_hours=int(str(payload.get("dispatch_min_hours") or 0)),
            dispatch_max_hours=int(str(payload.get("dispatch_max_hours") or 0)),
            purchase_notice=(
                str(payload["purchase_notice"])
                if payload.get("purchase_notice") is not None
                else None
            ),
        )
        try:
            fulfillment_result = await ProductAdminService(session, get_settings()).set_fulfillment(
                access,
                product.product_no,
                fulfillment_request,
                product.version,
            )
        except Exception as exc:
            raise _service_conflict(exc, "商品发货配置已经变化，请重新核对。") from exc
        fulfillment_window_label = _dispatch_window_label(
            fulfillment_result.dispatch_min_hours,
            fulfillment_result.dispatch_max_hours,
        )
        return (
            f"“{product.product_name}”的发货信息已更新: "
            f"{_region_label(fulfillment_result.origin_region_code)}，"
            f"{fulfillment_window_label}。",
            {
                "product_id": product.product_no,
                "product_name": product.product_name,
                "shipping_template_id": fulfillment_result.shipping_template_id,
                "origin_region_code": fulfillment_result.origin_region_code,
                "dispatch_min_hours": fulfillment_result.dispatch_min_hours,
                "dispatch_max_hours": fulfillment_result.dispatch_max_hours,
                "purchase_notice": fulfillment_result.purchase_notice,
                "profile_version": fulfillment_result.profile_version,
                "version": fulfillment_result.version,
            },
            product.product_no,
        )

    if action_type == "merchant_product_image_description":
        if context.store is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "当前商家店铺身份已经失效。"
            )
        product = await session.scalar(
            select(Product).where(
                Product.product_no == payload.get("product_no"),
                Product.store_id == context.store.id,
                Product.deleted_at.is_(None),
            )
        )
        if product is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标商品不再属于当前店铺。"
            )
        _require_version(product.version, expected.get("product"))
        _require_version(context.store.version, expected.get("store"))
        current = (
            await session.scalar(
                select(ProductContentVersion).where(
                    ProductContentVersion.id == product.current_detail_content_version_id,
                    ProductContentVersion.product_id == product.id,
                )
            )
            if product.current_detail_content_version_id is not None
            else None
        )
        if current is None or current.safe_blocks is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "商品详情版本已经变化，请重新发起操作。"
            )
        _require_version(current.version, expected.get("content"))
        image_number = int(str(payload.get("image_number") or 0))
        expected_file_id = str(payload.get("file_id") or "")
        description = str(payload.get("description") or "")
        if not 1 <= image_number <= 100 or len(description) > 8000:
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "详情图片序号或图片说明不符合要求。"
            )
        blocks = [dict(item) for item in current.safe_blocks]
        image_indexes = [index for index, item in enumerate(blocks) if item.get("type") == "image"]
        if image_number > len(image_indexes):
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "详情图片顺序已经变化，请重新发起操作。"
            )
        block_index = image_indexes[image_number - 1]
        if str(blocks[block_index].get("file_id") or "") != expected_file_id:
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "目标详情图片已经变化，请重新发起操作。"
            )
        if description:
            blocks[block_index]["description"] = description
        else:
            blocks[block_index].pop("description", None)
        access = await _operations_admin_access(session, context, "products:update")
        service = ProductAdminService(session, get_settings())
        try:
            content_result = await service.create_content_version(
                access,
                product.product_no,
                AdminContentVersionCreateRequest(
                    source_format="structured",
                    source_content=json.dumps(blocks, ensure_ascii=False),
                ),
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "商品详情已经变化，请重新核对图片说明。") from exc
        return (
            f"“{product.product_name}”第 {image_number} 张详情图片说明已更新，"
            "我已回读新的详情版本。",
            {
                "product_id": product.product_no,
                "image_number": image_number,
                "file_id": expected_file_id,
                "image_description": description,
                "content_version_id": content_result.version_id,
                "content_version": content_result.content_version,
            },
            content_result.version_id,
        )

    if action_type in {"merchant_product_sku_update", "admin_product_sku_update"}:
        is_merchant_action = action_type.startswith("merchant_")
        store = await session.scalar(
            select(Store)
            .where(
                Store.store_no
                == (
                    context.store.store_no
                    if is_merchant_action and context.store is not None
                    else payload.get("store_no")
                )
            )
            .with_for_update()
        )
        product = await session.scalar(
            select(Product)
            .where(
                Product.product_no == payload.get("product_no"),
                Product.deleted_at.is_(None),
            )
            .with_for_update()
        )
        sku = await session.scalar(
            select(ProductSku)
            .where(
                ProductSku.sku_no == payload.get("sku_no"),
                ProductSku.sku_status == "active",
            )
            .with_for_update()
        )
        inventory = (
            await session.scalar(
                select(Inventory).where(Inventory.sku_id == sku.id).with_for_update()
            )
            if sku is not None
            else None
        )
        if (
            store is None
            or product is None
            or sku is None
            or inventory is None
            or product.store_id != store.id
            or sku.product_id != product.id
            or sku.store_id != store.id
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标商品款式已经不在可管理范围内。"
            )
        if is_merchant_action:
            if (
                context.audience != "merchant"
                or context.store is None
                or context.store.id != store.id
            ):
                raise OperationsActionConflict(
                    "AGENT_ACTION_SCOPE_CHANGED", "当前商家身份不能修改该商品款式。"
                )
        else:
            await _operations_admin_access(session, context, "products:update")
        _require_version(store.version, expected.get("store"))
        _require_version(product.version, expected.get("product"))
        _require_version(sku.version, expected.get("sku"))
        _require_version(inventory.version, expected.get("inventory"))
        if product.product_status not in {"draft", "rejected", "off_shelf", "on_sale"}:
            raise OperationsActionConflict(
                "PRODUCT_NOT_EDITABLE", "商品状态已经变化，当前不能修改款式。"
            )
        new_name = str(payload.get("sku_name") or "").strip()
        new_price = int(str(payload.get("price_minor") or 0))
        new_stock = int(str(payload.get("stock_quantity") or 0))
        if not 1 <= len(new_name) <= 255 or not 1 <= new_price <= 999_999_999_999:
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "款式名称或售价超出允许范围。"
            )
        if not 0 <= new_stock <= 999_999_999:
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "库存数量超出允许范围。"
            )
        if new_stock < inventory.reserved_quantity:
            raise OperationsActionConflict(
                "INVENTORY_BELOW_RESERVED",
                f"当前已有 {inventory.reserved_quantity} 件预占，不能将库存设得更低。",
            )
        duplicate_name = await session.scalar(
            select(ProductSku.id).where(
                ProductSku.product_id == product.id,
                ProductSku.id != sku.id,
                ProductSku.sku_status == "active",
                func.lower(ProductSku.sku_name) == new_name.casefold(),
            )
        )
        if duplicate_name is not None:
            raise OperationsActionConflict(
                "SKU_NAME_ALREADY_EXISTS", "同一商品已经存在该款式名称，请重新选择。"
            )
        before = {
            "sku_name": sku.sku_name,
            "price_minor": sku.sale_price_amount,
            "stock_quantity": inventory.on_hand_quantity,
            "reserved_quantity": inventory.reserved_quantity,
        }
        sku_changed = sku.sku_name != new_name or sku.sale_price_amount != new_price
        stock_changed = inventory.on_hand_quantity != new_stock
        sku.sku_name = new_name
        sku.sale_price_amount = new_price
        if sku.market_price_amount < new_price:
            sku.market_price_amount = new_price
        if sku_changed:
            sku.version += 1
            product.version += 1
        if stock_changed:
            stock_before = inventory.on_hand_quantity
            inventory.on_hand_quantity = new_stock
            inventory.version += 1
            session.add(
                InventoryLog(
                    inventory_id=inventory.id,
                    sku_id=sku.id,
                    operation_type="agent_set",
                    on_hand_delta=new_stock - stock_before,
                    reserved_delta=0,
                    on_hand_before=stock_before,
                    on_hand_after=new_stock,
                    reserved_before=inventory.reserved_quantity,
                    reserved_after=inventory.reserved_quantity,
                    reference_type="agent_action",
                    reference_no=action.action_no,
                    idempotency_key=action.idempotency_key,
                    actor_type="merchant" if is_merchant_action else "admin",
                    actor_id=context.user.id,
                    reason="由 AI 确认卡原子修改商品款式",
                    inventory_version=inventory.version,
                )
            )
            _outbox(
                session,
                context,
                "inventory.adjusted.v1",
                "inventory",
                sku.sku_no,
                inventory.version,
                {
                    "product_id": product.product_no,
                    "sku_id": sku.sku_no,
                    "on_hand_before": stock_before,
                    "on_hand_after": new_stock,
                    "source": "agent_confirmed",
                },
            )
        if sku_changed:
            await session.flush()
            bounds = await session.execute(
                select(
                    func.min(ProductSku.sale_price_amount),
                    func.max(ProductSku.sale_price_amount),
                ).where(
                    ProductSku.product_id == product.id,
                    ProductSku.sku_status == "active",
                )
            )
            minimum, maximum = bounds.one()
            product.min_price_amount = int(minimum or new_price)
            product.max_price_amount = int(maximum or new_price)
            _outbox(
                session,
                context,
                "product.sku_changed.v1",
                "product",
                product.product_no,
                product.version,
                {
                    "sku_id": sku.sku_no,
                    "name_before": before["sku_name"],
                    "name_after": new_name,
                    "price_before": before["price_minor"],
                    "price_after": new_price,
                    "source": "agent_confirmed",
                },
            )
        after = {
            "sku_name": sku.sku_name,
            "price_minor": sku.sale_price_amount,
            "stock_quantity": inventory.on_hand_quantity,
            "reserved_quantity": inventory.reserved_quantity,
        }
        if not is_merchant_action:
            _admin_audit(
                session,
                context,
                action_type,
                "product_sku",
                sku.sku_no,
                before,
                after,
            )
        return (
            f"款式“{sku.sku_name}”已更新：售价 {_money(sku.sale_price_amount)}，"
            f"库存 {inventory.on_hand_quantity} 件，可售 "
            f"{inventory.on_hand_quantity - inventory.reserved_quantity} 件。",
            {
                "store_id": store.store_no,
                "product_id": product.product_no,
                "product_name": product.product_name,
                "sku_id": sku.sku_no,
                "sku_name": sku.sku_name,
                "price_minor": sku.sale_price_amount,
                "on_hand_quantity": inventory.on_hand_quantity,
                "reserved_quantity": inventory.reserved_quantity,
                "available_quantity": inventory.on_hand_quantity - inventory.reserved_quantity,
                "version": sku.version,
            },
            sku.sku_no,
        )

    if action_type == "merchant_inventory_set":
        product, sku, inventory = await _locked_merchant_sku(session, context, payload)
        _require_version(product.version, expected.get("product"))
        _require_version(sku.version, expected.get("sku"))
        _require_version(inventory.version, expected.get("inventory"))
        inventory_target = int(str(payload["target_quantity"]))
        if inventory_target < inventory.reserved_quantity:
            raise OperationsActionConflict(
                "INVENTORY_BELOW_RESERVED",
                f"当前已有 {inventory.reserved_quantity} 件预占，不能将库存设得更低。",
            )
        inventory_before = inventory.on_hand_quantity
        inventory.on_hand_quantity = inventory_target
        inventory.version += 1
        session.add(
            InventoryLog(
                inventory_id=inventory.id,
                sku_id=sku.id,
                operation_type="agent_set",
                on_hand_delta=inventory_target - inventory_before,
                reserved_delta=0,
                on_hand_before=inventory_before,
                on_hand_after=inventory_target,
                reserved_before=inventory.reserved_quantity,
                reserved_after=inventory.reserved_quantity,
                reference_type="agent_action",
                reference_no=action.action_no,
                idempotency_key=action.idempotency_key,
                actor_type="merchant",
                actor_id=context.user.id,
                reason="由 AI 经营助理确认卡调整",
                inventory_version=inventory.version,
            )
        )
        _outbox(
            session,
            context,
            "inventory.adjusted.v1",
            "inventory",
            sku.sku_no,
            inventory.version,
            {
                "product_id": product.product_no,
                "sku_id": sku.sku_no,
                "on_hand_before": inventory_before,
                "on_hand_after": inventory_target,
                "source": "agent_confirmed",
            },
        )
        return (
            f"“{sku.sku_name}”库存已从 {inventory_before} 件调整为 {inventory_target} 件，"
            f"当前可售 {inventory_target - inventory.reserved_quantity} 件。",
            {
                "product_id": product.product_no,
                "sku_id": sku.sku_no,
                "sku_name": sku.sku_name,
                "on_hand_quantity": inventory_target,
                "reserved_quantity": inventory.reserved_quantity,
                "available_quantity": inventory_target - inventory.reserved_quantity,
                "version": inventory.version,
            },
            sku.sku_no,
        )

    if action_type == "merchant_price_set":
        product, sku, _ = await _locked_merchant_sku(session, context, payload)
        _require_version(product.version, expected.get("product"))
        _require_version(sku.version, expected.get("sku"))
        price_before = sku.sale_price_amount
        price_target = int(str(payload["target_price_minor"]))
        sku.sale_price_amount = price_target
        if sku.market_price_amount < price_target:
            sku.market_price_amount = price_target
        sku.version += 1
        product.version += 1
        bounds = await session.execute(
            select(
                func.min(ProductSku.sale_price_amount), func.max(ProductSku.sale_price_amount)
            ).where(ProductSku.product_id == product.id, ProductSku.sku_status == "active")
        )
        minimum, maximum = bounds.one()
        product.min_price_amount = int(minimum or price_target)
        product.max_price_amount = int(maximum or price_target)
        _outbox(
            session,
            context,
            "product.sku_changed.v1",
            "product",
            product.product_no,
            product.version,
            {
                "sku_id": sku.sku_no,
                "price_before": price_before,
                "price_after": price_target,
                "source": "agent_confirmed",
            },
        )
        return (
            f"“{sku.sku_name}”售价已从 {_money(price_before)} 修改为 {_money(price_target)}，"
            "新结算将读取该价格。",
            {
                "product_id": product.product_no,
                "sku_id": sku.sku_no,
                "sku_name": sku.sku_name,
                "price_minor": price_target,
                "currency": sku.currency,
                "version": sku.version,
            },
            sku.sku_no,
        )

    if action_type == "merchant_shipment_create":
        access = await _operations_admin_access(session, context, "shipments:create")
        settings = get_settings()
        logistics_service = LogisticsService(
            session,
            SecurityService(settings),
            settings.security_hmac_secret.get_secret_value(),
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENTS_INVALID", "发货商品明细已经失效，请重新发起操作。"
            )
        create_request = AdminShipmentCreateRequest(
            carrier_code="fake_express",
            carrier_name="商城模拟物流",
            tracking_no=str(payload.get("tracking_no") or ""),
            items=[AdminShipmentCreateItem.model_validate(item) for item in items],
        )
        try:
            shipment_result = await logistics_service.create_shipment(
                access,
                str(payload.get("order_no") or ""),
                create_request,
                int(str(expected.get("order", -1))),
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "订单发货条件已经变化，请重新核对。") from exc
        return (
            f"订单 {shipment_result.order_id} 已创建包裹，当前为“待揽收”。"
            "后续物流节点不会按时间自动推进，"
            "可继续让我按实际进度更新。",
            {
                "order_id": shipment_result.order_id,
                "shipment_id": shipment_result.shipment_id,
                "status": shipment_result.shipment_status,
                "carrier_name": shipment_result.carrier_name,
                "tracking_no_masked": shipment_result.tracking_no_masked,
                "version": shipment_result.version,
            },
            shipment_result.shipment_id,
        )

    if action_type == "admin_order_cancel":
        if context.audience != "admin":
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "只有平台 AI 管家可以执行订单治理。"
            )
        order = await session.scalar(
            select(Order)
            .where(Order.order_no == payload.get("order_no"))
            .with_for_update()
        )
        if order is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标订单已经不存在。"
            )
        _require_version(order.version, expected.get("order"))
        access = await _operations_admin_access(session, context, "orders:cancel")
        settings = get_settings()
        order_service = OrderService(session, settings, SecurityService(settings))
        try:
            result = await order_service.admin_cancel(
                access,
                order.order_no,
                AdminOrderCancellationRequest(
                    reason_code="ADMIN_AGENT_CANCELLED",
                    reason=str(payload.get("reason") or "管理员通过 AI 管家确认取消未付款交易"),
                ),
                order.version,
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "订单或支付状态已经变化，本次没有取消交易。") from exc
        return (
            f"订单 {result.order.order_id} 所属未付款交易已取消，库存预占已按订单服务规则释放。",
            {
                "order_id": result.order.order_id,
                "order_status": result.order.order_status,
                "payment_status": result.order.payment_status,
                "available_actions": result.order.available_actions,
                "version": result.order.version,
            },
            result.order.order_id,
        )

    if action_type == "admin_shipment_progress":
        if context.audience != "admin":
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "只有平台 AI 管家可以执行平台物流治理。"
            )
        target_row = (
            await session.execute(
                select(Shipment, Order, Store)
                .join(Order, Order.id == Shipment.order_id)
                .join(Store, Store.id == Shipment.store_id)
                .where(Shipment.shipment_no == payload.get("shipment_no"))
                .with_for_update()
            )
        ).one_or_none()
        if target_row is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标包裹或所属订单已经不存在。"
            )
        shipment, order, store = target_row
        if order.order_no != payload.get("order_no"):
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "包裹与确认时的订单归属已经变化。"
            )
        _require_version(shipment.version, expected.get("shipment"))
        _require_version(order.version, expected.get("order"))
        _require_version(store.version, expected.get("store"))
        access = await _operations_admin_access(session, context, "shipments:create")
        settings = get_settings()
        logistics_service = LogisticsService(
            session,
            SecurityService(settings),
            settings.security_hmac_secret.get_secret_value(),
        )
        progress_request = AdminShipmentSimulationEventRequest(
            event_type=cast(
                Literal[
                    "picked_up",
                    "in_transit",
                    "out_for_delivery",
                    "delivered",
                    "exception",
                    "returned",
                ],
                str(payload.get("event_type") or ""),
            ),
            description=str(payload.get("description") or ""),
            location_text=(str(payload["location_text"]) if payload.get("location_text") else None),
        )
        try:
            shipment_result = await logistics_service.record_simulation_event(
                access,
                shipment.shipment_no,
                progress_request,
                shipment.version,
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "物流节点已经变化，请重新核对后再推进。") from exc
        return (
            f"包裹 {shipment_result.shipment_id} 已更新为"
            f"“{_shipment_status_label(shipment_result.shipment_status)}”，"
            "顾客端和店铺端将读取同一条最新轨迹。",
            {
                "order_id": shipment_result.order_id,
                "shipment_id": shipment_result.shipment_id,
                "status": shipment_result.shipment_status,
                "carrier_name": shipment_result.carrier_name,
                "tracking_no_masked": shipment_result.tracking_no_masked,
                "latest_track": (
                    shipment_result.latest_tracks[0].model_dump(mode="json")
                    if shipment_result.latest_tracks
                    else None
                ),
                "version": shipment_result.version,
            },
            shipment_result.shipment_id,
        )

    if action_type == "merchant_shipment_progress":
        access = await _operations_admin_access(session, context, "shipments:create")
        settings = get_settings()
        logistics_service = LogisticsService(
            session,
            SecurityService(settings),
            settings.security_hmac_secret.get_secret_value(),
        )
        progress_request = AdminShipmentSimulationEventRequest(
            event_type=cast(
                Literal[
                    "picked_up",
                    "in_transit",
                    "out_for_delivery",
                    "delivered",
                    "exception",
                    "returned",
                ],
                str(payload.get("event_type") or ""),
            ),
            description=str(payload.get("description") or ""),
            location_text=(str(payload["location_text"]) if payload.get("location_text") else None),
        )
        try:
            shipment_result = await logistics_service.record_simulation_event(
                access,
                str(payload.get("shipment_no") or ""),
                progress_request,
                int(str(expected.get("shipment", -1))),
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "物流节点已经变化，请重新核对后再推进。") from exc
        return (
            f"包裹 {shipment_result.shipment_id} 已更新为"
            f"“{_shipment_status_label(shipment_result.shipment_status)}”。",
            {
                "order_id": shipment_result.order_id,
                "shipment_id": shipment_result.shipment_id,
                "status": shipment_result.shipment_status,
                "carrier_name": shipment_result.carrier_name,
                "tracking_no_masked": shipment_result.tracking_no_masked,
                "version": shipment_result.version,
            },
            shipment_result.shipment_id,
        )

    if action_type == "merchant_review_reply":
        access = await _operations_admin_access(session, context, "reviews:reply")
        review_service = ReviewService(session, get_settings())
        try:
            review_result = await review_service.admin_reply(
                access,
                str(payload.get("review_no") or ""),
                AdminReviewReplyRequest(content=str(payload.get("content") or "")),
                int(str(expected.get("review", -1))),
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "评价状态已经变化，请重新核对后回复。") from exc
        return (
            f"已回复“{payload.get('product_name')}”的这条评价，并回读到公开回复。",
            {
                "review_id": review_result.review_id,
                "product_name": payload.get("product_name"),
                "customer_name": payload.get("customer_name"),
                "status": review_result.review_status,
                "version": review_result.version,
            },
            review_result.review_id,
        )

    if action_type in {
        "merchant_support_claim",
        "merchant_support_reply",
        "merchant_support_resolve",
        "admin_support_claim",
        "admin_support_reply",
        "admin_support_resolve",
    }:
        support_row = (
            await session.execute(
                select(HumanServiceTicket, Conversation)
                .join(Conversation, Conversation.id == HumanServiceTicket.conversation_id)
                .where(HumanServiceTicket.ticket_no == payload.get("ticket_no"))
                .with_for_update()
            )
        ).one_or_none()
        if support_row is None:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "人工服务工单已经不存在。")
        ticket, conversation = support_row
        if conversation.conversation_no != payload.get("conversation_no"):
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "人工服务会话与确认卡不再一致。"
            )
        if action_type.startswith("merchant_"):
            if (
                context.audience != "merchant"
                or context.store is None
                or conversation.store_id != context.store.id
            ):
                raise OperationsActionConflict(
                    "AGENT_ACTION_SCOPE_CHANGED", "该工单不再属于当前店铺。"
                )
        elif context.audience != "admin" or conversation.conversation_type != "exclusive":
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "该工单不在当前平台客服范围内。"
            )
        _require_version(ticket.version, expected.get("ticket"))
        _require_version(conversation.version, expected.get("conversation"))
        support_service = SupportService(session)
        target_label = str(payload.get("target_label") or "对方")
        result_payload: dict[str, object]
        try:
            if action_type.endswith("_claim"):
                access = await _operations_admin_access(session, context, "support:claim")
                ticket_result = await support_service.claim(
                    access,
                    ticket.ticket_no,
                    ticket.version,
                    action.idempotency_key,
                )
                answer = f"已接入 {target_label} 的人工服务工单。"
                result_payload = {
                    "ticket_id": ticket_result.ticket_id,
                    "status": ticket_result.ticket_status,
                    "assigned_user_id": ticket_result.assigned_user_id,
                    "version": ticket_result.version,
                }
            elif action_type.endswith("_resolve"):
                access = await _operations_admin_access(session, context, "support:resolve")
                resolution_result = await support_service.resolve(
                    access,
                    ticket.ticket_no,
                    SupportResolveRequest(
                        resolution_code="AI_ASSISTED_RESOLVED",
                        summary="运营人员确认结束人工服务",
                    ),
                    ticket.version,
                    action.idempotency_key,
                )
                answer = f"{target_label} 的人工服务已结束，AI 已恢复接待。"
                result_payload = {
                    "ticket_id": resolution_result.ticket_id,
                    "status": resolution_result.ticket_status,
                    "resolution_summary": resolution_result.resolution_summary,
                    "version": resolution_result.version,
                }
            else:
                access = await _operations_admin_access(session, context, "support:reply")
                reply_kind = str(payload.get("reply_kind") or "text")
                request_payload: SupportMessageRequest
                if reply_kind == "order_card":
                    order = await session.scalar(
                        select(Order).where(Order.order_no == payload.get("order_no"))
                    )
                    if (
                        order is None
                        or order.user_id != conversation.user_id
                        or (
                            context.audience == "merchant"
                            and (context.store is None or order.store_id != context.store.id)
                        )
                    ):
                        raise OperationsActionConflict(
                            "AGENT_ACTION_SCOPE_CHANGED", "订单已经不在当前顾客和店铺授权范围内。"
                        )
                    _require_version(order.version, expected.get("order"))
                    request_payload = SupportMessageRequest(
                        client_message_id=new_prefixed_ulid("cmsg_"),
                        order_id=order.order_no,
                    )
                elif reply_kind == "product_card":
                    product = await session.scalar(
                        select(Product).where(
                            Product.product_no == payload.get("product_no"),
                            Product.product_status == "on_sale",
                            Product.deleted_at.is_(None),
                        )
                    )
                    if product is None or (
                        context.audience == "merchant"
                        and (context.store is None or product.store_id != context.store.id)
                    ):
                        raise OperationsActionConflict(
                            "AGENT_ACTION_SCOPE_CHANGED", "商品已经下架或不在当前店铺授权范围内。"
                        )
                    _require_version(product.version, expected.get("product"))
                    request_payload = SupportMessageRequest(
                        client_message_id=new_prefixed_ulid("cmsg_"),
                        product_id=product.product_no,
                    )
                else:
                    reply_text = str(payload.get("reply_text") or "").strip()
                    if not reply_text:
                        raise OperationsActionConflict(
                            "AGENT_ACTION_INVALID_PAYLOAD", "回复内容已经失效，请重新编辑。"
                        )
                    request_payload = SupportMessageRequest(
                        client_message_id=new_prefixed_ulid("cmsg_"),
                        text=reply_text,
                    )
                message_result = await support_service.send_conversation(
                    access,
                    conversation.conversation_no,
                    request_payload,
                )
                message_label = {
                    "order_card": "订单卡片",
                    "product_card": "商品卡片",
                }.get(reply_kind, "消息")
                answer = f"{message_label}已发送给 {target_label}。"
                result_payload = {
                    "message_id": message_result.message_id,
                    "message_type": message_result.message_type,
                    "sequence_no": message_result.sequence_no,
                    "status": message_result.message_status,
                    "sent_at": message_result.sent_at.isoformat(),
                }
        except OperationsActionConflict:
            raise
        except Exception as exc:
            raise _service_conflict(exc, "人工服务状态已经变化，请重新核对后再试。") from exc
        return answer, result_payload, ticket.ticket_no

    if action_type in {
        "merchant_refund_decision",
        "admin_refund_decision",
        "merchant_refund_more_info",
        "admin_refund_more_info",
    }:
        refund = await session.scalar(
            select(RefundApplication)
            .where(RefundApplication.refund_no == payload.get("refund_no"))
            .with_for_update()
        )
        if refund is None or (
            context.audience == "merchant"
            and (context.store is None or refund.store_id != context.store.id)
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标售后申请不再属于当前授权范围。"
            )
        _require_version(refund.version, expected.get("refund"))
        order = await session.scalar(select(Order).where(Order.id == refund.order_id))
        store = await session.scalar(select(Store).where(Store.id == refund.store_id))
        if order is None or store is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "售后关联订单或店铺已经失效。"
            )
        _require_version(order.version, expected.get("order"))
        _require_version(store.version, expected.get("store"))
        access = await _operations_admin_access(session, context, "refunds:review")
        if action_type in {"merchant_refund_more_info", "admin_refund_more_info"}:
            if refund.refund_status not in {"submitted", "merchant_review"}:
                raise OperationsActionConflict(
                    "AGENT_ACTION_RESOURCE_CHANGED",
                    "该售后申请已经离开待审核阶段，不能再要求补充材料。",
                )
            requirements = str(payload.get("required_materials") or "").strip()
            if len(requirements) < 2:
                raise OperationsActionConflict(
                    "AGENT_ACTION_INVALID_PAYLOAD", "补充材料要求已经失效，请重新填写。"
                )
            previous_status = refund.refund_status
            refund.refund_status = "merchant_review"
            refund.version += 1
            now = utc_now()
            event_no = new_prefixed_ulid("rfe_")
            session.add(
                RefundEvent(
                    event_no=event_no,
                    refund_id=refund.id,
                    from_status=previous_status,
                    to_status=refund.refund_status,
                    event_code="more_info_requested",
                    actor_type=("merchant" if context.audience == "merchant" else "admin"),
                    actor_user_id=context.user.id,
                    reason=requirements,
                    request_id=context.run.trace_id,
                )
            )
            customer_conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.user_id == refund.user_id,
                    Conversation.store_id == refund.store_id,
                    Conversation.conversation_type == "store",
                    Conversation.deleted_at.is_(None),
                )
            )
            if customer_conversation is None:
                customer_conversation = Conversation(
                    conversation_no=new_prefixed_ulid("conv_"),
                    user_id=refund.user_id,
                    store_id=refund.store_id,
                    conversation_type="store",
                    is_fixed=False,
                    conversation_status="active",
                    last_sequence_no=0,
                )
                session.add(customer_conversation)
                await session.flush()
            else:
                customer_conversation = await lock_conversation_for_append(
                    session, customer_conversation.id
                )
            customer_conversation.last_sequence_no += 1
            customer_conversation.version += 1
            customer_conversation.last_message_at = now
            customer_conversation.user_hidden_at = None
            if customer_conversation.conversation_status == "closed":
                customer_conversation.conversation_status = "active"
            notice = Message(
                message_no=new_prefixed_ulid("msg_"),
                conversation_id=customer_conversation.id,
                sequence_no=customer_conversation.last_sequence_no,
                client_message_no=None,
                sender_type="system",
                sender_id=None,
                message_type="system",
                text_content=(
                    f"售后申请需要补充材料：{requirements}。请在当前会话回复相关说明；"
                    "如需补充图片或文件，请联系店铺人工客服确认提交方式。"
                ),
                content_payload={
                    "event": "refund_more_info_requested",
                    "refund_id": refund.refund_no,
                    "order_id": order.order_no,
                    "required_materials": requirements,
                    "run_id": context.run.run_no,
                },
                message_status="sent",
                moderation_status="passed",
                sent_at=now,
            )
            session.add(notice)
            await session.flush()
            customer_conversation.last_message_id = notice.id
            _outbox(
                session,
                context,
                "refund.more_info_requested.v1",
                "refund_application",
                refund.refund_no,
                refund.version,
                {
                    "refund_id": refund.refund_no,
                    "order_id": order.order_no,
                    "conversation_id": customer_conversation.conversation_no,
                    "message_id": notice.message_no,
                    "event_id": event_no,
                },
            )
            _outbox(
                session,
                context,
                "message.sent.v1",
                "conversation",
                customer_conversation.conversation_no,
                customer_conversation.version,
                {
                    "conversation_id": customer_conversation.conversation_no,
                    "message_id": notice.message_no,
                },
            )
            if context.audience == "admin":
                _admin_audit(
                    session,
                    context,
                    action_type,
                    "refund_application",
                    refund.refund_no,
                    {"status": previous_status, "version": refund.version - 1},
                    {
                        "status": refund.refund_status,
                        "version": refund.version,
                        "event": "more_info_requested",
                        "required_materials": requirements,
                    },
                )
            return (
                "已向顾客发送售后补充材料要求。申请仍处于审核中，本次没有批准、拒绝或退款。",
                {
                    "refund_id": refund.refund_no,
                    "order_id": order.order_no,
                    "status": refund.refund_status,
                    "required_materials": requirements,
                    "conversation_id": customer_conversation.conversation_no,
                    "message_id": notice.message_no,
                    "version": refund.version,
                },
                refund.refund_no,
            )
        settings = get_settings()
        after_sale_service = AfterSaleService(session, settings, SecurityService(settings))
        decision = cast(Literal["approve", "reject"], str(payload.get("decision") or ""))
        try:
            decision_result = await after_sale_service.request_refund_decision(
                access,
                refund.refund_no,
                AdminRefundDecisionRequest(
                    decision=decision,
                    reason_code=("AGENT_APPROVED" if decision == "approve" else "AGENT_REJECTED"),
                    reason=str(payload.get("reason") or ""),
                    approved_amount=None,
                ),
                refund.version,
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "售后状态已经变化，请重新核对后再处理。") from exc
        result_payload = decision_result.model_dump(mode="json")
        if result_payload.get("command_status") == "approval_required":
            approval_id = str(result_payload.get("approval_request_id") or "")
            return (
                "该售后金额触发平台双人复核，我已创建复核任务。复核完成前不会执行退款。",
                {
                    "refund_id": refund.refund_no,
                    "order_id": order.order_no,
                    "status": "approval_required",
                    "approval_request_id": approval_id,
                    "required_approval_count": result_payload.get("required_approval_count"),
                },
                approval_id or refund.refund_no,
            )
        return (
            (
                "已同意售后申请，后续将按退款类型进入退款或退货流程。"
                if decision == "approve"
                else "已拒绝售后申请，原因已写入售后状态流水。"
            ),
            {
                "refund_id": refund.refund_no,
                "order_id": order.order_no,
                "status": result_payload.get("refund_status"),
                "decision": decision,
                "version": result_payload.get("version"),
            },
            refund.refund_no,
        )

    if action_type == "admin_user_cart_clear":
        user = await session.scalar(
            select(User)
            .where(User.user_no == payload.get("user_no"), User.deleted_at.is_(None))
            .with_for_update()
        )
        if user is None or user.id == context.user.id:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标用户不再可操作。")
        _require_version(user.version, expected.get("user"))
        cart = await session.scalar(select(Cart).where(Cart.user_id == user.id).with_for_update())
        if cart is None:
            raise OperationsActionConflict("AGENT_ACTION_RESOURCE_CHANGED", "用户购物车已经为空。")
        _require_version(cart.version, expected.get("cart"))
        item_count = int(
            await session.scalar(
                select(func.count()).select_from(CartItem).where(CartItem.cart_id == cart.id)
            )
            or 0
        )
        _require_version(item_count, expected.get("cart_item_count"))
        if item_count == 0:
            raise OperationsActionConflict("AGENT_ACTION_RESOURCE_CHANGED", "用户购物车已经为空。")
        try:
            cart_view = await CartService(session).clear_all(user, cart.version)
        except Exception as exc:
            raise _service_conflict(exc, "购物车已经变化，请重新查看后再操作。") from exc
        _admin_audit(
            session,
            context,
            action_type,
            "cart",
            cart.cart_no,
            {"item_count": item_count},
            {"item_count": 0},
        )
        return (
            f"已清空用户“{user.username}”的购物车，共移除 {item_count} 种商品。",
            {
                "user_id": user.user_no,
                "username": user.username,
                "status": "empty",
                "removed_item_count": item_count,
                "cart_total_quantity": cart_view.cart_total_quantity,
                "cart_version": cart_view.version,
            },
            cart.cart_no,
        )

    if action_type in {"admin_user_cart_item_update", "admin_user_cart_item_delete"}:
        user = await session.scalar(
            select(User)
            .where(User.user_no == payload.get("user_no"), User.deleted_at.is_(None))
            .with_for_update()
        )
        if user is None or user.id == context.user.id:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标用户不再可操作。")
        _require_version(user.version, expected.get("user"))
        cart = await session.scalar(select(Cart).where(Cart.user_id == user.id).with_for_update())
        if cart is None:
            raise OperationsActionConflict("AGENT_ACTION_RESOURCE_CHANGED", "用户购物车已经为空。")
        _require_version(cart.version, expected.get("cart"))
        cart_item = await session.scalar(
            select(CartItem)
            .where(
                CartItem.cart_id == cart.id,
                CartItem.cart_item_no == payload.get("cart_item_no"),
            )
            .with_for_update()
        )
        if cart_item is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "目标购物车商品已经不存在。"
            )
        _require_version(cart_item.version, expected.get("cart_item"))
        cart_sku = await session.scalar(select(ProductSku).where(ProductSku.id == cart_item.sku_id))
        cart_product = (
            await session.scalar(select(Product).where(Product.id == cart_sku.product_id))
            if cart_sku is not None
            else None
        )
        if cart_sku is None or cart_product is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "购物车关联商品已经失效。"
            )
        _require_version(cart_sku.version, expected.get("sku"))
        _require_version(cart_product.version, expected.get("product"))
        cart_service = CartService(session)
        try:
            if action_type == "admin_user_cart_item_update":
                quantity = int(str(payload.get("quantity") or 0))
                cart_view = await cart_service.patch(
                    user,
                    cart_item.cart_item_no,
                    CartItemPatchRequest(quantity=quantity),
                    cart.version,
                )
                answer = (
                    f"用户“{user.username}”购物车中的“{cart_product.product_name}”"
                    f"已改为 {quantity} 件。"
                )
            else:
                cart_view = await cart_service.delete(user, cart_item.cart_item_no, cart.version)
                answer = f"已从用户“{user.username}”的购物车移除“{cart_product.product_name}”。"
        except Exception as exc:
            raise _service_conflict(exc, "购物车已经变化，请重新查看后再操作。") from exc
        _admin_audit(
            session,
            context,
            action_type,
            "cart_item",
            cart_item.cart_item_no,
            {"quantity": cart_item.quantity},
            {
                "quantity": payload.get("quantity"),
                "removed": action_type == "admin_user_cart_item_delete",
            },
        )
        return (
            answer,
            {
                "user_id": user.user_no,
                "username": user.username,
                "product_name": cart_product.product_name,
                "sku_name": cart_sku.sku_name,
                "quantity": payload.get("quantity"),
                "cart_total_quantity": cart_view.cart_total_quantity,
                "cart_version": cart_view.version,
            },
            cart_item.cart_item_no,
        )

    if action_type in {
        "admin_user_address_create",
        "admin_user_address_update",
        "admin_user_address_delete",
        "admin_user_address_set_default",
    }:
        user = await session.scalar(
            select(User)
            .where(User.user_no == payload.get("user_no"), User.deleted_at.is_(None))
            .with_for_update()
        )
        if user is None or user.id == context.user.id:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标用户不再可操作。")
        _require_version(user.version, expected.get("user"))
        identity = IdentityService(
            session,
            cast(Any, None),
            SecurityService(get_settings()),
            get_settings(),
        )
        address_before: dict[str, object] = {}
        try:
            if action_type == "admin_user_address_create":
                current = await identity.list_addresses(user.id)
                _require_version(current.active_count, expected.get("address_count"))
                address_view = await identity.create_address(
                    user,
                    AddressWrite(
                        recipient_name=str(payload.get("recipient_name") or ""),
                        phone=str(payload.get("phone") or ""),
                        country_code="CN",
                        province_code=str(payload.get("province_code") or ""),
                        city_code=str(payload.get("city_code") or ""),
                        district_code=str(payload.get("district_code") or ""),
                        address=str(payload.get("address") or ""),
                        is_default=bool(payload.get("is_default")),
                    ),
                    action.idempotency_key,
                )
                address_no = address_view.address_id
                answer = f"已为用户“{user.username}”新增收货地址。"
            else:
                address = await session.scalar(
                    select(UserAddress)
                    .where(
                        UserAddress.user_id == user.id,
                        UserAddress.address_no == payload.get("address_no"),
                        UserAddress.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if address is None:
                    raise OperationsActionConflict(
                        "AGENT_ACTION_RESOURCE_CHANGED", "目标地址已经不存在。"
                    )
                _require_version(address.version, expected.get("address"))
                address_no = address.address_no
                address_before = {
                    "address_no": address_no,
                    "province_code": address.province_code,
                    "city_code": address.city_code,
                    "district_code": address.district_code,
                    "is_default": address.is_default,
                    "version": address.version,
                }
                if action_type == "admin_user_address_update":
                    patch_values = {
                        key: payload[key]
                        for key in (
                            "recipient_name",
                            "phone",
                            "province_code",
                            "city_code",
                            "district_code",
                            "address",
                        )
                        if key in payload
                    }
                    address_view = await identity.update_address(
                        user.id,
                        address_no,
                        AddressPatch.model_validate(patch_values),
                        address.version,
                    )
                    answer = f"已更新用户“{user.username}”的指定收货地址。"
                elif action_type == "admin_user_address_set_default":
                    address_view = await identity.set_default_address(user.id, address_no)
                    answer = f"已把用户“{user.username}”的指定地址设为默认收货地址。"
                else:
                    await identity.delete_address(user.id, address_no, address.version)
                    address_view = None
                    answer = f"已删除用户“{user.username}”的指定收货地址。"
            address_list = await identity.list_addresses(user.id)
        except OperationsActionConflict:
            raise
        except Exception as exc:
            raise _service_conflict(exc, "地址簿已经变化，请重新查看后再操作。") from exc
        _admin_audit(
            session,
            context,
            action_type,
            "user_address",
            address_no,
            address_before,
            {
                "is_default": address_view.is_default if address_view is not None else False,
                "deleted": action_type == "admin_user_address_delete",
            },
        )
        return (
            answer,
            {
                "user_id": user.user_no,
                "username": user.username,
                "address_id": address_no,
                "status": (
                    "deleted"
                    if address_view is None
                    else "default"
                    if address_view.is_default
                    else "active"
                ),
                "recipient_name": address_view.recipient_name if address_view is not None else None,
                "phone_masked": address_view.phone_masked if address_view is not None else None,
                "region": (
                    region_label(
                        address_view.province_code,
                        address_view.city_code,
                        address_view.district_code,
                    )
                    if address_view is not None
                    else None
                ),
                "address": address_view.address if address_view is not None else None,
                "active_address_count": address_list.active_count,
                "version": address_view.version if address_view is not None else None,
            },
            address_no,
        )

    if action_type in {
        "admin_user_favorite_product_remove",
        "admin_user_favorite_store_remove",
    }:
        user = await session.scalar(
            select(User)
            .where(User.user_no == payload.get("user_no"), User.deleted_at.is_(None))
            .with_for_update()
        )
        if user is None or user.id == context.user.id:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标用户不再可操作。")
        _require_version(user.version, expected.get("user"))
        settings = get_settings()
        if action_type == "admin_user_favorite_product_remove":
            favorite_row = (
                await session.execute(
                    select(ProductFavorite, Product)
                    .join(Product, Product.id == ProductFavorite.product_id)
                    .where(
                        Product.product_no == payload.get("product_no"),
                        ProductFavorite.user_id == user.id,
                        ProductFavorite.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if favorite_row is None:
                raise OperationsActionConflict(
                    "AGENT_ACTION_RESOURCE_CHANGED", "目标商品已经不在该用户收藏中。"
                )
            favorite, product = favorite_row
            _require_version(favorite.version, expected.get("favorite"))
            _require_version(product.version, expected.get("product"))
            try:
                await CatalogService(session, settings).remove_favorite_as_admin(
                    user.id, product.product_no
                )
            except Exception as exc:
                raise _service_conflict(exc, "商品收藏已经变化，请重新查看后再操作。") from exc
            entity_name = product.product_name
            entity_no = product.product_no
            target_type = "product_favorite"
        else:
            follow_row = (
                await session.execute(
                    select(StoreFollow, Store)
                    .join(Store, Store.id == StoreFollow.store_id)
                    .where(
                        Store.store_no == payload.get("store_no"),
                        StoreFollow.user_id == user.id,
                        StoreFollow.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if follow_row is None:
                raise OperationsActionConflict(
                    "AGENT_ACTION_RESOURCE_CHANGED", "目标店铺已经不在该用户收藏中。"
                )
            follow, store = follow_row
            _require_version(follow.version, expected.get("favorite"))
            _require_version(store.version, expected.get("store"))
            try:
                await StoreService(session, settings).remove_follow_as_admin(
                    user.id, store.store_no
                )
            except Exception as exc:
                raise _service_conflict(exc, "店铺收藏已经变化，请重新查看后再操作。") from exc
            entity_name = store.store_name
            entity_no = store.store_no
            target_type = "store_follow"
        remaining_products = int(
            await session.scalar(
                select(func.count(ProductFavorite.id)).where(
                    ProductFavorite.user_id == user.id,
                    ProductFavorite.deleted_at.is_(None),
                )
            )
            or 0
        )
        remaining_stores = int(
            await session.scalar(
                select(func.count(StoreFollow.id)).where(
                    StoreFollow.user_id == user.id,
                    StoreFollow.deleted_at.is_(None),
                )
            )
            or 0
        )
        _admin_audit(
            session,
            context,
            action_type,
            target_type,
            entity_no,
            {"favorited": True},
            {"favorited": False},
        )
        return (
            f"已取消用户“{user.username}”对“{entity_name}”的收藏。",
            {
                "user_id": user.user_no,
                "username": user.username,
                "target_name": entity_name,
                "status": "removed",
                "favorite_product_count": remaining_products,
                "followed_store_count": remaining_stores,
            },
            entity_no,
        )

    if action_type == "admin_user_create":
        username = str(payload.get("new_username") or "")
        email = str(payload.get("new_email") or "")
        ciphertext_hex = str(payload.get("password_ciphertext") or "")
        try:
            normalized_username = normalize_username(username)
            normalized_email = normalize_target("email", email)
            password = SecurityService(get_settings()).decrypt(
                "agent-action:user-password", bytes.fromhex(ciphertext_hex)
            )
            request = AdminUserCreateRequest(
                username=username,
                password=password,
                email=normalized_email,
            )
        except (TypeError, ValueError) as exc:
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "用户创建资料校验失败，本次没有创建账号。"
            ) from exc
        if expected.get("username_available") != normalized_username:
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "用户名确认信息不一致，本次没有创建账号。"
            )
        duplicate = await session.scalar(
            select(User.id).where(User.username_normalized == normalized_username)
        )
        if duplicate is not None:
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "该用户名刚刚已被使用，请更换用户名后重试。"
            )
        access = await _operations_admin_access(session, context, "users:manage")
        try:
            user_result = await RbacService(session, SecurityService(get_settings())).create_user(
                access, request, action.idempotency_key
            )
        except Exception as exc:
            raise _service_conflict(exc, "用户创建条件已经变化，请重新核对。") from exc
        created_user = await session.scalar(
            select(User).where(User.user_no == user_result.user_id).with_for_update()
        )
        created_credential = (
            await session.scalar(
                select(UserCredential)
                .where(
                    UserCredential.user_id == created_user.id,
                    UserCredential.credential_type == "password",
                    UserCredential.credential_status == "active",
                )
                .with_for_update()
            )
            if created_user is not None
            else None
        )
        if created_credential is None:
            raise OperationsActionConflict(
                "USER_PASSWORD_NOT_CONFIGURED", "用户已创建但初始凭证状态异常，请由管理员核查。"
            )
        if not created_credential.must_change_password:
            created_credential.must_change_password = True
            created_credential.credential_version += 1
        created_wallet = await session.scalar(
            select(UserWallet).where(
                UserWallet.user_id == created_user.id,
                UserWallet.currency == "CNY",
            )
        )
        if created_wallet is None:
            raise OperationsActionConflict(
                "USER_WALLET_NOT_CONFIGURED", "用户已创建但钱包初始化异常，请由管理员核查。"
            )
        return (
            f"普通用户“{user_result.username}”已创建；首次使用前需通过恢复邮箱重置密码。",
            {
                "user_id": user_result.user_id,
                "username": user_result.username,
                "status": user_result.account_status,
                "balance_minor": int(created_wallet.balance_amount),
                "credential_status": "必须通过恢复邮箱重置",
                "version": user_result.version,
            },
            user_result.user_id,
        )

    if action_type == "admin_user_password_reset_requirement":
        user = await session.scalar(
            select(User)
            .where(User.user_no == payload.get("user_no"), User.deleted_at.is_(None))
            .with_for_update()
        )
        if user is None or user.id == context.user.id:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标用户不再可重置密码。")
        _require_version(user.version, expected.get("user"))
        credential = await session.scalar(
            select(UserCredential)
            .where(
                UserCredential.user_id == user.id,
                UserCredential.credential_type == "password",
                UserCredential.credential_status == "active",
            )
            .with_for_update()
        )
        if credential is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "目标用户的密码凭证已失效。"
            )
        _require_version(credential.credential_version, expected.get("credential"))
        active_session_count = int(
            await session.scalar(
                select(func.count(AuthSession.id)).where(
                    AuthSession.user_id == user.id,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > utc_now(),
                )
            )
            or 0
        )
        access = await _operations_admin_access(session, context, "users:force_password_reset")
        try:
            await RbacService(session, SecurityService(get_settings())).require_password_reset(
                access,
                user.user_no,
                "超级管理员通过 AI 管家要求用户重置密码",
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "用户密码凭证已经变化，请重新发起重置。") from exc
        return (
            f"用户“{user.username}”已被要求重置密码，原有登录会话已全部撤销。",
            {
                "user_id": user.user_no,
                "username": user.username,
                "status": user.user_status,
                "credential_status": "必须通过恢复邮箱重置",
                "revoked_session_count": active_session_count,
            },
            user.user_no,
        )

    if action_type == "admin_user_profile":
        user = await session.scalar(
            select(User)
            .where(User.user_no == payload.get("user_no"), User.deleted_at.is_(None))
            .with_for_update()
        )
        if user is None or user.id == context.user.id:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标用户不再可操作。")
        _require_version(user.version, expected.get("user"))
        user_request_fields: dict[str, object] = {}
        if isinstance(payload.get("username"), str):
            user_request_fields["username"] = payload["username"]
        if isinstance(payload.get("email"), str):
            user_request_fields["email"] = payload["email"]
        if not user_request_fields:
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "没有可执行的用户资料修改字段。"
            )
        access = await _operations_admin_access(session, context, "users:manage")
        settings = get_settings()
        try:
            user_result = await RbacService(session, SecurityService(settings)).update_user(
                access,
                user.user_no,
                AdminUserUpdateRequest.model_validate(user_request_fields),
                user.version,
            )
        except Exception as exc:
            raise _service_conflict(exc, "用户资料已经变化，请重新核对。") from exc
        return (
            f"用户“{user_result.username}”的账号资料已更新，我已回读最新版本。",
            {
                "user_id": user_result.user_id,
                "username": user_result.username,
                "status": user_result.account_status,
                "version": user_result.version,
            },
            user_result.user_id,
        )

    if action_type == "admin_user_delete":
        user = await session.scalar(
            select(User)
            .where(User.user_no == payload.get("user_no"), User.deleted_at.is_(None))
            .with_for_update()
        )
        if user is None or user.id == context.user.id:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标用户不再可注销。")
        _require_version(user.version, expected.get("user"))
        access = await _operations_admin_access(session, context, "users:manage")
        settings = get_settings()
        try:
            deletion_target = await RbacService(
                session, SecurityService(settings)
            ).prepare_user_deletion(access, user.user_no, user.version)
            task = await AccountDeletionService(session).delete_consumer(deletion_target)
        except Exception as exc:
            raise _service_conflict(exc, "用户注销资格已经变化，请重新核对。") from exc
        return (
            f"用户“{user.username}”已进入安全注销任务，登录会话已撤销。",
            {
                "user_id": user.user_no,
                "username": user.username,
                "deletion_task_id": task.task_no,
                "status": task.task_status,
                "phase": task.current_phase,
            },
            task.task_no,
        )

    if action_type == "admin_store_create":
        store_name = str(payload.get("new_store_name") or "")
        merchant_username = str(payload.get("new_merchant_username") or "")
        merchant_email = str(payload.get("new_merchant_email") or "")
        description_value = payload.get("new_description")
        description = str(description_value) if isinstance(description_value, str) else None
        ciphertext_hex = str(payload.get("password_ciphertext") or "")
        try:
            normalized_name = _normalize_store_name(store_name)
            normalized_username = normalize_username(merchant_username)
            normalized_email = normalize_target("email", merchant_email)
            password = SecurityService(get_settings()).decrypt(
                "agent-action:merchant-password", bytes.fromhex(ciphertext_hex)
            )
            request = AdminStoreCreateRequest(
                store_name=store_name,
                description=description,
                merchant_username=merchant_username,
                merchant_password=password,
                merchant_email=normalized_email,
            )
        except (TypeError, ValueError) as exc:
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "店铺创建资料校验失败，本次没有创建店铺。"
            ) from exc
        if (
            expected.get("store_name_available") != normalized_name
            or expected.get("merchant_username_available") != normalized_username
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "店铺创建确认信息不一致，本次没有创建店铺。"
            )
        if (
            await session.scalar(
                select(Store.id).where(Store.store_name_normalized == normalized_name)
            )
            is not None
            or await session.scalar(
                select(User.id).where(User.username_normalized == normalized_username)
            )
            is not None
        ):
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "店铺名称或商家用户名刚刚已被使用，请修改后重试。"
            )
        access = await _operations_admin_access(session, context, "stores:manage")
        try:
            store_result = await AdminStoreService(session, get_settings()).create_store(
                access, request, action.idempotency_key
            )
        except Exception as exc:
            raise _service_conflict(exc, "店铺创建条件已经变化，请重新核对。") from exc
        owner = await session.scalar(
            select(User).where(User.user_no == store_result.owner_user_id).with_for_update()
        )
        owner_credential = (
            await session.scalar(
                select(UserCredential)
                .where(
                    UserCredential.user_id == owner.id,
                    UserCredential.credential_type == "password",
                    UserCredential.credential_status == "active",
                )
                .with_for_update()
            )
            if owner is not None
            else None
        )
        if owner_credential is None:
            raise OperationsActionConflict(
                "MERCHANT_PASSWORD_NOT_CONFIGURED", "店铺已创建但商家初始凭证异常，请由管理员核查。"
            )
        if not owner_credential.must_change_password:
            owner_credential.must_change_password = True
            owner_credential.credential_version += 1
        return (
            f"店铺“{store_result.store_name}”与独立商家账号已创建；首次使用前需通过恢复邮箱重置密码。",
            {
                "store_id": store_result.store_id,
                "store_name": store_result.store_name,
                "merchant_user_id": store_result.owner_user_id,
                "merchant_username": merchant_username,
                "status": store_result.status,
                "credential_status": "必须通过恢复邮箱重置",
                "version": store_result.version,
            },
            store_result.store_id,
        )

    if action_type == "admin_store_profile":
        store = await session.scalar(
            select(Store).where(Store.store_no == payload.get("store_no")).with_for_update()
        )
        if store is None:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标店铺不再可操作。")
        _require_version(store.version, expected.get("store"))
        store_request_fields: dict[str, object] = {}
        if isinstance(payload.get("store_name"), str):
            store_request_fields["store_name"] = payload["store_name"]
        if "description" in payload:
            store_request_fields["description"] = payload.get("description")
        if not store_request_fields:
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "没有可执行的店铺资料修改字段。"
            )
        access = await _operations_admin_access(session, context, "stores:manage")
        try:
            store_result = await AdminStoreService(session, get_settings()).update_store(
                access,
                store.store_no,
                AdminStoreUpdateRequest.model_validate(store_request_fields),
                store.version,
            )
        except Exception as exc:
            raise _service_conflict(exc, "店铺资料已经变化，请重新核对。") from exc
        return (
            f"店铺“{store_result.store_name}”的公开资料已更新，我已回读最新版本。",
            {
                "store_id": store_result.store_id,
                "store_name": store_result.store_name,
                "description": store_result.description,
                "status": store_result.status,
                "version": store_result.version,
            },
            store_result.store_id,
        )

    if action_type == "admin_store_delete":
        store = await session.scalar(
            select(Store).where(Store.store_no == payload.get("store_no")).with_for_update()
        )
        if store is None:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标店铺不再可注销。")
        _require_version(store.version, expected.get("store"))
        access = await _operations_admin_access(session, context, "stores:manage")
        try:
            owner = await AdminStoreService(session, get_settings()).prepare_store_deletion(
                access,
                store.store_no,
                AdminStoreDeleteRequest(
                    reason="超级管理员通过 AI 管家确认注销店铺",
                    confirmation="DELETE_STORE",
                ),
                store.version,
            )
            task = await AccountDeletionService(session).delete_merchant(owner)
        except Exception as exc:
            raise _service_conflict(exc, "店铺注销资格已经变化，请重新核对。") from exc
        return (
            f"店铺“{store.store_name}”及其商家账号已进入安全注销任务。",
            {
                "store_id": store.store_no,
                "store_name": store.store_name,
                "deletion_task_id": task.task_no,
                "status": task.task_status,
                "phase": task.current_phase,
            },
            task.task_no,
        )

    if action_type == "admin_product_image_description":
        product_store_row = (
            await session.execute(
                select(Product, Store)
                .join(Store, Store.id == Product.store_id)
                .where(
                    Product.product_no == payload.get("product_no"),
                    Store.store_no == payload.get("store_no"),
                    Product.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).one_or_none()
        if product_store_row is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_SCOPE_CHANGED", "目标店铺或商品已经不存在。"
            )
        product, store = product_store_row
        _require_version(product.version, expected.get("product"))
        _require_version(store.version, expected.get("store"))
        current = (
            await session.scalar(
                select(ProductContentVersion).where(
                    ProductContentVersion.id == product.current_detail_content_version_id,
                    ProductContentVersion.product_id == product.id,
                )
            )
            if product.current_detail_content_version_id is not None
            else None
        )
        if current is None or current.safe_blocks is None:
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "商品详情版本已经变化，请重新发起操作。"
            )
        _require_version(current.version, expected.get("content"))
        image_number = int(str(payload.get("image_number") or 0))
        expected_file_id = str(payload.get("file_id") or "")
        description = str(payload.get("description") or "")
        if not 1 <= image_number <= 100 or len(description) > 8000:
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "详情图片序号或图片说明不符合要求。"
            )
        blocks = [dict(item) for item in current.safe_blocks]
        image_indexes = [index for index, item in enumerate(blocks) if item.get("type") == "image"]
        if image_number > len(image_indexes):
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "详情图片顺序已经变化，请重新发起操作。"
            )
        block_index = image_indexes[image_number - 1]
        if str(blocks[block_index].get("file_id") or "") != expected_file_id:
            raise OperationsActionConflict(
                "AGENT_ACTION_RESOURCE_CHANGED", "目标详情图片已经变化，请重新发起操作。"
            )
        if description:
            blocks[block_index]["description"] = description
        else:
            blocks[block_index].pop("description", None)
        access = await _operations_admin_access(session, context, "products:update")
        try:
            content_result = await ProductAdminService(
                session, get_settings()
            ).create_content_version(
                access,
                product.product_no,
                AdminContentVersionCreateRequest(
                    source_format="structured",
                    source_content=json.dumps(blocks, ensure_ascii=False),
                ),
                action.idempotency_key,
            )
        except Exception as exc:
            raise _service_conflict(exc, "商品详情已经变化，请重新核对图片说明。") from exc
        return (
            f"“{store.store_name}”的“{product.product_name}”第 {image_number} 张详情图片说明"
            "已更新，我已回读新的详情版本。",
            {
                "store_id": store.store_no,
                "store_name": store.store_name,
                "product_id": product.product_no,
                "product_name": product.product_name,
                "image_number": image_number,
                "file_id": expected_file_id,
                "image_description": description,
                "content_version_id": content_result.version_id,
                "content_version": content_result.content_version,
            },
            content_result.version_id,
        )

    if action_type in {"merchant_product_profile", "admin_product_profile"}:
        product = await session.scalar(
            select(Product)
            .where(Product.product_no == payload.get("product_no"), Product.deleted_at.is_(None))
            .with_for_update()
        )
        store = await session.scalar(select(Store).where(Store.store_no == payload.get("store_no")))
        if (
            product is None
            or store is None
            or product.store_id != store.id
            or (
                action_type == "merchant_product_profile"
                and (context.store is None or context.store.id != store.id)
            )
        ):
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标商品不再可操作。")
        _require_version(product.version, expected.get("product"))
        _require_version(store.version, expected.get("store"))
        product_request_fields: dict[str, object] = {}
        if isinstance(payload.get("product_name"), str):
            product_request_fields["product_name"] = payload["product_name"]
        if "description" in payload:
            product_request_fields["description"] = payload.get("description")
        if not product_request_fields:
            raise OperationsActionConflict(
                "AGENT_ACTION_ARGUMENT_INVALID", "没有可执行的商品资料修改字段。"
            )
        access = await _operations_admin_access(session, context, "products:update")
        try:
            product_result = await ProductAdminService(session, get_settings()).update_product(
                access,
                product.product_no,
                AdminProductUpdateRequest.model_validate(product_request_fields),
                product.version,
            )
        except Exception as exc:
            raise _service_conflict(exc, "商品资料已经变化，请重新核对。") from exc
        return (
            f"商品“{product_result.product_name}”的基础资料已更新，我已回读最新版本。",
            {
                "product_id": product_result.product_id,
                "product_name": product_result.product_name,
                "description": product_result.description,
                "status": product_result.status,
                "version": product_result.version,
            },
            product_result.product_id,
        )

    if action_type == "admin_product_review":
        product = await session.scalar(
            select(Product)
            .where(Product.product_no == payload.get("product_no"), Product.deleted_at.is_(None))
            .with_for_update()
        )
        store = await session.scalar(select(Store).where(Store.store_no == payload.get("store_no")))
        if product is None or store is None or product.store_id != store.id:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标商品不再可审核。")
        _require_version(product.version, expected.get("product"))
        _require_version(store.version, expected.get("store"))
        review_decision = cast(
            Literal["approve", "reject", "request_changes"], payload.get("decision")
        )
        reason = str(payload.get("reason") or "")
        access = await _operations_admin_access(session, context, "products:review")
        service = ProductAdminService(session, get_settings())
        try:
            reviewed_product = await service.moderate(
                access,
                product.product_no,
                AdminProductModerationRequest(
                    decision=review_decision,
                    reason_code="ADMIN_AGENT_REVIEW",
                    reason=reason,
                ),
                product.version,
                f"{action.idempotency_key}-review",
            )
            if review_decision == "approve":
                publish_access = await _operations_admin_access(
                    session, context, "products:publish"
                )
                reviewed_product = await service.publish(
                    publish_access,
                    product.product_no,
                    AdminProductCommandRequest(
                        reason_code="ADMIN_AGENT_APPROVED",
                        reason=reason,
                    ),
                    reviewed_product.version,
                    f"{action.idempotency_key}-publish",
                )
        except Exception as exc:
            raise _service_conflict(exc, "商品审核条件或版本已经变化，请重新核对。") from exc
        return (
            (
                f"商品“{reviewed_product.product_name}”已审核通过并发布。"
                if review_decision == "approve"
                else f"商品“{reviewed_product.product_name}”已按平台决定退回商家整改。"
            ),
            {
                "product_id": reviewed_product.product_id,
                "product_name": reviewed_product.product_name,
                "decision": review_decision,
                "status": reviewed_product.status,
                "version": reviewed_product.version,
            },
            reviewed_product.product_id,
        )

    if action_type == "admin_user_force_logout":
        user = await session.scalar(
            select(User)
            .where(User.user_no == payload.get("user_no"), User.deleted_at.is_(None))
            .with_for_update()
        )
        if user is None or user.id == context.user.id:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标用户不再可操作。")
        _require_version(user.version, expected.get("user"))
        revoke_result = await session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now, revoke_reason="admin_agent_forced_logout")
        )
        count = int(getattr(revoke_result, "rowcount", 0) or 0)
        _admin_audit(
            session,
            context,
            action_type,
            "user",
            user.user_no,
            {"active_sessions": count},
            {"active_sessions": 0},
        )
        _outbox(
            session,
            context,
            "user.sessions_revoked.v1",
            "user",
            user.user_no,
            user.version,
            {"session_count": count, "source": "agent_confirmed"},
        )
        return (
            f"“{user.username}”的 {count} 个有效会话已撤销，账号和业务数据保持不变。",
            {"user_id": user.user_no, "username": user.username, "revoked_session_count": count},
            user.user_no,
        )

    if action_type == "admin_user_status":
        user = await session.scalar(
            select(User)
            .where(User.user_no == payload.get("user_no"), User.deleted_at.is_(None))
            .with_for_update()
        )
        if user is None or user.id == context.user.id:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标用户不再可操作。")
        _require_version(user.version, expected.get("user"))
        target = str(payload.get("target_status"))
        if (user.user_status, target) not in {("active", "suspended"), ("suspended", "active")}:
            raise OperationsActionConflict(
                "AGENT_ACTION_STATE_CHANGED", "用户状态已经变化，请重新发起操作。"
            )
        previous = user.user_status
        status_record = UserStatusRecord(
            status_record_no=new_prefixed_ulid("usrst_"),
            user_id=user.id,
            from_status=previous,
            to_status=target,
            reason_code="AGENT_CONFIRMED_ACTION",
            reason="由 AI 管家确认卡执行",
            effective_at=now,
            expires_at=None,
            actor_type="admin",
            actor_user_id=context.user.id,
            scope_type="platform",
            scope_id=0,
            expected_user_version=user.version,
            result_user_version=user.version + 1,
            idempotency_key=action.idempotency_key,
            idempotency_scope_key=canonical_request_hash(
                {"action": action.idempotency_key, "user": user.user_no}
            ),
            request_id=context.run.trace_id,
            trace_id=context.run.trace_id,
        )
        session.add(status_record)
        await session.flush()
        user.user_status = target
        user.status_reason_code = "AGENT_CONFIRMED_ACTION" if target == "suspended" else None
        user.status_expires_at = None
        user.current_status_record_id = status_record.id
        user.version += 1
        revoked = 0
        if target == "suspended":
            suspend_result = await session.execute(
                update(AuthSession)
                .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
                .values(revoked_at=now, revoke_reason="admin_agent_suspended")
            )
            revoked = int(getattr(suspend_result, "rowcount", 0) or 0)
        _admin_audit(
            session,
            context,
            action_type,
            "user",
            user.user_no,
            {"status": previous},
            {"status": target, "revoked_sessions": revoked},
        )
        _outbox(
            session,
            context,
            f"user.{'suspended' if target == 'suspended' else 'resumed'}.v1",
            "user",
            user.user_no,
            user.version,
            {"from_status": previous, "to_status": target, "source": "agent_confirmed"},
        )
        return (
            f"“{user.username}”已切换为“{_user_status_label(target)}”"
            f"{f'，并撤销 {revoked} 个会话' if revoked else ''}。",
            {
                "user_id": user.user_no,
                "username": user.username,
                "status": target,
                "revoked_session_count": revoked,
                "version": user.version,
            },
            user.user_no,
        )

    if action_type == "admin_user_wallet_adjust":
        user = await session.scalar(
            select(User)
            .where(User.user_no == payload.get("user_no"), User.deleted_at.is_(None))
            .with_for_update()
        )
        if user is None or user.id == context.user.id:
            raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标用户不再可操作。")
        _require_version(user.version, expected.get("user"))
        wallet = await session.scalar(
            select(UserWallet)
            .where(UserWallet.user_id == user.id, UserWallet.currency == "CNY")
            .with_for_update()
        )
        expected_wallet_version = int(str(expected.get("wallet") or 0))
        if wallet is None:
            if expected_wallet_version != 0:
                raise OperationsActionConflict(
                    "AGENT_ACTION_RESOURCE_CHANGED", "用户钱包已经变化，请重新发起操作。"
                )
            wallet = UserWallet(
                wallet_no=new_prefixed_ulid("wal_"),
                user_id=user.id,
                balance_amount=0,
                total_recharged_amount=0,
                currency="CNY",
                wallet_status="active",
            )
            session.add(wallet)
            await session.flush()
        else:
            _require_version(wallet.version, expected_wallet_version)
        if wallet.wallet_status != "active":
            raise OperationsActionConflict("WALLET_NOT_ACTIVE", "该用户钱包当前不可调整。")
        direction = str(payload.get("direction"))
        amount_minor = int(str(payload.get("amount_minor") or 0))
        if direction not in {"credit", "debit"} or not 1 <= amount_minor <= 100_000_000:
            raise OperationsActionConflict("AGENT_ACTION_ARGUMENT_INVALID", "调整方向或金额无效。")
        before = wallet.balance_amount
        if direction == "debit" and amount_minor > before:
            raise OperationsActionConflict(
                "WALLET_INSUFFICIENT_BALANCE", "扣减金额不能超过用户当前余额。"
            )
        after = before + amount_minor if direction == "credit" else before - amount_minor
        transaction = WalletTransaction(
            transaction_no=new_prefixed_ulid("wtx_"),
            wallet_id=wallet.id,
            transaction_type="admin_adjustment",
            direction=direction,
            amount=amount_minor,
            balance_before=before,
            balance_after=after,
            currency="CNY",
            business_type="agent_admin_adjustment",
            business_no=action.action_no,
            channel="admin_agent",
            description="超级管理员通过 AI 管家确认调整账户余额",
            occurred_at=now,
        )
        session.add(transaction)
        wallet.balance_amount = after
        wallet.version += 1
        _admin_audit(
            session,
            context,
            action_type,
            "user_wallet",
            wallet.wallet_no,
            {"balance_minor": before, "currency": "CNY"},
            {
                "balance_minor": after,
                "currency": "CNY",
                "transaction_no": transaction.transaction_no,
            },
        )
        _outbox(
            session,
            context,
            "user.wallet.adjusted.v1",
            "user_wallet",
            wallet.wallet_no,
            wallet.version,
            {
                "user_no": user.user_no,
                "transaction_no": transaction.transaction_no,
                "direction": direction,
                "amount_minor": amount_minor,
                "balance_minor": after,
            },
        )
        return (
            f"“{user.username}”账户余额已{('增加' if direction == 'credit' else '扣减')}"
            f" {_money(amount_minor)}，最新余额为 {_money(after)}。",
            {
                "user_id": user.user_no,
                "username": user.username,
                "direction": direction,
                "amount_minor": amount_minor,
                "balance_minor": after,
                "balance_display": _money(after),
                "transaction_id": transaction.transaction_no,
            },
            transaction.transaction_no,
        )

    raise OperationsActionConflict(
        "AGENT_ACTION_UNSUPPORTED", "该确认操作当前不受支持，没有修改业务数据。"
    )


async def _approval_message(
    session: AsyncSession,
    context: TrustedOperationsContext,
    approval: AgentToolApproval,
    *,
    execution_trace: dict[str, object] | None = None,
) -> None:
    now = utc_now()
    conversation = await lock_conversation_for_append(session, context.conversation.id)
    conversation.last_sequence_no += 1
    conversation.last_message_at = now
    conversation.version += 1
    payload = approval.action_payload or {}
    message = Message(
        message_no=new_prefixed_ulid("msg_"),
        conversation_id=conversation.id,
        sequence_no=conversation.last_sequence_no,
        client_message_no=None,
        sender_type="agent",
        sender_id=None,
        message_type="agent_action_approval",
        text_content=str(payload.get("summary") or "请核对本次操作后确认。")[:4000],
        content_payload={
            "run_id": context.run.run_no,
            "approval_id": approval.approval_no,
            "approval_version": approval.version,
            "action_type": approval.action_type,
            "title": payload.get("title"),
            "summary": payload.get("summary"),
            "target_label": payload.get("target_label"),
            "changes": payload.get("changes"),
            "tool_code": payload.get("tool_code"),
            "expires_at": approval.expires_at.isoformat() + "Z",
            "requires_explicit_confirmation": True,
            "approval_status": "pending",
            "execution_status": "waiting_confirmation",
            "data_scope": context.trusted_scope,
            "execution_trace": execution_trace or {},
        },
        agent_version_id=context.agent_version.id,
        ai_run_no=context.run.run_no,
        message_status="sent",
        moderation_status="passed",
        sent_at=now,
    )
    session.add(message)
    await session.flush()
    conversation.last_message_id = message.id
    context.run.response_message_id = message.id
    context.run.public_output = message.text_content
    _outbox(
        session,
        context,
        "message.sent.v1",
        "conversation",
        conversation.conversation_no,
        conversation.version,
        {"conversation_id": conversation.conversation_no, "message_id": message.message_no},
    )


async def _settle_approval_message(
    session: AsyncSession,
    approval: AgentToolApproval,
    execution_status: str,
    error_code: str | None,
) -> None:
    message = await session.scalar(
        select(Message)
        .where(
            Message.conversation_id == approval.conversation_id,
            Message.ai_run_no.is_not(None),
            Message.message_type == "agent_action_approval",
            Message.content_payload["approval_id"].as_string() == approval.approval_no,
        )
        .with_for_update()
    )
    if message is None:
        return
    message.content_payload = {
        **(message.content_payload or {}),
        "approval_status": approval.approval_status,
        "approval_version": approval.version,
        "execution_status": execution_status,
        "execution_error_code": error_code,
    }
    message.version += 1


async def _store_products(session: AsyncSession, store_id: int) -> list[Product]:
    return list(
        (
            await session.scalars(
                select(Product)
                .where(Product.store_id == store_id, Product.deleted_at.is_(None))
                .order_by(Product.id)
            )
        ).all()
    )


async def _product_skus(session: AsyncSession, product_id: int) -> list[ProductSku]:
    return list(
        (
            await session.scalars(
                select(ProductSku)
                .where(ProductSku.product_id == product_id, ProductSku.sku_status == "active")
                .order_by(ProductSku.id)
            )
        ).all()
    )


async def _locked_merchant_sku(
    session: AsyncSession, context: TrustedOperationsContext, payload: dict[str, object]
) -> tuple[Product, ProductSku, Inventory]:
    assert context.store is not None
    product = await session.scalar(
        select(Product)
        .where(
            Product.product_no == payload.get("product_no"),
            Product.store_id == context.store.id,
            Product.deleted_at.is_(None),
        )
        .with_for_update()
    )
    sku = await session.scalar(
        select(ProductSku)
        .where(ProductSku.sku_no == payload.get("sku_no"), ProductSku.store_id == context.store.id)
        .with_for_update()
    )
    inventory = (
        await session.scalar(select(Inventory).where(Inventory.sku_id == sku.id).with_for_update())
        if sku
        else None
    )
    if product is None or sku is None or inventory is None or sku.product_id != product.id:
        raise OperationsActionConflict("AGENT_ACTION_SCOPE_CHANGED", "目标商品款式不再可访问。")
    return product, sku, inventory


def _match_product(products: list[Product], value: str) -> tuple[Product | None, str | None]:
    matches = [
        item
        for item in products
        if item.product_no in value or _compact(item.product_name) in _compact(value)
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "匹配到多个本店商品，请补充完整商品名称。"
    if len(products) == 1:
        return products[0], None
    return None, "请在消息中写出要操作的商品名称。"


def _match_sku(skus: list[ProductSku], value: str) -> tuple[ProductSku | None, str | None]:
    matches = [
        item for item in skus if item.sku_no in value or _compact(item.sku_name) in _compact(value)
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "匹配到多个款式，请补充完整款式名称。"
    if len(skus) == 1:
        return skus[0], None
    return None, "该商品有多个款式，请说明要操作的款式名称。"


def _match_user(users: list[User], value: str) -> tuple[User | None, str | None]:
    matches = [
        item for item in users if item.user_no in value or _mentions_username(item.username, value)
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "匹配到多个用户，请提供完整用户名或用户 ID。"
    return None, "没有找到消息中指定的用户，请核对用户名。"


def _match_store(stores: list[Store], value: str) -> tuple[Store | None, str | None]:
    matches = [
        item
        for item in stores
        if item.store_no in value or _compact(item.store_name) in _compact(value)
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "匹配到多个店铺，请提供完整店铺名称或店铺 ID。"
    return None, "没有找到消息中指定的店铺，请核对店铺名称。"


def _match_admin_product(
    rows: list[tuple[Product, Store]], value: str
) -> tuple[Product | None, Store | None, str | None]:
    matches = [
        (product, store)
        for product, store in rows
        if product.product_no in value
        or (
            _compact(product.product_name) in _compact(value)
            and (
                _compact(store.store_name) in _compact(value)
                or sum(
                    1 for p, _ in rows if _compact(p.product_name) == _compact(product.product_name)
                )
                == 1
            )
        )
    ]
    if len(matches) == 1:
        return matches[0][0], matches[0][1], None
    if len(matches) > 1:
        return None, None, "匹配到多个同名商品，请同时说明店铺名称。"
    return None, None, "没有找到消息中指定的商品，请核对店铺与商品名称。"


async def _prepare_merchant_shipment_create(
    session: AsyncSession, context: TrustedOperationsContext, value: str
) -> tuple[PreparedOperationsAction | None, str | None]:
    assert context.store is not None
    rows = (
        await session.execute(
            select(Order, User)
            .join(User, User.id == Order.user_id)
            .where(
                Order.store_id == context.store.id,
                Order.order_status == "pending_shipment",
                Order.payment_status == "paid",
                Order.fulfillment_status.in_(("unfulfilled", "partial")),
            )
            .order_by(Order.created_at.desc())
        )
    ).all()
    order, customer, error = _match_merchant_order([(row[0], row[1]) for row in rows], value)
    if order is None or customer is None:
        return None, error or "请提供待发货订单号或顾客用户名。"
    order_items = list(
        (
            await session.scalars(
                select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
            )
        ).all()
    )
    if not order_items:
        return None, "该订单没有可发货的商品明细。"
    items = [
        {"order_item_id": item.order_item_no, "quantity": item.quantity - item.refunded_quantity}
        for item in order_items
        if item.quantity > item.refunded_quantity
    ]
    if not items:
        return None, "该订单当前没有剩余可发货数量。"
    tracking_no = f"FX{new_prefixed_ulid('trk_')[4:28]}"
    return PreparedOperationsAction(
        action_type="merchant_shipment_create",
        title="确认创建发货包裹",
        summary="将为该订单全部剩余商品创建模拟物流包裹; 物流节点不会自动推进。",
        target_label=f"订单 {order.order_no} · {customer.username}",
        payload={
            "order_no": order.order_no,
            "tracking_no": tracking_no,
            "items": items,
        },
        resource_versions={"order": order.version, "store": context.store.version},
        changes=(
            {"label": "订单", "value": order.order_no},
            {"label": "顾客", "value": customer.username},
            {"label": "承运方式", "value": "商城模拟物流"},
            {"label": "发货商品项", "value": f"{len(items)} 项"},
        ),
        tool_code="store_ops.shipment.create.commit",
    ), None


async def _prepare_merchant_shipment_progress(
    session: AsyncSession, context: TrustedOperationsContext, value: str
) -> tuple[PreparedOperationsAction | None, str | None]:
    assert context.store is not None
    rows = (
        await session.execute(
            select(Shipment, Order)
            .join(Order, Order.id == Shipment.order_id)
            .where(
                Shipment.store_id == context.store.id,
                Shipment.carrier_code == "fake_express",
                Shipment.shipment_status.not_in(("delivered", "returned", "closed", "voided")),
            )
            .order_by(Shipment.created_at.desc())
        )
    ).all()
    shipment: Shipment | None = None
    order: Order | None = None
    error: str | None = None
    if _uses_recent_reference(value):
        latest_resource = await session.scalar(
            select(AgentToolAction.resource_no)
            .join(AgentToolApproval, AgentToolApproval.id == AgentToolAction.approval_id)
            .where(
                AgentToolApproval.conversation_id == context.conversation.id,
                AgentToolAction.action_status == "succeeded",
                AgentToolAction.action_type.in_(
                    ("merchant_shipment_create", "merchant_shipment_progress")
                ),
                AgentToolAction.resource_no.is_not(None),
            )
            .order_by(AgentToolAction.finished_at.desc(), AgentToolAction.id.desc())
            .limit(1)
        )
        recent = [item for item in rows if item[0].shipment_no == latest_resource]
        if len(recent) == 1:
            shipment, order = recent[0]
    if shipment is None or order is None:
        shipment, order, error = _match_merchant_shipment([(row[0], row[1]) for row in rows], value)
    if shipment is None or order is None:
        return None, error or "请提供包裹号或订单号。"
    event_type = _requested_shipment_event(value)
    if event_type is None:
        return None, "请说明要更新为已揽收、运输中、派送中、已签收、异常或退回。"
    descriptions = {
        "picked_up": "包裹已由承运商揽收",
        "in_transit": "包裹正在运输途中",
        "out_for_delivery": "包裹正在派送中",
        "delivered": "包裹已签收",
        "exception": "包裹运输出现异常",
        "returned": "包裹已退回",
    }
    target_status = "in_transit" if event_type == "out_for_delivery" else event_type
    return PreparedOperationsAction(
        action_type="merchant_shipment_progress",
        title="确认更新物流节点",
        summary="该节点会立即展示给顾客，并可能推动订单履约状态。请按实际物流进度确认。",
        target_label=f"订单 {order.order_no} · 包裹 {shipment.shipment_no}",
        payload={
            "shipment_no": shipment.shipment_no,
            "event_type": event_type,
            "description": descriptions[event_type],
            "location_text": _requested_location(value),
        },
        resource_versions={"shipment": shipment.version, "order": order.version},
        changes=(
            {"label": "当前节点", "value": _shipment_status_label(shipment.shipment_status)},
            {"label": "更新为", "value": _shipment_status_label(target_status, event_type)},
            {"label": "位置", "value": _requested_location(value) or "未填写"},
        ),
        tool_code="store_ops.shipment.progress.commit",
    ), None


async def _prepare_admin_shipment_progress(
    session: AsyncSession, context: TrustedOperationsContext, value: str
) -> tuple[PreparedOperationsAction | None, str | None]:
    """Prepare one explicit fake-carrier transition for the platform operator.

    The target always comes from the business database.  A model-provided store,
    user or shipment scope is never trusted, and a request matching more than one
    package stops for clarification instead of choosing the newest package.
    """

    rows = list(
        (
            await session.execute(
                select(Shipment, Order, Store, User)
                .join(Order, Order.id == Shipment.order_id)
                .join(Store, Store.id == Shipment.store_id)
                .join(User, User.id == Order.user_id)
                .where(
                    Shipment.carrier_code == "fake_express",
                    Shipment.shipment_status.not_in(("delivered", "returned", "closed", "voided")),
                )
                .order_by(Shipment.created_at.desc(), Shipment.id.desc())
                .limit(100)
            )
        ).all()
    )
    shipment: Shipment | None = None
    order: Order | None = None
    store: Store | None = None
    customer: User | None = None
    if _uses_recent_reference(value):
        latest_resource = await session.scalar(
            select(AgentToolAction.resource_no)
            .join(AgentToolApproval, AgentToolApproval.id == AgentToolAction.approval_id)
            .where(
                AgentToolApproval.conversation_id == context.conversation.id,
                AgentToolAction.action_status == "succeeded",
                AgentToolAction.action_type == "admin_shipment_progress",
                AgentToolAction.resource_no.is_not(None),
            )
            .order_by(AgentToolAction.finished_at.desc(), AgentToolAction.id.desc())
            .limit(1)
        )
        recent = [row for row in rows if row[0].shipment_no == latest_resource]
        if len(recent) == 1:
            shipment, order, store, customer = recent[0]
    if shipment is None:
        compact_value = _compact(value)
        exact_matches = [
            row
            for row in rows
            if row[0].shipment_no.casefold() in value.casefold()
            or row[1].order_no.casefold() in value.casefold()
        ]
        matches = exact_matches
        if not matches:
            mentioned_stores = {
                row[2].id for row in rows if _compact(row[2].store_name) in compact_value
            }
            mentioned_customers = {
                row[3].id for row in rows if _mentions_username(row[3].username, value)
            }
            if mentioned_stores or mentioned_customers:
                matches = [
                    row
                    for row in rows
                    if (not mentioned_stores or row[2].id in mentioned_stores)
                    and (not mentioned_customers or row[3].id in mentioned_customers)
                ]
        if len(matches) == 1:
            shipment, order, store, customer = matches[0]
        elif len(matches) > 1:
            return None, "匹配到多个可推进包裹，请提供完整包裹编号。"
        elif len(rows) == 1:
            shipment, order, store, customer = rows[0]
        elif not rows:
            return None, "平台当前没有可手动推进的商城模拟物流包裹。"
        else:
            return None, "平台有多个可推进包裹，请提供完整订单号或包裹号。"
    assert shipment is not None and order is not None and store is not None and customer is not None
    event_type = _requested_shipment_event(value)
    if event_type is None:
        return None, "请明确更新为已揽收、运输中、派送中、已签收、异常或退回。"
    target_status = "in_transit" if event_type == "out_for_delivery" else event_type
    command = _shipment_transition_command(target_status)
    allowed_from, _result_status = SHIPMENT_TRANSITIONS[command]
    if shipment.shipment_status != target_status and shipment.shipment_status not in allowed_from:
        return None, (
            f"该包裹当前为“{_shipment_status_label(shipment.shipment_status)}”，"
            f"不能直接推进为“{_shipment_status_label(target_status, event_type)}”。"
            "请按真实运输顺序更新。"
        )
    descriptions = {
        "picked_up": "包裹已由承运商揽收",
        "in_transit": "包裹正在运输途中",
        "out_for_delivery": "包裹正在派送中",
        "delivered": "包裹已签收",
        "exception": "包裹运输出现异常",
        "returned": "包裹已退回",
    }
    location = _requested_location(value)
    return PreparedOperationsAction(
        action_type="admin_shipment_progress",
        title="确认以平台管理员身份更新物流节点",
        summary=(
            "该节点会立即进入包裹不可变轨迹并同步给顾客和店铺，可能推动订单履约状态。"
            "本操作只适用于商城模拟物流，不会自动定时推进。"
        ),
        target_label=(f"{store.store_name} · {customer.username} · 包裹 {shipment.shipment_no}"),
        payload={
            "shipment_no": shipment.shipment_no,
            "order_no": order.order_no,
            "event_type": event_type,
            "description": descriptions[event_type],
            "location_text": location,
        },
        resource_versions={
            "shipment": shipment.version,
            "order": order.version,
            "store": store.version,
        },
        changes=(
            {"label": "订单", "value": order.order_no},
            {"label": "当前节点", "value": _shipment_status_label(shipment.shipment_status)},
            {"label": "更新为", "value": _shipment_status_label(target_status, event_type)},
            {"label": "位置", "value": location or "未填写"},
        ),
        tool_code="governance.trade.shipments.progress.commit",
    ), None


async def _prepare_merchant_review_reply(
    session: AsyncSession, context: TrustedOperationsContext, value: str
) -> tuple[PreparedOperationsAction | None, str | None]:
    assert context.store is not None
    rows = (
        await session.execute(
            select(Review, Product, User)
            .join(Product, Product.id == Review.product_id)
            .join(User, User.id == Review.user_id)
            .where(
                Review.store_id == context.store.id,
                Review.review_status.in_(("published", "hidden")),
                ~select(ReviewReply.id).where(ReviewReply.review_id == Review.id).exists(),
            )
            .order_by(Review.published_at.desc(), Review.id.desc())
        )
    ).all()
    review, product, customer, error = _match_merchant_review(
        [(row[0], row[1], row[2]) for row in rows], value
    )
    if review is None or product is None or customer is None:
        return None, error or "请提供评价 ID、商品名称或顾客用户名。"
    content = _review_reply_content(value)
    if content is None:
        return None, "请写明回复内容，例如“回复这条评价: 感谢您的支持，我们会继续努力”。"
    return PreparedOperationsAction(
        action_type="merchant_review_reply",
        title="确认公开回复评价",
        summary="回复发布后会展示给所有查看该商品评价的顾客，不能在确认后自动撤回。",
        target_label=f"{product.product_name} · {customer.username}",
        payload={
            "review_no": review.review_no,
            "product_name": product.product_name,
            "customer_name": customer.username,
            "content": content,
        },
        resource_versions={"review": review.version, "store": context.store.version},
        changes=(
            {"label": "顾客评分", "value": f"{review.rating} 星"},
            {"label": "顾客评价", "value": (review.content or "顾客未填写文字评价")[:120]},
            {"label": "公开回复", "value": content[:160]},
        ),
        tool_code="store_ops.review.reply.commit",
    ), None


def _match_merchant_order(
    rows: list[tuple[Order, User]], value: str
) -> tuple[Order | None, User | None, str | None]:
    exact = [(order, user) for order, user in rows if order.order_no in value]
    if len(exact) == 1:
        return exact[0][0], exact[0][1], None
    mentioned = [(order, user) for order, user in rows if _mentions_username(user.username, value)]
    candidates = exact or mentioned
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][1], None
    if len(candidates) > 1:
        return None, None, "该顾客有多笔待发货订单，请补充完整订单号。"
    if len(rows) == 1:
        return rows[0][0], rows[0][1], None
    if not rows:
        return None, None, "本店当前没有可以发货的订单。"
    return None, None, "本店有多笔待发货订单，请提供订单号或唯一的顾客用户名。"


def _match_merchant_shipment(
    rows: list[tuple[Shipment, Order]], value: str
) -> tuple[Shipment | None, Order | None, str | None]:
    matches = [
        (shipment, order)
        for shipment, order in rows
        if shipment.shipment_no in value or order.order_no in value
    ]
    if len(matches) == 1:
        return matches[0][0], matches[0][1], None
    if len(matches) > 1:
        return None, None, "该订单有多个运输中包裹，请补充完整包裹号。"
    if len(rows) == 1:
        return rows[0][0], rows[0][1], None
    if not rows:
        return None, None, "本店当前没有可手动推进的模拟物流包裹。"
    return None, None, "本店有多个运输中包裹，请提供订单号或包裹号。"


def _match_merchant_review(
    rows: list[tuple[Review, Product, User]], value: str
) -> tuple[Review | None, Product | None, User | None, str | None]:
    exact = [(review, product, user) for review, product, user in rows if review.review_no in value]
    matches = exact or [
        (review, product, user)
        for review, product, user in rows
        if _compact(product.product_name) in _compact(value)
        or _mentions_username(user.username, value)
    ]
    if len(matches) == 1:
        return matches[0][0], matches[0][1], matches[0][2], None
    if len(matches) > 1:
        return None, None, None, "匹配到多条待回复评价，请提供评价 ID 或同时说明商品和顾客。"
    if len(rows) == 1:
        return rows[0][0], rows[0][1], rows[0][2], None
    if not rows:
        return None, None, None, "本店当前没有待回复的公开评价。"
    return None, None, None, "请提供评价 ID、完整商品名称或唯一的顾客用户名。"


async def _operations_admin_access(
    session: AsyncSession,
    context: TrustedOperationsContext,
    permission_code: str,
) -> AdminAccess:
    permission_rows = await RbacRepository(session).permissions_for_user(context.user.id, utc_now())
    matching_rows = [row for row in permission_rows if row[0].permission_code == permission_code]
    auth_session = await session.scalar(
        select(AuthSession)
        .where(
            AuthSession.user_id == context.user.id,
            AuthSession.audience == "admin",
            AuthSession.client_type.in_(_operations_session_client_types(context.audience)),
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > utc_now(),
        )
        .order_by(AuthSession.authenticated_at.desc(), AuthSession.id.desc())
        .limit(1)
    )
    if not matching_rows or auth_session is None:
        identity_label = "经营身份" if context.audience == "merchant" else "管理身份"
        raise OperationsActionConflict(
            "AGENT_ACTION_AUTH_CONTEXT_UNAVAILABLE",
            f"当前{identity_label}或工具权限已经失效，请重新登录后再试。",
        )
    permission = matching_rows[0][0]
    claims = TokenClaims(
        subject=context.user.user_no,
        session_id=auth_session.session_no,
        audience="admin",
        permission_version=context.user.permission_version,
        expires_at=auth_session.expires_at,
    )
    granted_scopes = {
        (grant.scope_type, grant.scope_id) for _permission, grant, _role in matching_rows
    }
    if context.store is not None and ("platform", 0) not in granted_scopes:
        granted_scopes = {scope for scope in granted_scopes if scope == ("store", context.store.id)}
    if not granted_scopes:
        raise OperationsActionConflict(
            "AGENT_ACTION_SCOPE_CHANGED",
            "当前管理身份没有目标资源的数据范围，本次没有执行操作。",
        )
    return AdminAccess(
        context=AuthContext(user=context.user, session=auth_session, claims=claims),
        permission=permission,
        scopes=tuple(sorted(granted_scopes)),
    )


def _operations_session_client_types(audience: str) -> tuple[str, ...]:
    """Return the real browser-session kinds accepted by each operations portal."""
    return ("merchant",) if audience == "merchant" else ("admin", "admin_password")


def _service_conflict(exc: Exception, fallback: str) -> OperationsActionConflict:
    if isinstance(exc, ApplicationError):
        return OperationsActionConflict(exc.code, exc.detail)
    return OperationsActionConflict("AGENT_ACTION_SERVICE_FAILED", fallback)


def _requests_merchant_fulfillment_write(value: str) -> bool:
    return (
        _requests_shipment_create(value)
        or _requests_shipment_progress(value)
        or _requests_review_reply(value)
    )


def _requests_product_fulfillment_change(value: str) -> bool:
    compact = _compact(value)
    return bool(
        "清空购买须知" in compact
        or re.search(r"(?:设置为|改为|调整为|更新为)\d{1,3}(?:小时|天)内发货", compact)
        or re.search(
            r"(?:把|将|请|帮我|修改|设置|更新|调整).{0,160}"
            r"(?:发货地|发货时效|最早发货|最晚发货|购买须知).{0,40}"
            r"(?:改为|设为|设置为|更新为|调整为|清空|\d)",
            compact,
        )
        or re.search(
            r"(?:发货地|发货时效|最早发货|最晚发货|购买须知)"
            r".{0,20}(?:改为|设为|设置为|更新为|调整为|清空)",
            compact,
        )
    )


def _requested_product_draft(value: str) -> tuple[dict[str, object], str | None]:
    """Parse a merchant-authored product draft command without inventing fields."""

    compact = _compact(value)
    if not any(marker in compact for marker in ("创建商品草稿", "新增商品草稿")):
        return {}, None
    if any(marker in compact for marker in ("如何创建", "怎么创建", "创建流程")):
        return {}, None

    def text_field(*labels: str) -> str | None:
        label_pattern = "|".join(re.escape(label) for label in labels)
        match = re.search(
            rf"(?:{label_pattern})\s*(?:为|是|叫|[:：=])\s*[“\"']?"
            rf"([^，,；;\n]{{1,255}}?)[”\"']?(?=\s*(?:，|,|；|;|\n|$))",
            value,
        )
        return match.group(1).strip() if match else None

    product_name = text_field("商品名称", "商品名")
    sku_name = text_field("款式名称", "首个款式", "款式")
    price_match = re.search(
        r"(?:售价|价格)\s*(?:为|是|设为|[:：=])?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元?",
        value,
    )
    stock_match = re.search(
        r"库存\s*(?:为|是|设为|[:：=])?\s*(\d{1,9})\s*(?:件|个|支|盒)?",
        value,
    )
    region_match = re.search(
        r"发货地\s*(?:为|是|设为|[:：=])?\s*([^，,；;\n]{2,32})",
        value,
    )
    window_match = re.search(
        r"(\d{1,3})\s*(?:到|至|[-~])\s*(\d{1,3})\s*(小时|天)(?:内)?(?:发货)?",
        value,
    )
    missing: list[str] = []
    if not product_name:
        missing.append("商品名称")
    if not sku_name:
        missing.append("首个款式名称")
    if price_match is None:
        missing.append("售价")
    if stock_match is None:
        missing.append("库存")
    if region_match is None:
        missing.append("发货地")
    if window_match is None:
        missing.append("发货时效")
    if missing:
        return {}, (
            "还需要补充: "
            + "、".join(missing)
            + "。例如: 创建商品草稿，商品名称: 考试中性笔，款式: 黑色 0.5mm，"
            "价格: 9.90 元，库存: 50，发货地: 广东省，24 到 48 小时发货。"
        )

    assert product_name is not None
    assert sku_name is not None
    assert price_match is not None
    assert stock_match is not None
    assert region_match is not None
    assert window_match is not None
    if len(product_name) > 255 or len(sku_name) > 255:
        return {}, "商品名称和款式名称均不能超过 255 个字符。"
    try:
        price_minor = int(Decimal(price_match.group(1)) * 100)
    except InvalidOperation:
        return {}, "售价格式不正确，请按“9.90 元”填写。"
    if not 1 <= price_minor <= 999_999_999_999:
        return {}, "售价必须大于 0，并且不能超过平台金额上限。"
    stock_quantity = int(stock_match.group(1))
    if stock_quantity > 999_999_999:
        return {}, "库存数量不能超过 999999999 件。"
    region_text = region_match.group(1).strip()
    origin_region_code = _region_code(region_text)
    if origin_region_code is None:
        return {}, f"暂时无法识别发货地“{region_text}”，请填写省级名称，例如“广东省”。"
    multiplier = 24 if window_match.group(3) == "天" else 1
    dispatch_min_hours = int(window_match.group(1)) * multiplier
    dispatch_max_hours = int(window_match.group(2)) * multiplier
    if dispatch_min_hours > dispatch_max_hours:
        return {}, "最早发货时间不能晚于最晚发货时间。"
    if dispatch_max_hours > 8760:
        return {}, "发货时效不能超过 8760 小时。"
    return {
        "product_name": product_name,
        "sku_name": sku_name,
        "price_minor": price_minor,
        "stock_quantity": stock_quantity,
        "origin_region_code": origin_region_code,
        "dispatch_min_hours": dispatch_min_hours,
        "dispatch_max_hours": dispatch_max_hours,
    }, None


def _requested_product_sku_create(value: str) -> tuple[dict[str, object], str | None]:
    compact = _compact(value)
    if not any(marker in compact for marker in ("新增款式", "添加款式", "创建款式")):
        return {}, None
    if any(marker in compact for marker in ("如何新增", "怎么新增", "新增流程")):
        return {}, None
    name_match = re.search(
        r"(?:款式名称|新款式|款式)\s*(?:为|是|叫|[:：=])\s*[“\"']?"
        r"([^，,；;\n]{1,255}?)[”\"']?(?=\s*(?:，|,|；|;|\n|$))",
        value,
    )
    price_match = re.search(
        r"(?:售价|价格)\s*(?:为|是|设为|[:：=])?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元?",
        value,
    )
    stock_match = re.search(
        r"库存\s*(?:为|是|设为|[:：=])?\s*(\d{1,9})\s*(?:件|个|支|盒)?",
        value,
    )
    missing = []
    if name_match is None:
        missing.append("款式名称")
    if price_match is None:
        missing.append("售价")
    if stock_match is None:
        missing.append("库存")
    if missing:
        return {}, (
            "还需要补充: "
            + "、".join(missing)
            + "。例如: 给商品考试中性笔新增款式，款式名称: 蓝色 0.5mm，"
            "价格: 9.90 元，库存: 50。"
        )
    assert name_match is not None and price_match is not None and stock_match is not None
    sku_name = name_match.group(1).strip()
    try:
        price_minor = int(Decimal(price_match.group(1)) * 100)
    except InvalidOperation:
        return {}, "售价格式不正确，请按“9.90 元”填写。"
    stock_quantity = int(stock_match.group(1))
    if not 1 <= len(sku_name) <= 255:
        return {}, "款式名称需为 1 至 255 个字符。"
    if not 1 <= price_minor <= 999_999_999_999:
        return {}, "售价必须大于 0，并且不能超过平台金额上限。"
    if stock_quantity > 999_999_999:
        return {}, "库存数量不能超过 999999999 件。"
    return {
        "sku_name": sku_name,
        "price_minor": price_minor,
        "stock_quantity": stock_quantity,
    }, None


def _requested_product_sku_update(value: str) -> tuple[dict[str, object], str | None]:
    compact = _compact(value)
    changes_name = any(marker in compact for marker in ("款式名称改", "款式名称设"))
    changes_price = any(
        marker in compact for marker in ("改价", "价格改", "价格设", "售价改", "售价设")
    )
    changes_stock = any(
        marker in compact for marker in ("库存改", "库存设", "库存调整", "库存补到")
    )
    if not changes_name and not (changes_price and changes_stock):
        return {}, None
    if any(marker in compact for marker in ("如何修改款式", "怎么修改款式", "修改款式流程")):
        return {}, None
    fields: dict[str, object] = {}
    if changes_name:
        name_match = re.search(
            r"(?:款式名称|规格名称)\s*(?:改为|改成|设为|设置为|更名为)\s*"
            r"[“\"']?(.+?)[”\"']?"
            r"(?=(?:[，,；;。]\s*(?:并且|并)?\s*(?:把|将)?\s*"
            r"(?:价格|售价|库存))|$)",
            value,
            re.S,
        )
        if name_match is None:
            return {}, "请明确新的款式名称，例如“把蓝色款式名称改为深海蓝”。"
        sku_name = " ".join(name_match.group(1).strip().strip("“”\"'").split())
        if not 1 <= len(sku_name) <= 255:
            return {}, "款式名称需为 1 至 255 个字符。"
        fields["sku_name"] = sku_name
    if changes_price:
        price_minor = _target_price_minor(value)
        if price_minor is None:
            return {}, "请明确目标价格，例如“价格改为 9.90 元”。"
        fields["price_minor"] = price_minor
    if changes_stock:
        stock_quantity = _target_integer(value, "库存")
        if stock_quantity is None:
            return {}, "请明确目标库存，例如“库存设为 50 件”。"
        if stock_quantity > 999_999_999:
            return {}, "库存数量不能超过 999999999 件。"
        fields["stock_quantity"] = stock_quantity
    return fields, None


def _requests_product_sku_disable(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何", "怎么", "流程", "能否")):
        return False
    return "款式" in compact and any(marker in compact for marker in ("删除", "移除", "停用"))


def _normalized_faq_question(value: str) -> str:
    return " ".join(value.strip().strip("“”\"'").split()).rstrip("？?")


def _requests_product_faq_write(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何", "怎么", "流程", "查看", "查询", "列出")):
        return False
    return "常见问题" in compact and any(
        marker in compact for marker in ("新增", "添加", "创建", "修改", "更新", "删除", "移除")
    )


def _requested_product_faq_change(
    value: str,
) -> tuple[dict[str, object] | None, str | None]:
    if not _requests_product_faq_write(value):
        return None, None
    compact = _compact(value)
    deleting = "常见问题" in compact and any(marker in compact for marker in ("删除", "移除"))
    question_match = re.search(
        r"(?:问题|问)\s*(?:为|是|[:：=])\s*[“\"']?"
        r"([^，,;；\n]{1,1000}?)[”\"']?(?=\s*(?:，|,|；|;|\n|$))",
        value,
    )
    if question_match is None:
        quoted_match = re.search(r"常见问题\s*[“\"']([^”\"']{1,1000})[”\"']", value)
        question_match = quoted_match
    if question_match is None:
        return None, (
            "请明确常见问题内容。例如：给商品考试中性笔新增常见问题，"
            "问题: 是否包邮，回答: 本商品包邮。"
        )
    question = _normalized_faq_question(question_match.group(1))
    if not question:
        return None, "常见问题不能为空。"
    if deleting:
        return {"mode": "delete", "question": question}, None
    answer_match = re.search(
        r"(?:回答|答案|答)\s*(?:改为|改成|设为|是|[:：=])\s*[“\"']?(.+?)[”\"']?\s*$",
        value,
        re.S,
    )
    if answer_match is None:
        return None, "还需要提供回答，例如“回答: 本商品包邮”。"
    answer = " ".join(answer_match.group(1).strip().strip("“”\"'").split())
    if not answer:
        return None, "常见问题的回答不能为空。"
    if len(answer.encode("utf-8")) > 100_000:
        return None, "回答内容不能超过 100KB。"
    return {"mode": "upsert", "question": question, "answer": answer}, None


def _normalized_detail_section_title(value: str) -> str:
    return " ".join(value.strip().strip("“”\"'").split())


def _requests_product_detail_section_write(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何", "怎么", "流程", "查看", "查询", "列出")):
        return False
    has_detail_target = any(
        marker in compact
        for marker in (
            "商品详情段落",
            "商品详情章节",
            "详情段落",
            "详情章节",
            "详情文字块",
        )
    )
    return has_detail_target and any(
        marker in compact for marker in ("新增", "添加", "创建", "修改", "更新", "删除", "移除")
    )


def _requested_product_detail_section_change(
    value: str,
) -> tuple[dict[str, str] | None, str | None]:
    if not _requests_product_detail_section_write(value):
        return None, None
    compact = _compact(value)
    deleting = any(marker in compact for marker in ("删除", "移除"))
    title_match = re.search(
        r"(?:段落标题|章节标题|标题)\s*(?:为|是|[:：=])\s*[“\"']?"
        r"([^，,;；\n]{1,120}?)[”\"']?(?=\s*(?:，|,|；|;|\n|$))",
        value,
    )
    if title_match is None:
        return None, (
            "请明确详情段落标题。例如：给商品考试中性笔新增详情段落，"
            "标题: 适用场景，内容: 适合日常书写与考试备用。"
        )
    title = _normalized_detail_section_title(title_match.group(1))
    if not 1 <= len(title) <= 120:
        return None, "详情段落标题需为 1 至 120 个字符。"
    if deleting:
        return {"mode": "delete", "title": title}, None
    content_match = re.search(
        r"(?:段落内容|正文|内容)\s*(?:改为|改成|设为|是|[:：=])\s*"
        r"[“\"']?(.+?)[”\"']?\s*$",
        value,
        re.S,
    )
    if content_match is None:
        return None, "还需要提供段落内容，例如“内容: 适合日常书写与考试备用”。"
    content = " ".join(content_match.group(1).strip().strip("“”\"'").split())
    if not content:
        return None, "详情段落内容不能为空。"
    if len(content.encode("utf-8")) > 50_000:
        return None, "单个详情段落不能超过 50KB。"
    return {"mode": "upsert", "title": title, "content": content}, None


def _detail_section_heading_indexes(blocks: list[dict[str, object]], title: str) -> list[int]:
    normalized = _normalized_detail_section_title(title).casefold()
    return [
        index
        for index, block in enumerate(blocks)
        if block.get("type") == "heading"
        and _normalized_detail_section_title(str(block.get("text") or "")).casefold() == normalized
    ]


def _replace_detail_section_blocks(
    blocks: list[dict[str, object]],
    *,
    title: str,
    content: str | None,
    delete: bool,
) -> list[dict[str, object]]:
    result = [dict(block) for block in blocks]
    matches = _detail_section_heading_indexes(result, title)
    if len(matches) > 1:
        raise ValueError("匹配到多个同名详情段落。")
    if not matches:
        if delete:
            raise LookupError("没有找到指定的详情段落。")
        assert content is not None
        return [
            *result,
            {"type": "heading", "level": 2, "text": title},
            {"type": "paragraph", "text": content},
        ]
    heading_index = matches[0]
    replace_end = heading_index + 1
    while replace_end < len(result) and result[replace_end].get("type") in {
        "paragraph",
        "bullet_list",
    }:
        replace_end += 1
    if delete:
        return result[:heading_index] + result[replace_end:]
    assert content is not None
    return [
        *result[: heading_index + 1],
        {"type": "paragraph", "text": content},
        *result[replace_end:],
    ]


def _sku_style_signature(sku_name: str) -> bytes:
    normalized_specs = [{"name": "款式", "value": sku_name.strip().casefold()}]
    return hashlib.sha256(
        json.dumps(normalized_specs, ensure_ascii=False, separators=(",", ":")).encode()
    ).digest()


def _requests_admin_knowledge_write(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何", "怎么", "流程", "查看", "查询", "列出")):
        return False
    return any(
        marker in compact for marker in ("发布知识文档", "重建知识索引", "重建索引", "撤回知识文档")
    )


def _requests_admin_ai_publication(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何", "怎么", "流程", "查看", "查询", "列出")):
        return False
    if "发布" not in compact and "发布审批" not in compact:
        return False
    if "知识文档" in compact or "知识索引" in compact:
        return False
    return any(marker in compact for marker in ("agent", "skill", "tool", "工具版本"))


def _requests_admin_agent_prompt_draft(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何", "怎么", "流程", "查看", "查询", "列出")):
        return False
    has_agent = "agent" in compact or "智能体" in compact
    has_prompt = any(marker in compact for marker in ("prompt", "系统提示词", "提示词"))
    has_write = any(
        marker in compact
        for marker in (
            "修改",
            "更新",
            "改为",
            "改成",
            "替换为",
            "设置为",
            "设为",
            "创建草稿",
            "新建草稿",
        )
    )
    return has_agent and has_prompt and has_write


def _requested_agent_prompt_body(value: str) -> str | None:
    patterns = (
        r"(?:系统\s*prompt|system\s*prompt|prompt|系统提示词|提示词)(?:正文)?\s*"
        r"(?:改为|改成|更新为|替换为|设置为|设为)?\s*[:：]\s*(.+)\Z",
        r"(?:新(?:的)?正文|正文|新内容)\s*[:：]\s*(.+)\Z",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE | re.DOTALL)
        if match is None:
            continue
        prompt = match.group(1).strip()
        if len(prompt) >= 20:
            return prompt[:50000]
    return None


def _prompt_preview(value: str, limit: int = 180) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


async def _prepare_admin_agent_prompt_draft(
    session: AsyncSession, value: str
) -> tuple[PreparedOperationsAction | None, str | None]:
    prompt = _requested_agent_prompt_body(value)
    if prompt is None:
        return None, (
            "请提供不少于 20 个字符的新 Prompt 正文，例如："
            "“把 Agent admin_copilot 的系统提示词改为：你是……”。"
        )
    definitions = list((await session.scalars(select(AgentDefinition))).all())
    folded = value.casefold()
    matches = [
        item
        for item in definitions
        if item.agent_no.casefold() in folded
        or item.agent_code.casefold() in folded
        or item.display_name.casefold() in folded
    ]
    if len(matches) != 1:
        return None, "请提供唯一的 Agent 编号 (agt_…)、完整 Agent Code 或完整名称。"
    definition = matches[0]
    base_version = await session.scalar(
        select(AgentVersion)
        .where(AgentVersion.agent_id == definition.id)
        .order_by(AgentVersion.version_no.desc())
        .limit(1)
    )
    if base_version is None:
        return None, "目标 Agent 尚无可继承的版本，请先在 AI 治理页面创建初始版本。"
    if prompt == base_version.system_prompt:
        return None, f"{definition.display_name} 最新版本的 Prompt 已经是这段内容，无需新建草稿。"
    policy_config = dict(base_version.policy_config or {})
    policy_config.pop("evaluation_report", None)
    next_version_no = int(base_version.version_no) + 1
    return PreparedOperationsAction(
        action_type="admin_ai_agent_prompt_draft_create",
        title="确认创建 Agent Prompt 草稿",
        summary=(
            "确认后会基于最新版本创建不可变的新草稿，不会覆盖或切换线上版本。"
            "新草稿必须重新评估并经过独立发布审批后才能生效。"
        ),
        target_label=f"{definition.display_name} · {definition.agent_code} · v{next_version_no}",
        payload={
            "agent_no": definition.agent_no,
            "agent_code": definition.agent_code,
            "base_version_no": int(base_version.version_no),
            "next_version_no": next_version_no,
            "system_prompt": prompt,
            "model_profile": base_version.model_profile,
            "tool_allowlist": list(base_version.tool_allowlist or []),
            "policy_config": policy_config,
        },
        resource_versions={
            "definition": definition.version,
            "base_version": base_version.version,
            "base_version_no": int(base_version.version_no),
            "next_version_no": next_version_no,
        },
        changes=(
            {"label": "基准版本", "value": f"v{base_version.version_no}"},
            {"label": "新草稿版本", "value": f"v{next_version_no}"},
            {"label": "原 Prompt 摘要", "value": _prompt_preview(base_version.system_prompt)},
            {"label": "新 Prompt 摘要", "value": _prompt_preview(prompt)},
            {"label": "上线影响", "value": "无；需重新评估和发布审批"},
        ),
        tool_code="governance.ai.agents.prompt_draft.create.commit",
    ), None


async def _prepare_admin_ai_publication(
    session: AsyncSession, value: str
) -> tuple[PreparedOperationsAction | None, str | None]:
    compact = _compact(value)
    version_match = re.search(r"(?:版本\s*|\bv\s*)(\d{1,9})", value, flags=re.IGNORECASE)
    if version_match is None:
        return None, "请明确提供要发起发布审批的版本号，例如 v2 或版本 2。"
    version_no = int(version_match.group(1))
    definition: Any
    version: Any
    if "agent" in compact:
        agent_definitions = list((await session.scalars(select(AgentDefinition))).all())
        agent_matches = [
            item
            for item in agent_definitions
            if item.agent_no.casefold() in value.casefold()
            or item.agent_code.casefold() in value.casefold()
        ]
        if len(agent_matches) != 1:
            return None, "请提供唯一的 Agent 编号 (agt_…) 或完整 Agent Code。"
        definition = agent_matches[0]
        version = await session.scalar(
            select(AgentVersion).where(
                AgentVersion.agent_id == definition.id,
                AgentVersion.version_no == version_no,
            )
        )
        if version is None:
            return None, f"没有找到 {definition.agent_code} 的 v{version_no}。"
        evaluation = (
            version.policy_config.get("evaluation_report")
            if isinstance(version.policy_config, dict)
            else None
        )
        entity_kind = "Agent"
        entity_code = definition.agent_code
        entity_no = definition.agent_no
        version_status = version.version_status
        evaluation_passed = isinstance(evaluation, dict) and bool(evaluation.get("passed"))
        definition_version = definition.version
        version_resource = version.version
        action_type = "admin_ai_agent_publish_request"
        tool_code = "governance.ai.agents.publish_request.commit"
        permission_code = "ai_agents:publish"
    elif "skill" in compact:
        skill_definitions = list((await session.scalars(select(SkillDefinition))).all())
        skill_matches = [
            item
            for item in skill_definitions
            if item.skill_no.casefold() in value.casefold()
            or item.skill_code.casefold() in value.casefold()
        ]
        if len(skill_matches) != 1:
            return None, "请提供唯一的 Skill 编号 (skl_…) 或完整 Skill Code。"
        definition = skill_matches[0]
        version = await session.scalar(
            select(SkillVersion).where(
                SkillVersion.skill_id == definition.id,
                SkillVersion.version_no == version_no,
            )
        )
        if version is None:
            return None, f"没有找到 {definition.skill_code} 的 v{version_no}。"
        entity_kind = "Skill"
        entity_code = definition.skill_code
        entity_no = definition.skill_no
        version_status = version.version_status
        evaluation_passed = bool(version.evaluation_report.get("passed"))
        definition_version = definition.version
        version_resource = version.version
        action_type = "admin_ai_skill_publish_request"
        tool_code = "governance.ai.skills.publish_request.commit"
        permission_code = "ai_skills:publish"
    else:
        tool_definitions = list((await session.scalars(select(ToolDefinition))).all())
        tool_matches = [
            item for item in tool_definitions if item.tool_code.casefold() in value.casefold()
        ]
        if len(tool_matches) != 1:
            return None, "请提供唯一且完整的 Tool Code。"
        definition = tool_matches[0]
        version = await session.scalar(
            select(ToolVersion).where(
                ToolVersion.tool_id == definition.id,
                ToolVersion.version_no == version_no,
            )
        )
        if version is None:
            return None, f"没有找到 {definition.tool_code} 的 v{version_no}。"
        entity_kind = "Tool"
        entity_code = definition.tool_code
        entity_no = definition.tool_code
        version_status = version.version_status
        evaluation_passed = bool(version.evaluation_report.get("passed"))
        definition_version = definition.version
        version_resource = version.version
        action_type = "admin_ai_tool_publish_request"
        tool_code = "governance.ai.tools.publish_request.commit"
        permission_code = "ai_tools:publish"
    if version_status != "draft":
        return None, f"{entity_kind} {entity_code} 的 v{version_no} 当前不是可发布草稿。"
    if not evaluation_passed:
        return None, f"{entity_kind} {entity_code} 的 v{version_no} 尚未通过质量与安全评估。"
    return PreparedOperationsAction(
        action_type=action_type,
        title=f"确认发起 {entity_kind} 版本发布审批",
        summary=(
            "本次确认只创建双人复核审批，不会直接发布。之后仍需两名不同管理员批准，"
            "发起人不能自批; 最终执行器会再次检查版本、评估和依赖。"
        ),
        target_label=f"{entity_code} · v{version_no}",
        payload={
            "entity_kind": entity_kind.casefold(),
            "entity_no": entity_no,
            "entity_code": entity_code,
            "version_no": version_no,
            "permission_code": permission_code,
        },
        resource_versions={
            "definition": definition_version,
            "publish_version": version_resource,
        },
        changes=(
            {"label": "当前版本状态", "value": "草稿"},
            {"label": "评估结果", "value": "已通过"},
            {"label": "本次确认后", "value": "创建双人发布审批"},
            {"label": "不会立即发生", "value": "不会直接切换线上版本"},
        ),
        tool_code=tool_code,
    ), None


def _requests_dead_letter_replay(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何", "怎么", "流程", "查看", "查询")):
        return False
    return any(marker in compact for marker in ("重放死信", "死信重放", "重新投递死信"))


def _dead_letter_replay_reason(value: str) -> str:
    match = re.search(r"(?:原因|依据)\s*[:\uff1a]\s*(.{5,500})$", value, re.S)
    if match:
        return match.group(1).strip()[:500]
    return "管理员通过 AI 管家请求按原始不可变事件进行受控重放"


def _requests_merchant_policy_write(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何", "怎么", "流程", "查看", "查询")):
        return False
    has_policy_noun = any(
        marker in compact
        for marker in ("店铺政策", "售后政策", "退换政策", "发货政策", "配送政策", "客服政策")
    )
    has_command = any(
        marker in compact for marker in ("新建", "创建", "修改", "更新", "发布", "撤回")
    )
    return has_policy_noun and has_command


def _merchant_policy_command(value: str) -> str:
    compact = _compact(value)
    if "撤回" in compact:
        return "withdraw"
    if "发布" in compact:
        return "publish"
    if any(marker in compact for marker in ("修改", "更新")):
        return "update"
    return "create"


def _merchant_policy_type(value: str) -> str:
    compact = _compact(value)
    if any(marker in compact for marker in ("售后", "退换", "退款")):
        return "after_sale"
    if any(marker in compact for marker in ("发货", "配送", "包邮", "运费")):
        return "shipping"
    if any(marker in compact for marker in ("客服", "咨询", "服务响应")):
        return "customer_service"
    return "store_service"


def _merchant_policy_fields(value: str) -> tuple[str | None, str | None]:
    title_match = re.search(
        r"标题\s*[:\uff1a]\s*([^,\uff0c;\uff1b\n]{1,128}?)(?=\s*[,\uff0c;\uff1b]\s*内容\s*[:\uff1a]|$)",
        value,
        re.S,
    )
    content_match = re.search(r"内容\s*[:\uff1a]\s*(.{1,100000})$", value, re.S)
    title = title_match.group(1).strip().strip("“”\"'") if title_match else None
    content = content_match.group(1).strip().strip("“”\"'") if content_match else None
    return title or None, content or None


def _match_store_policy(
    policies: list[StoreServicePolicy], value: str, policy_type: str, command: str
) -> tuple[StoreServicePolicy | None, str | None]:
    exact = [item for item in policies if item.policy_no.casefold() in value.casefold()]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, "匹配到多个政策编号，请只指定一个。"
    required_status = "published" if command == "withdraw" else "draft"
    candidates = [
        item
        for item in policies
        if item.policy_type == policy_type and item.policy_status == required_status
    ]
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        return None, (
            f"当前没有可{_policy_command_label(command)}的{_policy_type_label(policy_type)}，"
            "请先新建草稿或提供完整政策编号。"
        )
    return None, "匹配到多个候选政策，请提供完整政策编号。"


def _policy_type_label(value: str) -> str:
    return {
        "after_sale": "售后与退换政策",
        "shipping": "发货与配送政策",
        "customer_service": "客服服务政策",
        "store_service": "店铺服务政策",
    }.get(value, value)


def _policy_status_label(value: str) -> str:
    return {
        "draft": "草稿",
        "published": "已发布",
        "withdrawn": "已撤回",
        "expired": "已过期",
    }.get(value, value)


def _knowledge_document_status_label(value: str) -> str:
    return {
        "draft": "草稿",
        "published": "已发布",
        "withdrawn": "已撤回",
    }.get(value, value)


def _policy_command_label(value: str) -> str:
    return {"create": "新建", "update": "修改", "publish": "发布", "withdraw": "撤回"}.get(
        value, value
    )


def _policy_command_result_label(value: str) -> str:
    return {
        "create": "创建为草稿",
        "update": "更新",
        "publish": "发布",
        "withdraw": "撤回",
    }.get(value, "处理")


_PROVINCE_REGION_CODES: tuple[tuple[str, str], ...] = (
    ("北京市", "110000"),
    ("天津市", "120000"),
    ("河北省", "130000"),
    ("山西省", "140000"),
    ("内蒙古自治区", "150000"),
    ("辽宁省", "210000"),
    ("吉林省", "220000"),
    ("黑龙江省", "230000"),
    ("上海市", "310000"),
    ("江苏省", "320000"),
    ("浙江省", "330000"),
    ("安徽省", "340000"),
    ("福建省", "350000"),
    ("江西省", "360000"),
    ("山东省", "370000"),
    ("河南省", "410000"),
    ("湖北省", "420000"),
    ("湖南省", "430000"),
    ("广东省", "440000"),
    ("广西壮族自治区", "450000"),
    ("海南省", "460000"),
    ("重庆市", "500000"),
    ("四川省", "510000"),
    ("贵州省", "520000"),
    ("云南省", "530000"),
    ("西藏自治区", "540000"),
    ("陕西省", "610000"),
    ("甘肃省", "620000"),
    ("青海省", "630000"),
    ("宁夏回族自治区", "640000"),
    ("新疆维吾尔自治区", "650000"),
    ("台湾省", "710000"),
    ("香港特别行政区", "810000"),
    ("澳门特别行政区", "820000"),
)


def _requested_product_fulfillment_changes(
    value: str,
) -> tuple[dict[str, object], str | None]:
    if not _requests_product_fulfillment_change(value):
        return {}, None
    changes: dict[str, object] = {}
    region_match = re.search(
        r"发货地.{0,12}?(?:改为|设为|设置为|更新为|调整为)\s*"
        r"([^，。,;]{2,32})",
        value,
    )
    if region_match:
        region_text = region_match.group(1).strip()
        region_code = _region_code(region_text)
        if region_code is None:
            return {}, (
                f"暂时无法确认“{region_text}”对应的发货地区。"
                "请填写省级名称，例如“广东省”，或 6 位行政区代码。具体城市可在商品编辑页选择。"
            )
        changes["origin_region_code"] = region_code

    window_match = re.search(
        r"(?:发货时效.{0,16}?)?(\d{1,3})\s*(?:到|至|[-~])\s*(\d{1,3})\s*"
        r"(小时|天)(?:内)?(?:发货)?",
        value,
    )
    if window_match:
        multiplier = 24 if window_match.group(3) == "天" else 1
        changes["dispatch_min_hours"] = int(window_match.group(1)) * multiplier
        changes["dispatch_max_hours"] = int(window_match.group(2)) * multiplier
    else:
        earliest = re.search(
            r"最早发货.{0,12}?(?:改为|设为|设置为|调整为)?\s*(\d{1,3})\s*(小时|天)",
            value,
        )
        latest = re.search(
            r"最晚发货.{0,12}?(?:改为|设为|设置为|调整为)?\s*(\d{1,3})\s*(小时|天)",
            value,
        )
        within = re.search(r"(\d{1,3})\s*(小时|天)内发货", value)
        if earliest:
            changes["dispatch_min_hours"] = int(earliest.group(1)) * (
                24 if earliest.group(2) == "天" else 1
            )
        if latest:
            changes["dispatch_max_hours"] = int(latest.group(1)) * (
                24 if latest.group(2) == "天" else 1
            )
        elif within:
            changes["dispatch_max_hours"] = int(within.group(1)) * (
                24 if within.group(2) == "天" else 1
            )

    if "清空购买须知" in _compact(value):
        changes["purchase_notice"] = None
    else:
        notice_match = re.search(
            r"购买须知.{0,12}?(?:改为|设为|设置为|更新为|调整为)\s*(.{1,3000})$",
            value,
            re.S,
        )
        if notice_match:
            changes["purchase_notice"] = notice_match.group(1).strip().strip("“”\"'")
    if not changes:
        return {}, "请明确发货地、发货时间或购买须知要修改成什么内容。"
    return changes, None


def _region_code(value: str) -> str | None:
    normalized = re.sub(r"\s+", "", value)
    raw_code = re.fullmatch(r"\d{6}", normalized)
    if raw_code:
        return raw_code.group(0)
    for name, code in sorted(_PROVINCE_REGION_CODES, key=lambda item: len(item[0]), reverse=True):
        short_name = re.sub(r"(?:壮族|回族|维吾尔)?自治区$|特别行政区$|省$|市$", "", name)
        if name in normalized or short_name == normalized:
            return code
    return None


def _region_label(value: str) -> str:
    for name, code in _PROVINCE_REGION_CODES:
        if code == value:
            return name
    return value


def _dispatch_window_label(minimum: int, maximum: int) -> str:
    if minimum == maximum:
        return f"预计 {maximum} 小时内发货"
    return f"预计 {minimum}-{maximum} 小时内发货"


def _requests_shipment_create(value: str) -> bool:
    compact = _compact(value)
    if _requests_product_fulfillment_change(value) or any(
        marker in compact for marker in ("怎么发货", "如何发货", "发货流程")
    ):
        return False
    return bool(
        re.search(r"(?:把|将|帮我|请|替我|给).{0,160}(?:订单)?.{0,80}(?:发货|安排发货)", compact)
        or re.search(r"(?:订单|ord_)[^，。\s]{0,80}(?:发货|安排发货)", compact)
    )


def _requests_shipment_progress(value: str) -> bool:
    compact = _compact(value)
    return bool(
        any(
            marker in compact
            for marker in ("更新物流", "物流更新", "推进物流", "更新包裹", "物流改为")
        )
        and _requested_shipment_event(value) is not None
    )


def _requests_admin_shipment_progress(value: str) -> bool:
    return _requests_shipment_progress(value)


def _requests_admin_order_cancel(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何取消", "怎么取消", "能否取消", "可以取消吗")):
        return False
    return (
        "取消订单" in compact
        or "关闭未付款订单" in compact
        or ("取消" in compact and bool(re.search(r"ord_[0-9a-z]+", value, re.I)))
    )


def _requests_review_reply(value: str) -> bool:
    compact = _compact(value)
    if "待回复评价" in compact and not any(
        marker in compact for marker in ("帮我回复", "请回复", "替我回复", "回复内容", "回复为")
    ):
        return False
    return bool(
        re.search(r"(?:帮我|请|替我).{0,30}回复.{0,100}(?:评价|评论)", compact)
        or re.search(r"回复(?:评价|评论).{0,80}[\uff1a:]", compact)
        or any(marker in compact for marker in ("回复内容", "回复为"))
    )


def _requests_support_action(value: str) -> bool:
    return (
        _requests_support_claim(value)
        or _requests_support_resolve(value)
        or _requests_support_reply(value)
    )


def _requests_support_claim(value: str) -> bool:
    compact = _compact(value)
    return any(marker in compact for marker in ("接入人工工单", "领取人工工单", "接入人工服务"))


def _requests_support_resolve(value: str) -> bool:
    compact = _compact(value)
    return any(
        marker in compact
        for marker in ("结束人工服务", "解决人工工单", "结束客服服务", "完成客服工单")
    )


def _requests_support_reply(value: str) -> bool:
    compact = _compact(value)
    return any(
        marker in compact
        for marker in (
            "回复顾客",
            "回复用户",
            "回复商家",
            "给顾客发消息",
            "给用户发消息",
            "给商家发消息",
        )
    ) or bool(
        re.search(
            r"(?:给|向)(?:顾客|用户|商家).{0,100}(?:发送|发)(?:.{0,30})?(?:商品|订单)卡片",
            compact,
        )
    )


def _support_attachment_kind(value: str) -> Literal["product", "order"] | None:
    compact = _compact(value)
    product = any(marker in compact for marker in ("商品卡片", "产品卡片"))
    order = any(marker in compact for marker in ("订单卡片", "购买记录卡片"))
    if product == order:
        return None
    return "product" if product else "order"


def _match_support_order(orders: list[Order], value: str) -> Order | None:
    matches = [order for order in orders if order.order_no.casefold() in value.casefold()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None
    compact = _compact(value)
    if len(orders) == 1 or any(marker in compact for marker in ("最近订单", "最新订单")):
        return orders[0] if orders else None
    return None


def _match_support_product(products: list[Product], value: str) -> Product | None:
    compact = _compact(value)
    matches = [
        product
        for product in products
        if product.product_no.casefold() in value.casefold()
        or _compact(product.product_name) in compact
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None
    if len(products) == 1 or any(marker in compact for marker in ("最近商品", "最新商品")):
        return products[0] if products else None
    return None


def _support_reply_content(value: str) -> str | None:
    match = re.search(r"(?:回复内容|消息内容|发送内容)\s*[\uff1a:]\s*(.{1,4000})$", value, re.S)
    if match is None:
        return None
    content = match.group(1).strip().strip("“”\"'")
    return content[:4000] if content else None


def _requested_shipment_event(value: str) -> str | None:
    compact = _compact(value)
    for code, markers in (
        ("out_for_delivery", ("派送中", "正在派送", "开始派送")),
        ("picked_up", ("已揽收", "揽收")),
        ("in_transit", ("运输中", "开始运输", "在途")),
        ("delivered", ("已签收", "签收")),
        ("exception", ("物流异常", "运输异常", "异常")),
        ("returned", ("已退回", "退回")),
    ):
        if any(marker in compact for marker in markers):
            return code
    return None


def _shipment_transition_command(status: str) -> str:
    return {
        "picked_up": "RecordPickup",
        "in_transit": "RecordInTransit",
        "delivered": "RecordDelivery",
        "exception": "RecordException",
        "returned": "RecordReturn",
    }[status]


def _requested_location(value: str) -> str | None:
    match = re.search(
        r"(?:位置|地点|当前在|到达)\s*[\uff1a:]?\s*([^\uff0c\u3002\uff1b;]{2,80})",
        value,
    )
    return match.group(1).strip() if match else None


def _review_reply_content(value: str) -> str | None:
    patterns = (
        r"(?:回复内容|回复为|回复评价)\s*[\uff1a:]\s*(.{2,500})$",
        r"(?:评价|评论).{0,80}?(?:回复|答复)\s*[\uff1a:]\s*(.{2,500})$",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.S)
        if match:
            content = match.group(1).strip().strip("“”\"'")
            return content[:500] if len(content) >= 2 else None
    return None


def _uses_recent_reference(value: str) -> bool:
    compact = _compact(value)
    return any(marker in compact for marker in ("刚才", "上一个", "这个包裹", "该包裹", "它"))


def _shipment_status_label(value: str, provider_status: str | None = None) -> str:
    if provider_status == "out_for_delivery":
        return "派送中"
    return {
        "created": "待揽收",
        "picked_up": "已揽收",
        "in_transit": "运输中",
        "delivered": "已签收",
        "exception": "物流异常",
        "returned": "已退回",
        "closed": "已关闭",
        "voided": "已作废",
    }.get(value, value)


def _order_status_label(value: str) -> str:
    return {
        "pending_payment": "待付款",
        "paid": "已付款",
        "pending_shipment": "待发货",
        "shipped": "运输中",
        "completed": "已完成",
        "cancelled": "已取消",
        "closed": "已关闭",
    }.get(value, value)


def _target_integer(value: str, noun: str) -> int | None:
    patterns = (
        rf"{noun}.{{0,12}}?(?:改到|设为|设置为|调整到|补到|改为)\s*(\d{{1,9}})",
        r"(?:改到|设为|设置为|调整到|补到|改为)\s*(\d{1,9})\s*(?:件|个|支|盒)?(?:库存)?",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return int(match.group(1))
    return None


def _target_price_minor(value: str) -> int | None:
    for pattern in (
        r"(?:改价|价格|售价).{0,12}?(?:改到|改为|设为|设置为|调整到|到|为)\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元?",
        r"(?:改到|改为|设为|设置为|调整到)\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元",
    ):
        match = re.search(pattern, value)
        if not match:
            continue
        try:
            amount = Decimal(match.group(1))
        except InvalidOperation:
            return None
        minor = int(amount * 100)
        return minor if 1 <= minor <= 999_999_999_999 else None
    return None


def _require_version(actual: int, expected: object) -> None:
    if not isinstance(expected, int) or actual != expected:
        raise OperationsActionConflict(
            "AGENT_ACTION_RESOURCE_CHANGED", "目标数据在确认前已经变化，请重新发起操作。"
        )


def _admin_audit(
    session: AsyncSession,
    context: TrustedOperationsContext,
    action: str,
    target_type: str,
    target_no: str,
    before: dict[str, object],
    after: dict[str, object],
) -> None:
    if context.audience != "admin":
        return
    permission_code = _audit_permission_code(action)
    session.add(
        AdminOperationLog(
            operation_no=new_prefixed_ulid("aol_"),
            operator_user_id=context.user.id,
            scope_type="platform",
            scope_id=0,
            permission_code=permission_code,
            action=action,
            target_type=target_type,
            target_no=target_no,
            before_snapshot=before,
            after_snapshot=after,
            result_status="succeeded",
            reason="由 AI 管家确认卡执行",
            request_id=context.run.trace_id,
            trace_id=context.run.trace_id,
            ip_hash=None,
        )
    )


def _audit_permission_code(action: str) -> str:
    return {
        "admin_user_status": "users:manage",
        "admin_user_force_logout": "users:sessions_revoke",
        "admin_user_create": "users:manage",
        "admin_user_password_reset_requirement": "users:force_password_reset",
        "admin_user_wallet_adjust": "users:manage",
        "admin_user_profile": "users:manage",
        "admin_user_avatar_update": "users:manage",
        "admin_user_delete": "users:manage",
        "admin_user_address_create": "users:manage",
        "admin_user_address_update": "users:manage",
        "admin_user_address_delete": "users:manage",
        "admin_user_address_set_default": "users:manage",
        "admin_user_cart_item_update": "users:manage",
        "admin_user_cart_item_delete": "users:manage",
        "admin_user_cart_clear": "users:manage",
        "admin_user_favorite_product_remove": "users:manage",
        "admin_user_favorite_store_remove": "users:manage",
        "admin_store_status": "stores:manage",
        "admin_store_create": "stores:manage",
        "admin_store_profile": "stores:manage",
        "admin_store_logo_update": "stores:manage",
        "admin_store_merchant_email_update": "stores:manage",
        "admin_store_delete": "stores:manage",
        "admin_product_status": "products:publish",
        "admin_product_delete": "products:update",
        "admin_product_profile": "products:update",
        "admin_product_image_description": "products:update",
        "admin_product_faq_upsert": "products:update",
        "admin_product_faq_delete": "products:update",
        "admin_product_sku_create": "products:update",
        "admin_product_sku_update": "products:update",
        "admin_product_sku_disable": "products:update",
        "admin_product_sku_image_replace": "products:update",
        "admin_product_detail_section_upsert": "products:update",
        "admin_product_detail_section_delete": "products:update",
        "admin_product_review": "products:review",
        "admin_order_cancel": "orders:cancel",
        "admin_shipment_progress": "shipments:create",
        "admin_refund_decision": "refunds:review",
        "admin_refund_more_info": "refunds:review",
        "merchant_support_claim": "support:claim",
        "merchant_support_reply": "support:reply",
        "merchant_support_resolve": "support:resolve",
        "admin_support_claim": "support:claim",
        "admin_support_reply": "support:reply",
        "admin_support_resolve": "support:resolve",
        "admin_dead_letter_replay_request": "events:operate",
        "admin_ai_agent_prompt_draft_create": "ai_agents:manage",
        "admin_ai_evaluation_run": "ai_evaluations:run",
    }.get(action, "ai_tools:manage")


def _outbox(
    session: AsyncSession,
    context: TrustedOperationsContext,
    event_type: str,
    aggregate_type: str,
    aggregate_no: str,
    aggregate_version: int,
    payload: dict[str, object],
) -> None:
    session.add(
        OutboxEvent(
            event_no=new_prefixed_ulid("evt_"),
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_no=aggregate_no,
            aggregate_version=aggregate_version,
            payload=payload,
            event_status="pending",
            available_at=utc_now(),
            attempt_count=0,
            trace_id=context.run.trace_id,
        )
    )


def _requested_image_description(
    value: str,
) -> tuple[tuple[int, str] | None, str | None]:
    compact = _compact(value)
    if not any(marker in compact for marker in ("详情图片说明", "图片说明")):
        return None, None
    if any(marker in compact for marker in ("怎么修改", "如何修改", "有哪些图片说明")):
        return None, None
    number_match = re.search(r"第\s*(\d{1,3})\s*张", value)
    if number_match is None:
        return None, "请明确第几张详情图片，例如“把第 2 张详情图片说明改为……”。"
    image_number = int(number_match.group(1))
    clear_pattern = re.search(r"清空.{0,30}(?:详情)?图片说明|(?:详情)?图片说明.{0,20}清空", value)
    if clear_pattern:
        return (image_number, ""), None
    description_match = re.search(
        r"(?:详情)?图片说明\s*(?:改为|改成|设为|设置为)\s*[“\"']?(.+?)[”\"']?\s*$",
        value,
        re.S,
    )
    if description_match is None:
        return None, "请在消息末尾写明新的图片说明。"
    description = description_match.group(1).strip().strip("“”\"'")
    if not description:
        return None, "图片说明不能为空。如需删除，请明确说“清空第 N 张详情图片说明”。"
    if len(description) > 8000:
        return None, "图片说明不能超过 8000 个字符。"
    return (image_number, description), None


def _requests_product_image_description_write(value: str) -> bool:
    compact = _compact(value)
    if not any(marker in compact for marker in ("详情图片说明", "图片说明")):
        return False
    if any(marker in compact for marker in ("如何修改", "怎么修改", "查看", "查询", "有哪些")):
        return False
    return any(marker in compact for marker in ("改为", "改成", "设为", "设置为", "清空"))


def _requests_admin_user_profile(value: str) -> bool:
    compact = _compact(value)
    if not any(marker in compact for marker in ("用户", "账号")):
        return False
    if any(marker in compact for marker in ("如何修改", "怎么修改", "查看用户", "查询用户")):
        return False
    return bool(
        re.search(r"(?:用户名|账号名).{0,16}(?:改为|改成|设为|设置为)", compact)
        or re.search(r"(?:用户)?邮箱.{0,16}(?:改为|改成|设为|设置为)", compact)
    )


def _requests_admin_user_create(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何创建用户", "怎么新增用户", "创建用户规则")):
        return False
    if "收货地址" in compact:
        return False
    return bool(
        re.search(r"(?:创建|新增|添加)(?:一个)?(?:普通)?(?:用户|用户账号)", compact)
        or re.search(r"(?:用户|用户账号).{0,12}(?:创建|新增)", compact)
    )


def _admin_user_create_fields(
    value: str,
) -> tuple[dict[str, str] | None, str | None]:
    username_match = re.search(
        r"(?:用户名|账号名|账号)\s*[:：=]\s*[“\"']?([A-Za-z0-9_]{1,64})[”\"']?",
        value,
    )
    email_match = re.search(
        r"(?:恢复邮箱|用户邮箱|邮箱)\s*[:：=]\s*[“\"']?([^\s，,；;。\"']{3,254})[”\"']?",
        value,
        re.I,
    )
    missing = [
        label
        for match, label in (
            (username_match, "用户名"),
            (email_match, "恢复邮箱"),
        )
        if match is None
    ]
    if missing:
        return None, (
            "创建普通用户还缺少"
            + "、".join(missing)
            + "。例如：创建用户，用户名: buyer2026，恢复邮箱: buyer@example.com。"
        )
    assert username_match is not None and email_match is not None
    username = username_match.group(1)
    email = email_match.group(1)
    if not 4 <= len(username) <= 32:
        return None, "用户名需为 4 至 32 位字母、数字或下划线。"
    try:
        normalized_email = normalize_target("email", email)
    except ValueError:
        return None, "恢复邮箱格式不正确，请输入完整邮箱。"
    return {
        "username": username,
        "email": normalized_email,
    }, None


def _requests_admin_user_password_reset(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何重置密码", "怎么重置密码", "密码重置规则")):
        return False
    return bool(
        re.search(r"(?:要求|强制|重置).{0,40}(?:用户|账号).{0,80}(?:重置)?密码", compact)
        or re.search(r"(?:用户|账号).{0,80}密码.{0,20}(?:重置|改为|设为)", compact)
    )


def _requests_admin_user_delete(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何删除用户", "怎么注销用户", "删除用户规则")):
        return False
    return bool(
        re.search(r"(?:删除|注销).{0,30}(?:用户|账号)", compact)
        or re.search(r"(?:用户|账号).{0,80}(?:删除|注销)", compact)
    )


def _requests_admin_user_asset_write(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何", "怎么", "查看", "查询", "列出", "有哪些")):
        return False
    if "购物车" in compact:
        return any(marker in compact for marker in ("数量", "件数", "删除", "移除", "清空"))
    if "收货地址" in compact or "默认地址" in compact:
        return any(
            marker in compact
            for marker in (
                "新增",
                "添加",
                "创建",
                "修改",
                "编辑",
                "改为",
                "改成",
                "删除",
                "设为默认",
                "设成默认",
            )
        )
    if "收藏" in compact or "关注" in compact:
        return any(marker in compact for marker in ("取消", "删除", "移除"))
    return False


def _requests_admin_cart_clear(compact_value: str) -> bool:
    if "购物车" not in compact_value:
        return False
    return "清空" in compact_value or any(
        marker in compact_value for marker in ("全部删除", "全部移除", "删除全部", "移除全部")
    )


def _requests_admin_evaluation_run(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("查看", "查询", "列出", "结果", "门禁状态")):
        return False
    return any(
        marker in compact for marker in ("启动ai评估", "运行ai评估", "发起ai评估", "执行ai评估")
    )


def _evaluation_run_payload(payload: dict[str, object]) -> EvaluationRunCreate:
    """Rebuild business arguments without approval-card presentation metadata."""

    return EvaluationRunCreate.model_validate(
        {
            "dataset_id": payload.get("dataset_id"),
            "dataset_version": payload.get("dataset_version"),
            "baseline_type": payload.get("baseline_type"),
            "baseline_version": payload.get("baseline_version"),
            "candidate_type": payload.get("candidate_type"),
            "candidate_version": payload.get("candidate_version"),
            "require_significant_gain": payload.get("require_significant_gain", False),
        }
    )


def _admin_address_fields(value: str, *, require_all: bool) -> tuple[dict[str, object], str | None]:
    fields: dict[str, object] = {}
    recipient = _address_field_value(value, ("收货人", "姓名"))
    phone = _address_field_value(value, ("联系电话", "手机号", "电话"))
    region = _address_field_value(value, ("地区",))
    detail = _address_field_value(value, ("详细地址",))
    if recipient is not None:
        if not 1 <= len(recipient) <= 64:
            return {}, "收货人需为 1 至 64 个字符。"
        fields["recipient_name"] = recipient
    if phone is not None:
        normalized_phone = re.sub(r"\s+", "", phone)
        if not re.fullmatch(r"[0-9+\-]{7,32}", normalized_phone):
            return {}, "联系电话需为 7 至 32 位数字，可包含国际区号和连字符。"
        fields["phone"] = normalized_phone
    if region is not None:
        try:
            resolved = resolve_china_region(region)
        except ChinaRegionResolutionError as exc:
            return {}, str(exc)
        fields.update(
            {
                "province_code": resolved.province_code,
                "city_code": resolved.city_code,
                "district_code": resolved.district_code,
                "region_label": resolved.label,
            }
        )
    if detail is not None:
        if not 2 <= len(detail) <= 500:
            return {}, "详细地址需为 2 至 500 个字符。"
        fields["address"] = detail
    if require_all:
        missing_labels = [
            label
            for key, label in (
                ("recipient_name", "收货人"),
                ("phone", "联系电话"),
                ("province_code", "省/市/区"),
                ("address", "详细地址"),
            )
            if key not in fields
        ]
        if missing_labels:
            return {}, (
                "新增地址还缺少"
                + "、".join(missing_labels)
                + "。例如：给用户 tulubi 新增收货地址，收货人: 张三，联系电话: "
                "13800138000，地区: 广东省/深圳市/南山区，详细地址: 科技园 1 号。"
            )
        fields["is_default"] = any(
            marker in _compact(value) for marker in ("设为默认", "设成默认", "默认地址")
        )
    return fields, None


def _requests_admin_address_update(value: str) -> bool:
    compact = _compact(value)
    return any(marker in compact for marker in ("修改收货地址", "编辑收货地址")) or bool(
        re.search(
            r"(?:收货人|联系电话|手机号|电话|地区|详细地址).{0,8}"
            r"(?:改为|改成|设为|设置为)",
            value,
        )
    )


def _address_field_value(value: str, labels: tuple[str, ...]) -> str | None:
    label_group = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{label_group})\s*(?::|：|改为|改成|设为|设置为)\s*[“\"']?"
        r"([^,，;；。\n]+)",
        value,
    )
    if match is None:
        return None
    return match.group(1).strip().strip("“”\"'") or None


def _mask_phone(value: str) -> str:
    if len(value) <= 7:
        return value[:2] + "***" + value[-2:]
    return value[:3] + "****" + value[-4:]


def _admin_user_profile_changes(value: str) -> tuple[dict[str, str], str | None]:
    changes: dict[str, str] = {}
    username_match = re.search(
        r"(?:用户名|账号名)\s*(?:改为|改成|设为|设置为)\s*[“\"']?"
        r"([A-Za-z0-9_]{1,64})[”\"']?",
        value,
    )
    if username_match:
        username = username_match.group(1)
        if not 4 <= len(username) <= 32:
            return {}, "用户名需为 4 至 32 位字母、数字或下划线。"
        changes["username"] = username
    email_match = re.search(
        r"(?:用户)?邮箱\s*(?:改为|改成|设为|设置为)\s*[“\"']?"
        r"([^\s，,\uff1b;。”\"']{3,254})",
        value,
        re.I,
    )
    if email_match:
        changes["email"] = email_match.group(1).strip()
    if not changes:
        return {}, "请明确新的用户名或完整邮箱。"
    return changes, None


def _requests_admin_store_profile(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何修改店铺", "怎么修改店铺", "查询店铺")):
        return False
    return any(
        marker in compact
        for marker in (
            "店铺名称改",
            "店铺名称设",
            "店名改",
            "店名设",
            "店铺简介改",
            "店铺简介设",
            "清空店铺简介",
            "商家邮箱改",
            "商家邮箱设",
            "恢复邮箱改",
            "恢复邮箱设",
            "店铺邮箱改",
            "店铺邮箱设",
        )
    )


def _requests_admin_store_create(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何创建店铺", "怎么新增店铺", "创建店铺规则")):
        return False
    return bool(
        re.search(r"(?:创建|新增|添加)(?:一个)?(?:商家)?店铺", compact)
        or re.search(r"店铺.{0,12}(?:创建|新增)", compact)
    )


def _admin_store_create_fields(
    value: str,
) -> tuple[dict[str, str] | None, str | None]:
    store_name_match = re.search(
        r"(?:店铺名称|店名)\s*[:：=]\s*[“\"']?(.+?)[”\"']?"
        r"(?=\s*[，,；;。]\s*(?:商家用户名|商家账号|店铺简介|商家邮箱|邮箱)|$)",
        value,
    )
    username_match = re.search(
        r"(?:商家用户名|商家账号)\s*[:：=]\s*[“\"']?([A-Za-z0-9_]{1,64})[”\"']?",
        value,
    )
    email_match = re.search(
        r"(?:商家恢复邮箱|商家邮箱|邮箱)\s*[:：=]\s*[“\"']?([^\s，,；;。\"']{3,254})[”\"']?",
        value,
        re.I,
    )
    missing = [
        label
        for match, label in (
            (store_name_match, "店铺名称"),
            (username_match, "商家用户名"),
            (email_match, "商家恢复邮箱"),
        )
        if match is None
    ]
    if missing:
        return None, (
            "创建店铺还缺少"
            + "、".join(missing)
            + "。例如：创建店铺，店铺名称: 示例文具店，商家用户名: demo_store，"
            "商家邮箱: store@example.com。"
        )
    assert store_name_match is not None and username_match is not None and email_match is not None
    store_name = " ".join(store_name_match.group(1).strip().strip("“”\"'").split())
    username = username_match.group(1)
    email = email_match.group(1)
    if not 2 <= len(store_name) <= 128:
        return None, "店铺名称需为 2 至 128 个字符。"
    if not 4 <= len(username) <= 32:
        return None, "商家用户名需为 4 至 32 位字母、数字或下划线。"
    try:
        normalized_email = normalize_target("email", email)
    except ValueError:
        return None, "商家恢复邮箱格式不正确，请输入完整邮箱。"
    description_match = re.search(
        r"店铺简介\s*[:：=]\s*[“\"']?(.+?)[”\"']?\s*$",
        value,
        re.S,
    )
    description = description_match.group(1).strip().strip("“”\"'") if description_match else None
    if description is not None and len(description) > 2000:
        return None, "店铺简介不能超过 2000 个字符。"
    return {
        "store_name": store_name,
        "merchant_username": username,
        "merchant_email": normalized_email,
        **({"description": description} if description is not None else {}),
    }, None


def _requests_admin_store_delete(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何删除店铺", "怎么注销店铺", "删除店铺规则")):
        return False
    return bool(
        re.search(r"(?:删除|注销).{0,30}(?:店铺|商家)", compact)
        or re.search(r"(?:店铺|商家).{0,80}(?:删除|注销)", compact)
    )


def _admin_store_profile_changes(value: str) -> tuple[dict[str, str], str | None]:
    return _merchant_profile_changes(value)


def _requests_admin_product_profile(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何修改商品", "怎么修改商品", "查看商品")):
        return False
    return bool(
        re.search(r"商品名称.{0,16}(?:改为|改成|设为|设置为)", compact)
        or re.search(r"商品描述.{0,16}(?:改为|改成|设为|设置为)", compact)
        or "清空商品描述" in compact
    )


def _admin_product_profile_changes(value: str) -> tuple[dict[str, str], str | None]:
    changes: dict[str, str] = {}
    name_match = re.search(
        r"商品名称\s*(?:改为|改成|设为|设置为)\s*[“\"']?(.+?)[”\"']?"
        r"(?=(?:[，,\uff1b;。]\s*(?:并且|并)?\s*(?:把|将)?\s*商品描述)|$)",
        value,
        re.S,
    )
    if name_match:
        name = " ".join(name_match.group(1).strip().strip("“”\"'").split())
        if not 1 <= len(name) <= 255:
            return {}, "商品名称需为 1 至 255 个字符。"
        changes["product_name"] = name
    description_match = re.search(
        r"商品描述\s*(?:改为|改成|设为|设置为)\s*[“\"']?(.+?)[”\"']?\s*$",
        value,
        re.S,
    )
    if description_match:
        description = description_match.group(1).strip().strip("“”\"'")
        if len(description) > 2000:
            return {}, "商品描述不能超过 2000 个字符。"
        changes["description"] = description
    elif "清空商品描述" in _compact(value):
        changes["description"] = ""
    if not changes:
        return {}, "请明确新的商品名称或商品描述。"
    return changes, None


def _requests_admin_product_review(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何审核商品", "商品审核规则", "查看审核")):
        return False
    return any(
        marker in compact
        for marker in (
            "审核通过商品",
            "批准商品",
            "通过商品审核",
            "驳回商品",
            "拒绝商品",
            "要求商品修改",
            "商品需要修改",
        )
    )


def _admin_product_review_decision(
    value: str,
) -> tuple[Literal["approve", "reject", "request_changes"] | None, str, str | None]:
    compact = _compact(value)
    decisions: list[Literal["approve", "reject", "request_changes"]] = []
    if any(marker in compact for marker in ("审核通过", "批准商品", "通过商品审核")):
        decisions.append("approve")
    if any(marker in compact for marker in ("驳回商品", "拒绝商品")):
        decisions.append("reject")
    if any(marker in compact for marker in ("要求商品修改", "商品需要修改")):
        decisions.append("request_changes")
    if len(set(decisions)) != 1:
        return None, "", "请只选择一个审核决定: 通过、驳回或要求修改。"
    reason_match = re.search(r"(?:原因|理由|说明)\s*[\uff1a:]?\s*(.{2,500})$", value, re.S)
    reason = (
        reason_match.group(1).strip().strip("“”\"'") if reason_match else "符合平台商品审核规则"
    )
    if decisions[0] != "approve" and reason_match is None:
        return None, "", "驳回或要求修改时必须说明具体原因。"
    return decisions[0], reason[:500], None


def _mask_email(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "***"
    prefix = local[:1] if local else ""
    suffix = local[-1:] if len(local) > 1 else ""
    return f"{prefix}***{suffix}@{domain}"


def _merchant_profile_changes(value: str) -> tuple[dict[str, str], str | None]:
    compact = _compact(value)
    if not any(
        marker in compact
        for marker in (
            "店铺名称改",
            "店铺名称设",
            "店名改",
            "店名设",
            "店铺简介改",
            "店铺简介设",
            "清空店铺简介",
        )
    ):
        return {}, None
    changes: dict[str, str] = {}
    name_match = re.search(
        r"(?:店铺名称|店名)\s*(?:改为|改成|改回|设为|设置为|更名为)\s*"
        r"[“\"']?(.+?)[”\"']?"
        r"(?=(?:[，,\uff1b;。]\s*(?:并且|并)?\s*(?:把|将)?\s*(?:店铺简介|简介))|$)",
        value,
        re.S,
    )
    if name_match:
        name = " ".join(name_match.group(1).strip().strip("“”\"'").split())
        if not 2 <= len(name) <= 128:
            return {}, "店铺名称需为 2 至 128 个字符。"
        changes["store_name"] = name
    description_match = re.search(
        r"(?:店铺简介|简介)\s*(?:改为|改成|改回|设为|设置为)\s*"
        r"[“\"']?(.+?)[”\"']?\s*$",
        value,
        re.S,
    )
    if description_match:
        description = description_match.group(1).strip().strip("“”\"'")
        if len(description) > 2000:
            return {}, "店铺简介不能超过 2000 个字符。"
        changes["description"] = description
    elif "清空店铺简介" in compact:
        changes["description"] = ""
    if not changes:
        return {}, "请明确新的店铺名称或店铺简介，例如“把店铺简介改为……”。"
    return changes, None


def _requested_store_email_update(value: str) -> tuple[str | None, str | None]:
    compact = _compact(value)
    if not any(
        marker in compact
        for marker in (
            "商家邮箱改",
            "商家邮箱设",
            "恢复邮箱改",
            "恢复邮箱设",
            "店铺邮箱改",
            "店铺邮箱设",
        )
    ):
        return None, None
    match = re.search(
        r"(?:商家邮箱|恢复邮箱|店铺邮箱)\s*"
        r"(?:改为|改成|设为|设置为|换成|更换为)\s*[“\"']?"
        r"([^\s，,；;。\"']{3,254})[”\"']?",
        value,
        re.I,
    )
    if match is None:
        return None, "请明确新的完整商家恢复邮箱，例如“把商家邮箱改为 store@example.com”。"
    try:
        return normalize_target("email", match.group(1).strip()), None
    except ValueError:
        return None, "新的商家恢复邮箱格式不正确，请输入完整邮箱。"


async def _email_credential(
    session: AsyncSession, user_id: int, *, for_update: bool = False
) -> UserCredential | None:
    query = select(UserCredential).where(
        UserCredential.user_id == user_id,
        UserCredential.credential_type == "email",
        UserCredential.credential_status == "active",
    )
    if for_update:
        query = query.with_for_update()
    return cast(UserCredential | None, await session.scalar(query))


def _credential_email(credential: UserCredential | None) -> str | None:
    if credential is None or credential.identifier_ciphertext is None:
        return None
    try:
        return SecurityService(get_settings()).decrypt(
            "user-credential:email", credential.identifier_ciphertext
        )
    except ValueError:
        return None


def _normalize_store_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _mentions_username(username: str, value: str) -> bool:
    if not username:
        return False
    escaped = re.escape(username)
    if re.fullmatch(r"[A-Za-z0-9_]+", username):
        return bool(re.search(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])", value, re.I))
    if len(username) >= 2:
        return _compact(username) in _compact(value)
    return any(marker in value for marker in (f"“{username}”", f'"{username}"', f"'{username}'"))


def _requests_product_status_change(value: str) -> bool:
    compact = _compact(value)
    return bool(
        re.search(r"(?:把|将|帮我|请|替我|给我).{0,180}(?:下架|上架)", compact)
        or re.match(r"^(?:下架|上架)(?:这个|该|商品|产品)", compact)
    )


def _requests_product_delete(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何删除商品", "怎么删除商品", "删除商品规则")):
        return False
    return bool(
        re.search(
            r"(?:把|将|帮我|请|替我|给我).{0,180}(?:永久删除|彻底删除|删除)"
            r"(?:这个|该|商品|产品)",
            compact,
        )
        or re.search(
            r"(?:把|将|帮我|请|替我|给我).{0,180}(?:商品|产品).{0,80}"
            r"(?:永久删除|彻底删除|删除)",
            compact,
        )
        or re.search(r"(?:永久删除|彻底删除|删除).{0,180}(?:商品|产品)", compact)
        or re.match(r"^(?:永久删除|彻底删除|删除)(?:这个|该|商品|产品)", compact)
    )


def _requests_product_submit(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何提交审核", "怎么提交审核", "审核规则")):
        return False
    return bool(
        re.search(
            r"(?:把|将|帮我|请|替我|给我).{0,180}(?:商品|产品).{0,80}"
            r"(?:提交审核|上传审核|发布|提交并审核)",
            compact,
        )
        or re.search(
            r"(?:提交审核|上传审核|上传并审核|提交并审核|发布).{0,180}(?:商品|产品)",
            compact,
        )
    )


def _requests_refund_decision(value: str) -> bool:
    compact = _compact(value)
    if any(marker in compact for marker in ("如何处理退款", "怎么审核售后", "退款规则")):
        return False
    return any(
        marker in compact
        for marker in (
            "同意退款",
            "批准退款",
            "通过退款",
            "同意售后",
            "批准售后",
            "拒绝退款",
            "驳回退款",
            "拒绝售后",
            "驳回售后",
            "补充材料",
            "补材料",
            "补充凭证",
            "补凭证",
            "提供材料",
            "提供凭证",
        )
    )


def _refund_decision_reason(value: str) -> str | None:
    match = re.search(r"(?:原因|理由|因为)\s*[\uFF1A:]?\s*(.{2,500})$", value, re.S)
    if match is None:
        return None
    reason = match.group(1).strip().strip("“”\"'")
    return reason[:500] if len(reason) >= 2 else None


def _refund_more_info_requirements(value: str) -> str | None:
    match = re.search(
        r"(?:补充材料|补材料|补充凭证|补凭证|提供材料|提供凭证)\s*"
        r"(?:为|是|包括)?\s*[：:]?\s*(.{2,500})$",
        value,
        re.S,
    )
    if match is None:
        return None
    requirements = match.group(1).strip().strip("，。；;：:“”\"'")
    return requirements[:500] if len(requirements) >= 2 else None


def _requests_admin_user_change(value: str) -> bool:
    compact = _compact(value)
    return bool(
        re.search(r"(?:把|将|帮我|请|替我|给我).{0,120}(?:冻结|解冻|强制下线)", compact)
        or re.match(r"^(?:冻结|解冻|强制下线)(?:用户|账号)", compact)
    )


def _requests_admin_wallet_adjustment(value: str) -> bool:
    compact = _compact(value)
    has_amount = bool(re.search(r"\d+(?:\.\d{1,2})?(?:元|块|人民币)", compact))
    has_wallet = any(marker in compact for marker in ("余额", "账户", "钱包", "充值"))
    has_action = any(
        marker in compact for marker in ("增加", "加上", "充值", "扣减", "扣除", "减少")
    )
    return has_amount and has_wallet and has_action


def _parse_admin_wallet_adjustment(
    value: str,
) -> tuple[str | None, int | None, str | None]:
    compact = _compact(value)
    amounts = re.findall(r"(?<![\d.])(\d+(?:\.\d{1,2})?)(?:元|块|人民币)", compact)
    if len(amounts) != 1:
        return None, None, "请只提供一个明确的人民币调整金额，例如 10 元。"
    try:
        amount = Decimal(amounts[0])
    except InvalidOperation:
        return None, None, "调整金额格式不正确。"
    amount_minor = int(amount * 100)
    if amount_minor < 1 or amount_minor > 100_000_000:
        return None, None, "单次调整金额必须在 0.01 元至 100 万元之间。"
    debit = any(marker in compact for marker in ("扣减", "扣除", "减少"))
    credit = any(marker in compact for marker in ("增加", "加上", "充值"))
    if debit == credit:
        return None, None, "请明确是增加余额还是扣减余额。"
    return ("debit" if debit else "credit"), amount_minor, None


def _money(value: int) -> str:
    return f"¥{value / 100:.2f}"


def _store_status_label(value: str) -> str:
    return {
        "active": "营业中",
        "suspended": "暂停营业",
        "closed": "已关闭",
        "pending": "待开通",
    }.get(value, value)


def _product_status_label(value: str) -> str:
    return {
        "on_sale": "销售中",
        "off_shelf": "已下架",
        "draft": "草稿",
        "pending_review": "审核中",
        "needs_revision": "需修改",
        "rejected": "需修改",
        "deleted": "已删除",
    }.get(value, value)


def _user_status_label(value: str) -> str:
    return {"active": "正常", "suspended": "已冻结", "closed": "已注销"}.get(value, value)
