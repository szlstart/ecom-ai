from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import cast

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
from app.modules.agent_runtime.handoff_intent import is_explicit_handoff_request
from app.modules.agent_runtime.models import AgentToolAudit
from app.modules.agent_runtime.store_tools import (
    StoreToolResult,
    _availability_label,
    _available_quantity,
    _contains_scope_override,
)
from app.modules.cart.service import CartService
from app.modules.catalog.models import Product, ProductSku
from app.modules.catalog.repository import CatalogRepository
from app.modules.inventory.models import Inventory
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
        self._call_counts: dict[str, int] = {}

    async def execute(
        self,
        context: TrustedExclusiveAgentContext,
        tool_code: str,
        arguments: dict[str, object],
        handler: Callable[[], Awaitable[dict[str, object]]],
        *,
        trusted_approval_no: str | None = None,
    ) -> StoreToolResult:
        started = time.monotonic()
        arguments_hash = hashlib.sha256(
            json.dumps(
                arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).digest()
        policy = context.tool_policies.get(tool_code)
        call_count = self._call_counts.get(tool_code, 0)
        if tool_code not in context.allowed_tools or policy is None:
            result = StoreToolResult("denied", {}, "TOOL_NOT_ALLOWED")
        elif call_count >= policy.call_budget:
            result = StoreToolResult("denied", {}, "TOOL_CALL_BUDGET_EXCEEDED")
        elif policy.confirmation_policy != "none" and not (
            (
                tool_code == "support.create_platform_ticket"
                and is_explicit_handoff_request(context.trigger.text_content or "")
            )
            or (
                tool_code == "after_sale.submit_refund_application"
                and trusted_approval_no is not None
                and arguments.get("approval_id") == trusted_approval_no
            )
        ):
            result = StoreToolResult("denied", {}, "TOOL_CONFIRMATION_REQUIRED")
        elif _contains_scope_override(arguments):
            result = StoreToolResult("denied", {}, "TOOL_SCOPE_OVERRIDE_DENIED")
        else:
            self._call_counts[tool_code] = call_count + 1
            try:
                result = StoreToolResult(
                    "succeeded",
                    await asyncio.wait_for(handler(), timeout=policy.timeout_ms / 1000),
                )
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
        self,
        context: TrustedExclusiveAgentContext,
        query: str | None,
        *,
        fallback_query: str | None = None,
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            constraints = _catalog_search_constraints(query, fallback_query)
            candidates = list(constraints.candidates)
            if constraints.semantic_keywords:
                candidates = list(
                    dict.fromkeys([candidates[0], *constraints.semantic_keywords, *candidates[1:]])
                )
            rows = []
            seen_product_ids: set[int] = set()
            has_more = False
            for term in candidates:
                found, found_has_more = await self.catalog.search_products(
                    q=term,
                    category_no=None,
                    brand_no=None,
                    store_no=None,
                    group_no=None,
                    price_min=constraints.price_min,
                    price_max=constraints.price_max,
                    sort=constraints.sort,
                    position=None,
                    limit=8,
                )
                has_more = has_more or found_has_more
                for row in found:
                    product, _store = row
                    if product.id in seen_product_ids:
                        continue
                    seen_product_ids.add(product.id)
                    rows.append(row)
                if len(rows) >= constraints.requested_limit:
                    break
            if constraints.sort == "price_asc":
                rows.sort(key=lambda row: (row[0].min_price_amount, row[0].id))
            elif constraints.sort == "price_desc":
                rows.sort(key=lambda row: (row[0].min_price_amount, row[0].id), reverse=True)
            elif constraints.sort == "newest":
                rows.sort(
                    key=lambda row: (row[0].published_at or row[0].created_at, row[0].id),
                    reverse=True,
                )
            elif constraints.sort == "sales":
                rows.sort(key=lambda row: (row[0].sales_count, row[0].id), reverse=True)
            rows = rows[: constraints.requested_limit]
            skus_by_product: dict[int, list[dict[str, object]]] = {}
            stock_by_product: dict[int, int] = {}
            product_ids = [product.id for product, _store in rows]
            if product_ids:
                sku_rows = (
                    await self.session.execute(
                        select(ProductSku, Inventory)
                        .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
                        .where(
                            ProductSku.product_id.in_(product_ids),
                            ProductSku.sku_status == "active",
                        )
                        .order_by(ProductSku.product_id, ProductSku.id)
                    )
                ).all()
                for sku, inventory in sku_rows:
                    available = _available_quantity(inventory)
                    skus_by_product.setdefault(sku.product_id, []).append(
                        {
                            "sku_id": sku.sku_no,
                            "sku_name": sku.sku_name,
                            "price": _money_projection(sku.sale_price_amount, sku.currency),
                            "available_stock": available,
                            "availability_label": _availability_label(inventory),
                        }
                    )
                    stock_by_product[sku.product_id] = (
                        stock_by_product.get(sku.product_id, 0) + available
                    )
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
                        "available_stock": stock_by_product.get(product.id, 0),
                        "skus": skus_by_product.get(product.id, []),
                        "source_version": product.version,
                    }
                    for product, store in rows
                ],
                "has_more": has_more,
                "applied_filters": {
                    "keywords": list(constraints.keywords),
                    "price_min": constraints.price_min,
                    "price_max": constraints.price_max,
                    "sort": constraints.sort,
                },
                "as_of": utc_now(),
            }

        return await self.execute(
            context,
            "catalog.search_products",
            {
                "query": (query or "")[:120],
                "fallback_query": (fallback_query or "")[:120],
            },
            handler,
        )

    async def filter_recent_products(
        self,
        context: TrustedExclusiveAgentContext,
        product_nos: list[str],
        query: str,
    ) -> StoreToolResult:
        """Reapply changed hard constraints to the last visible product set."""

        async def handler() -> dict[str, object]:
            constraints = _catalog_search_constraints(None, query)
            rows: list[tuple[Product, Store]] = []
            for product_no in list(dict.fromkeys(product_nos))[:8]:
                row = await self.catalog.public_product(product_no)
                if row is None:
                    continue
                product, _store = row
                if (
                    constraints.price_min is not None
                    and product.min_price_amount < constraints.price_min
                ):
                    continue
                if (
                    constraints.price_max is not None
                    and product.min_price_amount > constraints.price_max
                ):
                    continue
                rows.append(row)
            if constraints.sort == "price_asc":
                rows.sort(key=lambda row: (row[0].min_price_amount, row[0].id))
            elif constraints.sort == "price_desc":
                rows.sort(key=lambda row: (row[0].min_price_amount, row[0].id), reverse=True)
            elif constraints.sort == "newest":
                rows.sort(
                    key=lambda row: (row[0].published_at or row[0].created_at, row[0].id),
                    reverse=True,
                )
            elif constraints.sort == "sales":
                rows.sort(key=lambda row: (row[0].sales_count, row[0].id), reverse=True)
            rows = rows[: constraints.requested_limit]
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
                "has_more": False,
                "continued_from_recent_results": True,
                "applied_filters": {
                    "price_min": constraints.price_min,
                    "price_max": constraints.price_max,
                    "sort": constraints.sort,
                },
                "as_of": utc_now(),
            }

        return await self.execute(
            context,
            "catalog.search_products",
            {"recent_product_ids": product_nos[:8], "query": query[:120]},
            handler,
        )

    async def list_orders(
        self,
        context: TrustedExclusiveAgentContext,
        query: str | None = None,
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            rows = cast(
                list[tuple[Order, Store]],
                list(
                    (
                        await self.session.execute(
                            select(Order, Store)
                            .join(Store, Store.id == Order.store_id)
                            .where(Order.user_id == context.user.id, Order.user_hidden_at.is_(None))
                            .order_by(Order.created_at.desc(), Order.id.desc())
                            .limit(20)
                        )
                    ).all()
                ),
            )
            order_ids = [order.id for order, _store in rows]
            order_items = (
                list(
                    (
                        await self.session.scalars(
                            select(OrderItem)
                            .where(OrderItem.order_id.in_(order_ids))
                            .order_by(OrderItem.order_id, OrderItem.id)
                        )
                    ).all()
                )
                if order_ids
                else []
            )
            items_by_order: dict[int, list[OrderItem]] = {}
            for item in order_items:
                items_by_order.setdefault(item.order_id, []).append(item)
            rows = _filter_order_rows(rows, items_by_order, query)[:5]
            return {
                "items": [
                    self._order_projection(order, store, items_by_order.get(order.id, []))
                    for order, store in rows
                ],
                "as_of": utc_now(),
                "presentation": "order_cards",
            }

        return await self.execute(
            context,
            "order.list_user_orders",
            {"query": (query or "")[:120]},
            handler,
        )

    async def get_cart(self, context: TrustedExclusiveAgentContext) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            view = await CartService(self.session).get(context.user)
            return view.model_dump(mode="json")

        return await self.execute(context, "cart.get_mine", {}, handler)

    async def compare_products(
        self, context: TrustedExclusiveAgentContext, product_nos: list[str]
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            items: list[dict[str, object]] = []
            for product_no in list(dict.fromkeys(product_nos))[:3]:
                row = await self.catalog.public_product(product_no)
                if row is None:
                    continue
                product, store = row
                sku_rows = await self.catalog.public_skus(product.id)
                available = sum(_available_quantity(inventory) for _sku, inventory in sku_rows)
                items.append(
                    {
                        "product_id": product.product_no,
                        "name": product.product_name,
                        "subtitle": product.subtitle,
                        "description": product.description,
                        "store_id": store.store_no,
                        "store_name": store.store_name,
                        "price": {
                            "min_amount": product.min_price_amount,
                            "max_amount": product.max_price_amount,
                            "currency": product.currency,
                        },
                        "available_stock": available,
                        "sku_count": len(sku_rows),
                        "rating": str(product.rating_score),
                        "sales_count": product.sales_count,
                        "source_version": product.version,
                    }
                )
            return {"items": items, "as_of": utc_now()}

        return await self.execute(
            context,
            "catalog.compare_products",
            {"product_ids": list(dict.fromkeys(product_nos))[:3]},
            handler,
        )

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

    async def latest_order_no(self, context: TrustedExclusiveAgentContext) -> str:
        order_no = await self.session.scalar(
            select(Order.order_no)
            .where(Order.user_id == context.user.id, Order.user_hidden_at.is_(None))
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(1)
        )
        if order_no is None:
            raise _not_accessible()
        return order_no

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
                (await logistics.list_for_order(context.user, order_no))
                .model_dump(mode="json")
                .get("items", [])
            )
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
                    # Keep both meanings explicit for model-grounded answers. A zero
                    # refunded quantity means nothing has been refunded, not that no
                    # quantity remains refundable.
                    "refunded_quantity": item.refunded_quantity,
                    "remaining_refundable_quantity": max(0, item.quantity - item.refunded_quantity),
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
    # Compatible providers sometimes retain requested output columns in
    # `search_text`, such as "铅笔商品，列出商品名、店铺、价格和库存".
    # Those columns are presentation instructions rather than catalog terms.
    raw_search_phrase = re.split(
        r"(?:[\uff0c,\uff1b;\u3002]\s*(?:请)?(?:列出|展示|显示|告诉我|说明)"
        r"|(?:所有|全部)?款式(?:的)?(?:名称|名字|价格|库存))",
        raw,
        maxsplit=1,
    )[0].strip()
    cleaned = raw
    for phrase in (
        "请列出",
        "请搜索",
        "请查找",
        "请告诉我",
        "帮我列出",
        "帮我搜索",
        "告诉我",
        "本店",
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
    if raw_search_phrase != raw:
        cleaned = raw_search_phrase
        for phrase in (
            "请列出",
            "请搜索",
            "请查找",
            "请告诉我",
            "帮我列出",
            "帮我搜索",
            "全平台当前在售的",
            "全平台在售的",
            "平台当前在售的",
            "当前在售的",
            "在售的",
            "不要转人工",
            "本店",
        ):
            cleaned = cleaned.replace(phrase, " ")
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
        # Chinese product names often interleave Latin model codes, for example
        # `绿杆2B铅笔`.  A provider may keep or drop either side of that code, and
        # a single SQL substring cannot match `2B书写铅笔`.  Script-boundary
        # segments preserve meaningful terms without degrading a specific
        # nonexistent query into an unrestricted catalogue listing.
        for token in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+", cleaned):
            if len(token) >= 2 and token not in {"商品", "在售", "当前", "平台"}:
                candidates.append(token[:120])
    broad = (
        any(marker in raw for marker in ("全部商品", "所有商品", "当前在售", "平台在售", "全平台"))
        and cleaned in generic_terms
    )
    if broad or cleaned in generic_terms:
        candidates.append(None)
    result: list[str | None] = []
    for candidate in candidates or [raw]:
        if candidate not in result:
            result.append(candidate)
    return result[:6]


def _combined_catalog_search_candidates(
    query: str | None, fallback_query: str | None
) -> list[str | None]:
    result: list[str | None] = []
    for raw_source in (query, fallback_query):
        for candidate in _catalog_search_candidates(raw_source):
            if candidate not in result:
                result.append(candidate)
    return result[:12]


@dataclass(frozen=True)
class CatalogSearchConstraints:
    candidates: tuple[str | None, ...]
    keywords: tuple[str, ...]
    semantic_keywords: tuple[str, ...]
    price_min: int | None
    price_max: int | None
    requested_limit: int
    sort: str


_PRICE_NUMBER = r"(\d+(?:\.\d{1,2})?)"


def _catalog_search_constraints(
    query: str | None, fallback_query: str | None
) -> CatalogSearchConstraints:
    """Build enforceable catalogue filters from model and original user text.

    The model is allowed to help select search words, but it is not allowed to
    silently drop hard price limits or turn a specific request into an
    unrestricted catalogue listing. The original user message is therefore the
    source of truth for numeric constraints and whether a request is specific.
    """

    original = re.sub(r"\s+", " ", (fallback_query or query or "").strip())[:240]
    price_min = _extract_price_bound(original, lower=True)
    price_max = _extract_price_bound(original, lower=False)
    keyword_source = _strip_catalog_request_syntax(original)
    keywords = _catalog_keywords(keyword_source)
    semantic_keywords = _semantic_catalog_expansions(original)

    candidates: list[str | None] = []
    # Prefer the server-cleaned request. Model output and the raw sentence are
    # fallbacks only; otherwise filler such as "几件" can accidentally become
    # the first SQL term with one incidental match and truncate better results.
    for source in (keyword_source,):
        for candidate in _catalog_search_candidates(source):
            if candidate is None and keywords:
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    for keyword in semantic_keywords:
        if keyword not in candidates:
            candidates.append(keyword)
    optional_sources: tuple[str | None, str | None] = (query, fallback_query)
    for raw_source in optional_sources:
        for candidate in _catalog_search_candidates(raw_source):
            if candidate is None and keywords:
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    for keyword in keywords:
        if keyword not in candidates:
            candidates.append(keyword)

    # A price-only request may search all public products, but a textual request
    # may never fall back to all products merely because no exact match exists.
    if not candidates:
        candidates = (
            [None] if price_min is not None or price_max is not None else [original or None]
        )
    return CatalogSearchConstraints(
        candidates=tuple(candidates[:12]),
        keywords=keywords,
        semantic_keywords=semantic_keywords,
        price_min=price_min,
        price_max=price_max,
        requested_limit=_extract_requested_count(original),
        sort=_extract_catalog_sort(original),
    )


def _extract_catalog_sort(text: str) -> str:
    compact = re.sub(r"\s+", "", text).casefold()
    if any(marker in compact for marker in ("价格从低到高", "价格升序", "便宜到贵", "低价优先")):
        return "price_asc"
    if any(marker in compact for marker in ("价格从高到低", "价格降序", "贵到便宜", "高价优先")):
        return "price_desc"
    if any(marker in compact for marker in ("最新上架", "最新优先", "按最新", "最新的")):
        return "newest"
    if any(marker in compact for marker in ("销量排序", "销量优先", "卖得最好", "最畅销")):
        return "sales"
    return "sales"


def _semantic_catalog_expansions(text: str) -> tuple[str, ...]:
    """Expand bounded shopping purposes into auditable catalogue terms."""

    normalized = re.sub(r"\s+", "", text).casefold()
    result: list[str] = []
    if any(term in normalized for term in ("考试", "考研", "答题", "绘图")):
        result.extend(("铅笔", "橡皮", "直尺", "笔芯", "笔记本"))
    if any(term in normalized for term in ("办公", "上班", "会议")):
        result.extend(("笔", "笔记本", "记录本", "直尺"))
    return tuple(dict.fromkeys(result))


def _extract_requested_count(text: str) -> int:
    chinese_numbers = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5}
    match = re.search(r"(?:推荐|找|看看|列出)?\s*([一两二三四五\d]+)\s*(?:件|个|款)", text)
    if match is None:
        return 5
    raw = match.group(1)
    try:
        value = int(raw) if raw.isdigit() else chinese_numbers.get(raw, 5)
    except ValueError:
        value = 5
    return max(1, min(value, 8))


def _filter_order_rows(
    rows: list[tuple[Order, Store]],
    items_by_order: dict[int, list[OrderItem]],
    query: str | None,
) -> list[tuple[Order, Store]]:
    """Narrow natural-language order lists when a store or product is named."""

    normalized = re.sub(r"\s+", "", query or "").casefold()
    if not normalized:
        return rows
    product_query = re.sub(
        r"(?:订单|刚刚|刚才|我的|这个|那个|状态|买的|购买的|查一下|看看)",
        "",
        normalized,
    )
    matches: list[tuple[Order, Store]] = []
    for order, store in rows:
        store_name = re.sub(r"\s+", "", store.store_name).casefold()
        store_stem = re.sub(r"(?:官方)?(?:旗舰店|专卖店|店铺|商店)$", "", store_name)
        store_match = bool(
            store_name in normalized or (len(store_stem) >= 2 and store_stem in normalized)
        )
        product_match = any(
            _meaningful_product_reference(item.product_name, product_query)
            for item in items_by_order.get(order.id, [])
        )
        if store_match or product_match:
            matches.append((order, store))
    return matches or rows


def _meaningful_product_reference(product_name: str, normalized_query: str) -> bool:
    product = re.sub(r"\s+", "", product_name).casefold()
    if product in normalized_query:
        return True
    for token in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{2,}", normalized_query):
        if token in {"订单", "刚刚", "刚才", "我的", "这个", "那个", "状态", "买的"}:
            continue
        if token in product:
            return True
    return False


def _extract_price_bound(text: str, *, lower: bool) -> int | None:
    if lower:
        patterns = (
            rf"{_PRICE_NUMBER}\s*元\s*(?:以上|起|起步|不少于|不低于)",
            rf"(?:至少|最低|不低于)\s*{_PRICE_NUMBER}\s*元?",
        )
    else:
        patterns = (
            rf"{_PRICE_NUMBER}\s*元\s*(?:以内|以下|内|封顶|不超过|最多)",
            rf"(?:不超过|最多|最高|预算(?:是|为|在)?|控制在)\s*{_PRICE_NUMBER}\s*元?",
        )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is None:
            continue
        try:
            return int(Decimal(match.group(1)) * 100)
        except (InvalidOperation, ValueError):
            return None
    return None


def _strip_catalog_request_syntax(text: str) -> str:
    cleaned = re.sub(
        rf"{_PRICE_NUMBER}\s*元\s*(?:以内|以下|以上|内|起|起步|封顶|不超过|不少于|不低于|最多)?",
        " ",
        text,
    )
    cleaned = re.sub(
        rf"(?:不超过|最多|最高|最低|至少|预算(?:是|为|在)?|控制在)\s*{_PRICE_NUMBER}\s*元?",
        " ",
        cleaned,
    )
    cleaned = re.sub(
        r"(?:按)?价格(?:从低到高|从高到低|升序|降序)|便宜到贵|贵到便宜|低价优先|高价优先|"
        r"最新上架|最新优先|按最新|销量排序|销量优先|卖得最好|最畅销",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"[一两二三四五\d]+\s*(?:件|个|款)", " ", cleaned)
    cleaned = re.sub(
        r"(?:麻烦|请|帮我|给我|我想|想要|看看|一下|全平台|当前|在售|搜索|查找|找找|找|推荐)",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"(?:几件|几款|几个|一些|一批)", " ", cleaned)
    cleaned = re.sub(r"(?:适合|用于|用来|使用|能用来|可以买来|的)", " ", cleaned)
    cleaned = re.sub(r"[\u3001\u3002\uff0c\uff01\uff1f\uff1a\uff1b,:;!?]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _catalog_keywords(text: str) -> tuple[str, ...]:
    generic = {"", "商品", "东西", "一些", "一款", "几款", "看看"}
    result: list[str] = []
    for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text):
        if len(token) < 2 or token in generic:
            continue
        result.append(token[:120])
    return tuple(dict.fromkeys(result))[:8]


def _not_accessible() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="AGENT_RESOURCE_NOT_ACCESSIBLE",
        title="Resource not accessible",
        detail="请求的资源不存在或不可访问。",
    )
