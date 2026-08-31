from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.modules.agent_runtime.handoff_intent import is_explicit_handoff_request
from app.modules.agent_runtime.models import AgentToolAudit
from app.modules.agent_runtime.store_context import TrustedStoreAgentContext
from app.modules.catalog.models import Product, ProductAttribute, ProductSku
from app.modules.catalog.repository import CatalogRepository
from app.modules.inventory.models import Inventory
from app.modules.logistics.models import Shipment, ShipmentTrack
from app.modules.messaging.human_schemas import HumanHandoffRequest
from app.modules.messaging.models import HumanServiceTicket
from app.modules.messaging.service import MessagingService
from app.modules.orders.domain import OrderPolicySnapshot, available_action_codes
from app.modules.orders.models import Order, OrderItem
from app.modules.stores.repository import StoreRepository

ToolStatus = Literal["succeeded", "denied", "failed", "unknown"]


@dataclass(frozen=True)
class StoreToolResult:
    status: ToolStatus
    data: dict[str, object]
    error_code: str | None = None


class StoreToolGateway:
    """Phase-7 in-process adapter with the same closed contract later exposed through MCP."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.catalog = CatalogRepository(session)
        self.stores = StoreRepository(session)
        self._call_counts: dict[str, int] = {}

    async def execute(
        self,
        context: TrustedStoreAgentContext,
        tool_code: str,
        arguments: dict[str, object],
        handler: Callable[[], Awaitable[dict[str, object]]],
    ) -> StoreToolResult:
        started = time.monotonic()
        payload_hash = hashlib.sha256(
            json.dumps(
                arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).digest()
        result: StoreToolResult
        policy = context.tool_policies.get(tool_code)
        call_count = self._call_counts.get(tool_code, 0)
        if tool_code not in context.allowed_tools or policy is None:
            result = StoreToolResult("denied", {}, "TOOL_NOT_ALLOWED")
        elif call_count >= policy.call_budget:
            result = StoreToolResult("denied", {}, "TOOL_CALL_BUDGET_EXCEEDED")
        elif policy.confirmation_policy != "none" and not (
            tool_code == "support.create_store_ticket"
            and is_explicit_handoff_request(context.trigger.text_content or "")
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
                result = StoreToolResult("failed", {}, "TOOL_EXECUTION_FAILED")
        self.session.add(
            AgentToolAudit(
                audit_no=new_prefixed_ulid("taud_"),
                run_id=context.run.id,
                tool_code=tool_code,
                scope_snapshot=context.trusted_scope,
                arguments_hash=payload_hash,
                outcome=result.status,
                error_code=result.error_code,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        )
        await self.session.flush()
        return result

    async def product(self, context: TrustedStoreAgentContext, product_no: str) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            row = await self.catalog.public_product(product_no)
            if row is None or row[0].store_id != context.store.id:
                raise _not_accessible()
            product, _store = row
            attributes = await self.catalog.product_attributes(product.id)
            skus = list(
                (
                    await self.session.scalars(
                        select(ProductSku)
                        .where(
                            ProductSku.product_id == product.id,
                            ProductSku.store_id == context.store.id,
                            ProductSku.sku_status == "active",
                        )
                        .order_by(ProductSku.id)
                    )
                ).all()
            )
            content = await self.catalog.published_content(product)
            faqs = await self.catalog.public_faqs(product.id)
            fulfillment = await self.catalog.fulfillment_profile(product.id)
            now = utc_now()
            return {
                "product_id": product.product_no,
                "name": product.product_name,
                "subtitle": product.subtitle,
                "description": product.description,
                "price": {
                    "min_amount": product.min_price_amount,
                    "max_amount": product.max_price_amount,
                    "currency": product.currency,
                },
                "attributes": [_attribute(item) for item in attributes],
                "skus": [
                    {
                        "sku_id": sku.sku_no,
                        "sku_name": sku.sku_name,
                        "specifications": sku.spec_values,
                    }
                    for sku in skus[:20]
                ],
                "safe_detail_text": content.safe_text[:6000] if content else None,
                "faqs": [
                    {
                        "faq_id": faq.faq_no,
                        "question": faq.question,
                        "answer": version.safe_text[:2000],
                        "source_version": version.faq_version_no,
                    }
                    for faq, version in faqs[:10]
                ],
                "dispatch_estimate": {
                    "status": "available" if fulfillment else "unavailable",
                    "min_at": (
                        now + timedelta(hours=fulfillment.dispatch_min_hours)
                        if fulfillment
                        else None
                    ),
                    "max_at": (
                        now + timedelta(hours=fulfillment.dispatch_max_hours)
                        if fulfillment
                        else None
                    ),
                    "source": "store_fulfillment_profile" if fulfillment else None,
                    "source_updated_at": fulfillment.updated_at if fulfillment else None,
                    "as_of": now,
                    "disclaimer_code": "ESTIMATE_NOT_GUARANTEE" if fulfillment else None,
                },
                "source_version": product.version,
                "as_of": now,
                "data_scope": {"store_id": context.store.store_no},
            }

        return await self.execute(
            context, "catalog.get_product", {"product_id": product_no}, handler
        )

    async def resolve_product(
        self, context: TrustedStoreAgentContext, query: str
    ) -> StoreToolResult:
        """Resolve an explicitly named product without trusting stale page context.

        Natural questions often name another product while the conversation still
        carries an older product/order card.  Resolution is bounded to active
        products in the current store and only succeeds for a unique fuzzy match.
        """

        async def handler() -> dict[str, object]:
            products = list(
                (
                    await self.session.scalars(
                        select(Product)
                        .where(
                            Product.store_id == context.store.id,
                            Product.product_status == "on_sale",
                        )
                        .order_by(Product.id)
                        .limit(100)
                    )
                ).all()
            )
            product_ids = [item.id for item in products]
            skus = (
                list(
                    (
                        await self.session.scalars(
                            select(ProductSku)
                            .where(
                                ProductSku.product_id.in_(product_ids),
                                ProductSku.store_id == context.store.id,
                                ProductSku.sku_status == "active",
                            )
                            .order_by(ProductSku.id)
                        )
                    ).all()
                )
                if product_ids
                else []
            )
            aliases_by_product: dict[int, list[str]] = {}
            for sku in skus:
                aliases_by_product.setdefault(sku.product_id, []).append(sku.sku_name)
            ranked = sorted(
                (
                    (
                        _product_match_score(
                            query,
                            item.product_name,
                            aliases_by_product.get(item.id, []),
                        ),
                        item,
                    )
                    for item in products
                ),
                key=lambda pair: (pair[0], -pair[1].id),
                reverse=True,
            )
            if not ranked or ranked[0][0] < 2:
                return {"product_id": None, "matched_name": None, "match_score": 0}
            if len(ranked) > 1 and ranked[1][0] == ranked[0][0]:
                return {"product_id": None, "matched_name": None, "match_score": ranked[0][0]}
            score, product = ranked[0]
            return {
                "product_id": product.product_no,
                "matched_name": product.product_name,
                "match_score": score,
            }

        return await self.execute(
            context,
            "catalog.search_store_products",
            {"query": query[:120]},
            handler,
        )

    async def compare_skus(
        self,
        context: TrustedStoreAgentContext,
        product_no: str,
        sku_nos: list[str] | None = None,
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            product = await self.session.scalar(
                select(Product).where(
                    Product.product_no == product_no,
                    Product.store_id == context.store.id,
                    Product.product_status == "on_sale",
                )
            )
            if product is None:
                raise _not_accessible()
            statement = select(ProductSku).where(
                ProductSku.product_id == product.id,
                ProductSku.store_id == context.store.id,
                ProductSku.sku_status == "active",
            )
            if sku_nos:
                unique = list(dict.fromkeys(sku_nos))
                if not 2 <= len(unique) <= 4:
                    raise _invalid_arguments("SKU 对比必须选择 2 至 4 个规格。")
                statement = statement.where(ProductSku.sku_no.in_(unique))
            rows = list((await self.session.scalars(statement.order_by(ProductSku.id))).all())
            rows = rows[:4] if not sku_nos else rows
            if len(rows) < 2 or (sku_nos and len(rows) != len(set(sku_nos))):
                raise _not_accessible()
            return {
                "product_id": product.product_no,
                "items": [_sku(item) for item in rows],
                "as_of": utc_now(),
                "source_version": product.version,
                "data_scope": {"store_id": context.store.store_no},
            }

        return await self.execute(
            context,
            "catalog.compare_skus",
            {"product_id": product_no, "sku_ids": sku_nos or []},
            handler,
        )

    async def inventory(
        self, context: TrustedStoreAgentContext, product_no: str
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            rows = list(
                (
                    await self.session.execute(
                        select(ProductSku, Inventory, Product.product_name)
                        .join(Product, Product.id == ProductSku.product_id)
                        .outerjoin(Inventory, Inventory.sku_id == ProductSku.id)
                        .where(
                            Product.product_no == product_no,
                            Product.store_id == context.store.id,
                            Product.product_status == "on_sale",
                            ProductSku.store_id == context.store.id,
                            ProductSku.sku_status == "active",
                        )
                        .order_by(ProductSku.id)
                    )
                ).all()
            )
            if not rows:
                raise _not_accessible()
            return {
                "product_id": product_no,
                "product_name": rows[0][2],
                "items": [
                    {
                        "sku_id": sku.sku_no,
                        "sku_name": sku.sku_name,
                        "availability": _availability(inventory),
                        "availability_label": _availability_label(inventory),
                        "available_quantity": _available_quantity(inventory),
                        "price": _money_projection(sku.sale_price_amount, sku.currency),
                        "as_of": inventory.updated_at if inventory else utc_now(),
                    }
                    for sku, inventory, _product_name in rows
                ],
                "data_scope": {"store_id": context.store.store_no},
            }

        return await self.execute(
            context,
            "catalog.get_inventory_availability",
            {"product_id": product_no},
            handler,
        )

    async def compare_products(
        self,
        context: TrustedStoreAgentContext,
        product_nos: list[str],
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            unique = list(dict.fromkeys(product_nos))
            if not 2 <= len(unique) <= 4:
                raise _invalid_arguments("商品对比必须选择 2 至 4 个商品。")
            products = list(
                (
                    await self.session.scalars(
                        select(Product)
                        .where(
                            Product.product_no.in_(unique),
                            Product.store_id == context.store.id,
                            Product.product_status == "on_sale",
                        )
                        .order_by(Product.id)
                    )
                ).all()
            )
            if len(products) != len(unique):
                raise _not_accessible()
            items: list[dict[str, object]] = []
            for product in products:
                attributes = await self.catalog.product_attributes(product.id)
                items.append(
                    {
                        "product_id": product.product_no,
                        "name": product.product_name,
                        "subtitle": product.subtitle,
                        "price": {
                            "min_amount": product.min_price_amount,
                            "max_amount": product.max_price_amount,
                            "currency": product.currency,
                        },
                        "attributes": [_attribute(item) for item in attributes],
                        "source_version": product.version,
                    }
                )
            return {
                "items": items,
                "as_of": utc_now(),
                "data_scope": {"store_id": context.store.store_no},
            }

        return await self.execute(
            context,
            "catalog.compare_products",
            {"product_ids": product_nos},
            handler,
        )

    async def policies(self, context: TrustedStoreAgentContext) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            rows = await self.stores.public_policies(context.store.id)
            return {
                "items": [
                    {
                        "policy_id": item.policy_no,
                        "policy_type": item.policy_type,
                        "title": item.title,
                        "content": item.content[:6000],
                        "effective_at": item.effective_at,
                        "expires_at": item.expires_at,
                        "source_version": item.policy_version,
                    }
                    for item in rows
                ],
                "as_of": utc_now(),
                "data_scope": {"store_id": context.store.store_no},
            }

        return await self.execute(context, "catalog.get_store_policy", {}, handler)

    async def order_summary(
        self, context: TrustedStoreAgentContext, order_no: str
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            order = await self.session.scalar(
                select(Order).where(
                    Order.order_no == order_no,
                    Order.user_id == context.user.id,
                    Order.store_id == context.store.id,
                )
            )
            if order is None:
                raise _not_accessible()
            items = list(
                (
                    await self.session.scalars(
                        select(OrderItem)
                        .where(OrderItem.order_id == order.id)
                        .order_by(OrderItem.id)
                    )
                ).all()
            )
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
                "status": {
                    "order": order.order_status,
                    "payment": order.payment_status,
                    "fulfillment": order.fulfillment_status,
                    "after_sale": order.after_sale_status,
                },
                "amounts": {
                    "payable": order.payable_amount,
                    "paid": order.paid_amount,
                    "refunded": order.refunded_amount,
                    "currency": order.currency,
                },
                "items": [
                    {
                        "order_item_id": item.order_item_no,
                        "product_id": item.product_no,
                        "sku_id": item.sku_no,
                        "product_name": item.product_name,
                        "sku_name": item.sku_name,
                        "quantity": item.quantity,
                        "payable_amount": item.payable_amount,
                        "currency": item.currency,
                    }
                    for item in items
                ],
                "page_target": {"name": "my-order-detail", "order_id": order.order_no},
                "available_actions": available_action_codes(policy, utc_now()),
                "source_version": order.version,
                "as_of": utc_now(),
                "data_scope": {
                    "user_id": context.user.user_no,
                    "store_id": context.store.store_no,
                },
            }

        return await self.execute(
            context, "order.get_store_order_summary", {"order_id": order_no}, handler
        )

    async def recommendations(
        self, context: TrustedStoreAgentContext, search_text: str | None
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            rows, _has_more = await self.catalog.search_products(
                q=None,
                category_no=None,
                brand_no=None,
                store_no=context.store.store_no,
                group_no=None,
                price_min=None,
                price_max=None,
                sort="sales",
                position=None,
                limit=5,
            )
            return {
                "query": search_text,
                "items": [
                    {
                        "product_id": product.product_no,
                        "name": product.product_name,
                        "subtitle": product.subtitle,
                        "price": {
                            "min_amount": product.min_price_amount,
                            "max_amount": product.max_price_amount,
                            "currency": product.currency,
                        },
                        "rating": str(product.rating_score),
                    }
                    for product, _store in rows
                ],
                "as_of": utc_now(),
                "data_scope": {"store_id": context.store.store_no},
            }

        return await self.execute(
            context,
            "catalog.search_store_products",
            {"query": (search_text or "")[:120]},
            handler,
        )

    async def shipments(self, context: TrustedStoreAgentContext, order_no: str) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            order = await self.session.scalar(
                select(Order).where(
                    Order.order_no == order_no,
                    Order.user_id == context.user.id,
                    Order.store_id == context.store.id,
                )
            )
            if order is None:
                raise _not_accessible()
            shipments = list(
                (
                    await self.session.scalars(
                        select(Shipment)
                        .where(
                            Shipment.order_id == order.id,
                            Shipment.store_id == context.store.id,
                            Shipment.shipment_status != "voided",
                        )
                        .order_by(Shipment.id)
                    )
                ).all()
            )
            items: list[dict[str, object]] = []
            for shipment in shipments:
                latest = list(
                    (
                        await self.session.scalars(
                            select(ShipmentTrack)
                            .where(ShipmentTrack.shipment_id == shipment.id)
                            .order_by(ShipmentTrack.occurred_at.desc(), ShipmentTrack.id.desc())
                            .limit(5)
                        )
                    ).all()
                )
                items.append(
                    {
                        "shipment_id": shipment.shipment_no,
                        "carrier_name": shipment.carrier_name,
                        "tracking_no_masked": shipment.tracking_no_masked,
                        "shipment_status": shipment.shipment_status,
                        "estimated_delivery": {
                            "min_at": shipment.estimated_delivery_min_at,
                            "max_at": shipment.estimated_delivery_max_at,
                            "source": shipment.estimate_source,
                            "updated_at": shipment.estimate_updated_at,
                            "disclaimer_code": "ESTIMATE_NOT_GUARANTEE",
                        },
                        "latest_tracks": [
                            {
                                "status": track.track_status,
                                "description": track.description,
                                "location": track.location_text,
                                "occurred_at": track.occurred_at,
                            }
                            for track in latest
                        ],
                    }
                )
            return {
                "order_id": order.order_no,
                "items": items,
                "as_of": utc_now(),
                "data_scope": {
                    "user_id": context.user.user_no,
                    "store_id": context.store.store_no,
                },
            }

        return await self.execute(
            context,
            "logistics.get_store_order_shipments",
            {"order_id": order_no},
            handler,
        )

    async def ticket_status(self, context: TrustedStoreAgentContext) -> StoreToolResult:
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
                ),
                "as_of": utc_now(),
            }

        return await self.execute(context, "support.get_ticket_status", {}, handler)

    async def handoff(
        self,
        context: TrustedStoreAgentContext,
        *,
        ticket_type: Literal["general", "order", "logistics", "refund", "complaint"],
        reason_code: str,
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            ticket = await MessagingService(self.session).request_human_from_agent(
                context.user,
                context.conversation.conversation_no,
                HumanHandoffRequest(
                    ticket_type=ticket_type,
                    summary=f"店铺客服转人工: {reason_code}",
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
            "support.create_store_ticket",
            {"ticket_type": ticket_type, "reason_code": reason_code},
            handler,
        )


def _attribute(item: ProductAttribute) -> dict[str, object]:
    return {
        "code": item.attribute_code,
        "name": item.attribute_name,
        "value": item.value_text,
        "unit": item.unit,
    }


def _sku(item: ProductSku) -> dict[str, object]:
    return {
        "sku_id": item.sku_no,
        "name": item.sku_name,
        "specifications": item.spec_values,
        "sale_price_amount": item.sale_price_amount,
        "currency": item.currency,
        "availability": "available",
    }


def _availability(item: Inventory | None) -> str:
    if item is None or item.inventory_status != "active":
        return "unknown"
    available = item.on_hand_quantity - item.reserved_quantity - item.safety_stock_quantity
    if available <= 0:
        return "out_of_stock"
    if available <= 5:
        return "low_stock"
    return "in_stock"


def _available_quantity(item: Inventory | None) -> int:
    if item is None or item.inventory_status != "active":
        return 0
    return max(
        0,
        item.on_hand_quantity - item.reserved_quantity - item.safety_stock_quantity,
    )


def _availability_label(item: Inventory | None) -> str:
    return {
        "unknown": "库存暂不可用",
        "out_of_stock": "缺货",
        "low_stock": "库存紧张",
        "in_stock": "有货",
    }[_availability(item)]


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


def _product_match_score(
    query: str,
    product_name: str,
    aliases: list[str] | None = None,
) -> int:
    def compact(value: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())

    def bigrams(value: str) -> set[str]:
        compacted = compact(value)
        return {compacted[index : index + 2] for index in range(max(0, len(compacted) - 1))}

    query_compact = compact(query)
    labels = [product_name, *(aliases or [])]
    score = len(bigrams(query) & set().union(*(bigrams(item) for item in labels)))
    for alias in aliases or []:
        normalized_alias = compact(alias)
        if len(normalized_alias) >= 2 and normalized_alias in query_compact:
            score += max(3, min(8, len(normalized_alias)))
    return score


_SCOPE_KEYS = frozenset({"userid", "userno", "storeid", "storeno", "conversationid", "contextid"})


def _contains_scope_override(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold().replace("_", "").replace("-", "") in _SCOPE_KEYS
            or _contains_scope_override(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_scope_override(item) for item in value)
    return False


def _not_accessible() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="AGENT_RESOURCE_NOT_ACCESSIBLE",
        title="Resource not accessible",
        detail="请求的资源不存在或不可访问。",
    )


def _invalid_arguments(detail: str) -> ApplicationError:
    return ApplicationError(
        status=422,
        code="AGENT_TOOL_ARGUMENTS_INVALID",
        title="Invalid tool arguments",
        detail=detail,
    )
