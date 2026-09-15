from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ToolScope:
    user_no: str
    conversation_no: str
    store_no: str | None
    context_no: str | None
    context_version: int | None
    approval_no: str | None = None


@dataclass(frozen=True)
class ToolCall:
    protocol_version: Literal["2025-11-25"]
    tool_code: str
    arguments: dict[str, Any]
    scope: ToolScope


@dataclass(frozen=True)
class ToolResult:
    status: Literal["succeeded", "denied", "failed", "unknown"]
    safe_data: dict[str, Any]
    error_code: str | None = None


READ_ONLY_TOOLS = frozenset(
    {
        "catalog.product.get",  # canonical Phase-9 alias
        "catalog.search",
        "catalog.get_product",
        "catalog.compare_skus",
        "catalog.compare_products",
        "catalog.search_store_products",
        "catalog.search_products",
        "catalog.get_inventory_availability",
        "catalog.get_store_policy",
        "order.list_user_store_orders",
        "order.get_store_order_summary",
        "after_sale.list_user_store_refunds",
        "order.list_user_orders",
        "order.get_user_order_detail",
        "cart.get_mine",
        "address.list_mine",
        "account.profile.get_mine",
        "account.wallet.get_mine",
        "account.favorites.list_mine",
        "logistics.get_store_order_shipments",
        "logistics.get_user_order_shipments",
        "after_sale.check_refund_eligibility",
        "after_sale.build_refund_draft",
        "after_sale.list_user_refunds",
        "after_sale.get_user_refund_detail",
        "support.get_ticket_status",
        "memory.list_mine",
        "rag.policy.search",
        "store_ops.overview",
        "store_ops.profile.get",
        "store_ops.revenue_metrics",
        "store_ops.catalog_summary",
        "store_ops.catalog.get_product",
        "store_ops.order_summary",
        "store_ops.orders.list",
        "store_ops.orders.get",
        "store_ops.inventory_risks",
        "store_ops.inventory.get_skus",
        "store_ops.review_summary",
        "store_ops.reviews.list",
        "store_ops.service_summary",
        "store_ops.conversations.list",
        "store_ops.policy_summary",
        "store_ops.after_sale.list",
        "governance.platform_overview",
        "governance.metrics.query",
        "governance.user_summary",
        "governance.users.search",
        "governance.users.addresses.list",
        "governance.users.cart.list",
        "governance.users.favorites.list",
        "governance.users.orders.list",
        "governance.users.wallet.get",
        "governance.store_summary",
        "governance.stores.search",
        "governance.stores.service_profile",
        "governance.catalog.search",
        "governance.order_summary",
        "governance.trade.payment_timeline",
        "governance.trade.shipments.get",
        "governance.after_sale.timeline",
        "governance.after_sale_summary",
        "governance.support_summary",
        "governance.ai_summary",
        "governance.ai.agents.list",
        "governance.ai.skills.list",
        "governance.ai.tools.list",
        "governance.knowledge.documents.list",
        "governance.ai.evaluations.list",
        "observability.runtime_health",
        "observability.traces.search",
        "observability.traces.get",
        "observability.cost_metrics",
        "observability.dead_letters.list",
    }
)

# Reversible, account-scoped mutations that are safe to execute immediately.
# They still pass trusted identity/scope injection, policy budgets and auditing.
DIRECT_WRITE_TOOLS = frozenset(
    {
        "cart.add_item",
        "cart.update_quantity",
        "cart.remove_item",
        "checkout.create_session",
        "favorite.add_product",
        "favorite.remove_product",
        "favorite.add_store",
        "favorite.remove_store",
    }
)

