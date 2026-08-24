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
        "order.get_store_order_summary",
        "order.list_user_orders",
        "order.get_user_order_detail",
        "logistics.get_store_order_shipments",
        "logistics.get_user_order_shipments",
        "after_sale.check_refund_eligibility",
        "after_sale.build_refund_draft",
        "after_sale.list_user_refunds",
        "after_sale.get_user_refund_detail",
        "support.get_ticket_status",
        "memory.list_mine",
    }
)

CONFIRMATION_REQUIRED_TOOLS = frozenset(
    {
        "after_sale.submit_refund_application",
        "support.create_store_ticket",
        "support.create_platform_ticket",
        "memory.remember_preference",
        "memory.delete_mine",
    }
)


def authorize_tool(call: ToolCall, allowed_tools: frozenset[str]) -> None:
    if call.tool_code not in allowed_tools:
        raise PermissionError("tool is not allowed for this agent version")
    if call.tool_code not in READ_ONLY_TOOLS | CONFIRMATION_REQUIRED_TOOLS:
        raise PermissionError("unregistered tools are denied")
    if call.tool_code in CONFIRMATION_REQUIRED_TOOLS and not call.scope.approval_no:
        raise PermissionError("a trusted approval is required")
    if not call.scope.user_no or not call.scope.conversation_no:
        raise PermissionError("trusted user and conversation scope are required")
