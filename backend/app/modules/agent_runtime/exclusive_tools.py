from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.modules.after_sale.schemas import (
    RefundEligibilityItemRequest,
    RefundEligibilityRequest,
)
from app.modules.after_sale.service import AfterSaleService
from app.modules.agent_runtime.exclusive_context import TrustedExclusiveAgentContext
from app.modules.agent_runtime.models import AgentToolAudit
from app.modules.agent_runtime.store_tools import StoreToolResult, _contains_scope_override
from app.modules.catalog.repository import CatalogRepository
from app.modules.logistics.service import LogisticsService
from app.modules.messaging.human_schemas import HumanHandoffRequest
from app.modules.messaging.models import HumanServiceTicket
from app.modules.messaging.service import MessagingService
from app.modules.orders.domain import OrderPolicySnapshot, available_action_codes
from app.modules.orders.models import Order, OrderItem
from app.modules.stores.models import Store

logger = logging.getLogger(__name__)


class ExclusiveToolGateway:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        security: SecurityService,
    ) -> None:
        self.session = session
        self.settings = settings
        self.security = security
        self.catalog = CatalogRepository(session)
        self.after_sale = AfterSaleService(session, settings, security)

    async def execute(
        self,
        context: TrustedExclusiveAgentContext,
        tool_code: str,
        arguments: dict[str, object],
        handler: Callable[[], Awaitable[dict[str, object]]],
    ) -> StoreToolResult:
        started = time.monotonic()
        arguments_hash = hashlib.sha256(
            json.dumps(
                arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).digest()
        if tool_code not in context.allowed_tools:
            result = StoreToolResult("denied", {}, "TOOL_NOT_ALLOWED")
        elif _contains_scope_override(arguments):
            result = StoreToolResult("denied", {}, "TOOL_SCOPE_OVERRIDE_DENIED")
        else:
            try:
                result = StoreToolResult("succeeded", await handler())
            except ApplicationError as exc:
                result = StoreToolResult("denied", {}, exc.code)
            except TimeoutError:
                result = StoreToolResult("unknown", {}, "TOOL_TIMEOUT_UNKNOWN")
            except Exception:
                logger.exception("exclusive agent tool failed", extra={"tool_code": tool_code})
                result = StoreToolResult("failed", {}, "TOOL_EXECUTION_FAILED")
        self.session.add(
            AgentToolAudit(
                audit_no=new_prefixed_ulid("taud_"),
                run_id=context.run.id,
                tool_code=tool_code,
                scope_snapshot=context.trusted_scope,
                arguments_hash=arguments_hash,
                outcome=result.status,
                error_code=result.error_code,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        )
        await self.session.flush()
        return result

    async def search_products(
        self, context: TrustedExclusiveAgentContext, query: str | None
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            rows = []
            has_more = False
            for term in _catalog_search_candidates(query):
                found, found_has_more = await self.catalog.search_products(
                    q=term,
                    category_no=None,
                    brand_no=None,
                    store_no=None,
                    group_no=None,
                    price_min=None,
                    price_max=None,
                    sort="sales",
                    position=None,
                    limit=8,
                )
                rows = found
                has_more = found_has_more
                if rows:
                    break
            return {
                "items": [
                    {
                        "product_id": product.product_no,
                        "store_id": store.store_no,
                        "store_name": store.store_name,
                        "name": product.product_name,
                        "subtitle": product.subtitle,
                        "price": {
                            "min_amount": product.min_price_amount,
                            "max_amount": product.max_price_amount,
                            "currency": product.currency,
                        },
                        "source_version": product.version,
                    }
                    for product, store in rows
                ],
                "has_more": has_more,
                "as_of": utc_now(),
            }

        return await self.execute(
            context,
            "catalog.search_products",
            {"query": (query or "")[:120]},
            handler,
        )

    async def list_orders(self, context: TrustedExclusiveAgentContext) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            rows = list(
                (
                    await self.session.execute(
                        select(Order, Store)
                        .join(Store, Store.id == Order.store_id)
                        .where(Order.user_id == context.user.id, Order.user_hidden_at.is_(None))
                        .order_by(Order.created_at.desc(), Order.id.desc())
                        .limit(5)
                    )
                ).all()
            )
            return {
                "items": [self._order_projection(order, store, []) for order, store in rows],
                "as_of": utc_now(),
            }

        return await self.execute(context, "order.list_user_orders", {}, handler)

    async def order_detail(
        self, context: TrustedExclusiveAgentContext, order_no: str
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            row = (
                await self.session.execute(
                    select(Order, Store)
                    .join(Store, Store.id == Order.store_id)
                    .where(Order.order_no == order_no, Order.user_id == context.user.id)
                )
            ).one_or_none()
            if row is None:
                raise _not_accessible()
            order, store = row
            items = list(
                (
                    await self.session.scalars(
                        select(OrderItem)
                        .where(OrderItem.order_id == order.id)
                        .order_by(OrderItem.id)
                    )
                ).all()
            )
            return self._order_projection(order, store, items)

        return await self.execute(
            context, "order.get_user_order_detail", {"order_id": order_no}, handler
        )

    async def shipments(
        self, context: TrustedExclusiveAgentContext, order_no: str
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            service = LogisticsService(
                self.session,
                self.security,
                self.settings.security_hmac_secret.get_secret_value(),
            )
            result = await service.list_for_order(context.user, order_no)
            return result.model_dump(mode="json")

        return await self.execute(
            context,
            "logistics.get_user_order_shipments",
            {"order_id": order_no},
            handler,
        )

    async def list_refunds(self, context: TrustedExclusiveAgentContext) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            result, pagination = await self.after_sale.list_mine(context.user, 10)
            return {
                **result.model_dump(mode="json"),
                "has_more": pagination.has_next,
            }

        return await self.execute(context, "after_sale.list_user_refunds", {}, handler)

    async def refund_detail(
        self, context: TrustedExclusiveAgentContext, refund_no: str
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            result = await self.after_sale.detail(context.user, refund_no)
            return result.model_dump(mode="json")

        return await self.execute(
            context,
            "after_sale.get_user_refund_detail",
            {"refund_id": refund_no},
            handler,
        )

    async def refund_precheck(
        self, context: TrustedExclusiveAgentContext, order_no: str
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            row = (
                await self.session.execute(
                    select(Order, Store)
                    .join(Store, Store.id == Order.store_id)
                    .where(Order.order_no == order_no, Order.user_id == context.user.id)
                )
            ).one_or_none()
            if row is None:
                raise _not_accessible()
            order, store = row
            items = list(
                (
                    await self.session.scalars(
                        select(OrderItem)
                        .where(OrderItem.order_id == order.id)
                        .order_by(OrderItem.id)
                    )
                ).all()
            )
            result = self._order_projection(order, store, items)
            candidates = [
                RefundEligibilityItemRequest(
                    order_item_id=item.order_item_no,
                    quantity=item.quantity - item.refunded_quantity,
                )
                for item in items
                if item.refunded_quantity < item.quantity
                and item.refunded_amount < item.payable_amount
            ]
            if candidates:
                eligibility = await self.after_sale.eligibility(
                    context.user,
                    RefundEligibilityRequest(
                        order_id=order.order_no,
                        items=candidates,
                        requested_type="refund_only",
                        reason_code="OTHER",
                    ),
                )
                result["refund_eligibility"] = eligibility.model_dump(
                    mode="json", exclude={"eligibility_token", "expires_at"}
                )
            else:
                result["refund_eligibility"] = {
                    "eligible": False,
                    "allowed_types": [],
                    "blocking_reasons": ["REFUND_ITEM_CAPACITY_CHANGED"],
                }
            logistics = LogisticsService(
                self.session,
                self.security,
                self.settings.security_hmac_secret.get_secret_value(),
            )
            result["shipments"] = (
                await logistics.list_for_order(context.user, order_no)
            ).model_dump(mode="json").get("items", [])
            return result

        return await self.execute(
            context,
            "after_sale.check_refund_eligibility",
            {"order_id": order_no},
            handler,
        )

    async def handoff(
        self, context: TrustedExclusiveAgentContext, reason_code: str
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            ticket = await MessagingService(self.session).request_human_from_agent(
                context.user,
                context.conversation.conversation_no,
                HumanHandoffRequest(
                    ticket_type="general",
                    summary=f"专属客服转平台人工: {reason_code}",
                    message_refs=[context.trigger.message_no],
                ),
                context.run.run_no,
            )
            return {
                "ticket_id": ticket.ticket_id,
                "ticket_status": ticket.ticket_status,
                "queue_type": ticket.queue_type,
            }

        return await self.execute(
            context,
            "support.create_platform_ticket",
            {"reason_code": reason_code},
            handler,
        )

    async def ticket_status(self, context: TrustedExclusiveAgentContext) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            ticket = await self.session.scalar(
                select(HumanServiceTicket).where(
                    HumanServiceTicket.conversation_id == context.conversation.id,
                    HumanServiceTicket.active_key == 1,
                )
            )
            return {
                "ticket": (
                    {
                        "ticket_id": ticket.ticket_no,
                        "ticket_status": ticket.ticket_status,
                        "queue_type": ticket.queue_type,
                    }
                    if ticket is not None
                    else None
                )
            }

        return await self.execute(context, "support.get_ticket_status", {}, handler)

    @staticmethod
    def _order_projection(order: Order, store: Store, items: list[OrderItem]) -> dict[str, object]:
        policy = OrderPolicySnapshot(
            order_status=order.order_status,
            payment_status=order.payment_status,
            fulfillment_status=order.fulfillment_status,
            after_sale_status=order.after_sale_status,
            paid_amount=order.paid_amount,
            expires_at=order.expires_at,
            all_reviews_terminal=all(
                item.review_status in {"reviewed", "closed"} for item in items
            ),
            has_pending_review=any(item.review_status == "pending" for item in items),
            has_after_sale_history=order.after_sale_status != "none",
            has_refundable_items=any(
                item.refunded_quantity < item.quantity
                and item.refunded_amount < item.payable_amount
                for item in items
            ),
        )
        return {
            "order_id": order.order_no,
            "store_id": store.store_no,
            "store_name": store.store_name,
            "status": {
                "order": order.order_status,
                "payment": order.payment_status,
                "fulfillment": order.fulfillment_status,
                "after_sale": order.after_sale_status,
            },
            "amounts": {
                "paid": _money_projection(order.paid_amount, order.currency),
                "refunded": _money_projection(order.refunded_amount, order.currency),
            },
            "items": [
                {
                    "order_item_id": item.order_item_no,
                    "product_id": item.product_no,
                    "sku_id": item.sku_no,
                    "product_name": item.product_name,
                    "sku_name": item.sku_name,
                    "quantity": item.quantity,
                    "refunded_quantity": item.refunded_quantity,
                }
                for item in items
            ],
            "available_actions": available_action_codes(policy, utc_now()),
            "source_version": order.version,
            "as_of": utc_now(),
        }


def _money_projection(minor_units: int, currency: str) -> dict[str, str]:
    normalized = currency.upper()
    symbol = "¥" if normalized == "CNY" else f"{normalized} "
    major_units = f"{minor_units / 100:.2f}"
    return {
        "minor_units": str(minor_units),
        "major_units": major_units,
        "currency": normalized,
        "display": f"{symbol}{major_units}",
    }


def _catalog_search_candidates(query: str | None) -> list[str | None]:
    """Return bounded public-catalog queries from an untrusted natural sentence.

    The LLM may return a whole instruction instead of the product phrase. We first
    try the cleaned phrase, then its meaningful tokens. A truly broad listing
    request is allowed to fall back to the public on-sale catalogue; a specific
    nonexistent term is never silently replaced with every product.
    """

    raw = re.sub(r"\s+", " ", (query or "").strip())[:120]
    if not raw:
        return [None]
    cleaned = raw
    for phrase in (
        "请列出",
        "请搜索",
        "请查找",
        "帮我列出",
        "帮我搜索",
        "告诉我",
        "并说明推荐依据",
        "说明推荐依据",
        "不要转人工",
        "全平台当前在售的",
        "全平台在售的",
        "平台当前在售的",
        "当前在售的",
        "在售的",
        "商品名、价格和店铺",
        "商品名价格和店铺",
    ):
        cleaned = cleaned.replace(phrase, " ")
    cleaned = re.sub(r"[\u3001\u3002\uff0c\uff01\uff1f,:;!?\uff08\uff09()\[\]{}]+", " ", cleaned)
    cleaned = re.sub(r"\b(?:please|search|find|products?)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned.endswith("商品") and len(cleaned) > 2:
        cleaned = cleaned.removesuffix("商品").strip()
    candidates: list[str | None] = []
    generic_terms = {"", "商品", "在售", "全部", "所有", "在售商品", "全部商品", "所有商品"}
    if cleaned and cleaned not in generic_terms:
        candidates.append(cleaned[:120])
        for token in cleaned.split():
            token = token.strip()
            if len(token) >= 2 and token not in {"商品", "在售", "当前", "平台"}:
                candidates.append(token[:120])
    broad = any(
        marker in raw
        for marker in ("全部商品", "所有商品", "当前在售", "平台在售", "全平台")
    ) and cleaned in generic_terms
    if broad or cleaned in generic_terms:
        candidates.append(None)
    result: list[str | None] = []
    for candidate in candidates or [raw]:
        if candidate not in result:
            result.append(candidate)
    return result[:6]


def _not_accessible() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="AGENT_RESOURCE_NOT_ACCESSIBLE",
        title="Resource not accessible",
        detail="请求的资源不存在或不可访问。",
    )