CONFIRMATION_REQUIRED_TOOLS = frozenset(
    {
        "after_sale.submit_refund_application",
        "cart.clear.commit",
        "support.create_store_ticket",
        "support.create_platform_ticket",
        "memory.remember_preference",
        "memory.delete_mine",
        "store_ops.profile.update.commit",
        "store_ops.profile.logo.update.commit",
        "store_ops.account.email.update.commit",
        "store_ops.status.update.commit",
        "store_ops.catalog.status.commit",
        "store_ops.catalog.delete.commit",
        "store_ops.catalog.submit_review.commit",
        "store_ops.catalog.update_image_description.commit",
        "store_ops.catalog.fulfillment.update.commit",
        "store_ops.catalog.save_draft.commit",
        "store_ops.catalog.update.commit",
        "store_ops.catalog.skus.create.commit",
        "store_ops.catalog.skus.update.commit",
        "store_ops.catalog.skus.disable.commit",
        "store_ops.catalog.skus.image.replace.commit",
        "store_ops.catalog.faqs.upsert.commit",
        "store_ops.catalog.faqs.delete.commit",
        "store_ops.catalog.detail_sections.upsert.commit",
        "store_ops.catalog.detail_sections.delete.commit",
        "store_ops.policy.manage.commit",
        "store_ops.inventory.adjust.commit",
        "store_ops.price.update.commit",
        "store_ops.shipment.create.commit",
        "store_ops.shipment.progress.commit",
        "store_ops.review.reply.commit",
        "store_ops.after_sale.decide.commit",
        "store_ops.after_sale.request_more_info.commit",
        "store_ops.support.claim.commit",
        "store_ops.conversations.send_message.commit",
        "store_ops.support.resolve.commit",
        "governance.users.status.commit",
        "governance.users.force_logout.commit",
        "governance.users.create.commit",
        "governance.users.require_password_reset.commit",
        "governance.users.wallet.adjust.commit",
        "governance.users.update_profile.commit",
        "governance.users.avatar.update.commit",
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
        "governance.stores.status.commit",
        "governance.stores.create.commit",
        "governance.stores.update.commit",
        "governance.stores.logo.update.commit",
        "governance.stores.merchant_email.update.commit",
        "governance.stores.delete.commit",
        "governance.catalog.status.commit",
        "governance.catalog.delete.commit",
        "governance.catalog.update.commit",
        "governance.catalog.update_image_description.commit",
        "governance.catalog.faqs.upsert.commit",
        "governance.catalog.faqs.delete.commit",
        "governance.catalog.skus.create.commit",
        "governance.catalog.skus.update.commit",
        "governance.catalog.skus.disable.commit",
        "governance.catalog.skus.image.replace.commit",
        "governance.catalog.detail_sections.upsert.commit",
        "governance.catalog.detail_sections.delete.commit",
        "governance.catalog.review.commit",
        "governance.trade.orders.cancel.commit",
        "governance.trade.shipments.progress.commit",
        "governance.after_sale.decide.commit",
        "governance.after_sale.request_more_info.commit",
        "governance.support.claim.commit",
        "governance.support.send_message.commit",
        "governance.support.resolve.commit",
        "governance.knowledge.documents.publish.commit",
        "governance.knowledge.documents.withdraw.commit",
        "governance.ai.agents.prompt_draft.create.commit",
        "governance.ai.agents.publish_request.commit",
        "governance.ai.skills.publish_request.commit",
        "governance.ai.tools.publish_request.commit",
        "governance.ai.evaluations.run.commit",
        "observability.dead_letters.replay_request.commit",
    }
)


def authorize_tool(call: ToolCall, allowed_tools: frozenset[str]) -> None:
    if call.tool_code not in allowed_tools:
        raise PermissionError("tool is not allowed for this agent version")
    if call.tool_code not in READ_ONLY_TOOLS | DIRECT_WRITE_TOOLS | CONFIRMATION_REQUIRED_TOOLS:
        raise PermissionError("unregistered tools are denied")
    if call.tool_code in CONFIRMATION_REQUIRED_TOOLS and not call.scope.approval_no:
        raise PermissionError("a trusted approval is required")
    if not call.scope.user_no or not call.scope.conversation_no:
        raise PermissionError("trusted user and conversation scope are required")
