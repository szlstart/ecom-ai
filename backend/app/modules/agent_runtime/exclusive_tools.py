from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
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
from app.modules.agent_runtime.public_trace import audit_projection, result_count
from app.modules.agent_runtime.store_tools import (
    StoreToolResult,
    _availability_label,
    _available_quantity,
    _combined_detail_text,
    _contains_scope_override,
)
from app.modules.cart.schemas import CartItemCreateRequest, CartItemPatchRequest
from app.modules.cart.service import CartService
from app.modules.catalog.models import Product, ProductSku
from app.modules.catalog.repository import CatalogRepository, described_image_file_ids
from app.modules.catalog.service import CatalogService
from app.modules.checkout.schemas import CartSource, CheckoutCreateRequest
from app.modules.checkout.service import CheckoutService
from app.modules.finance.models import UserWallet, WalletTransaction
from app.modules.identity.models import UserAddress, UserCredential
from app.modules.inventory.models import Inventory
from app.modules.logistics.service import LogisticsService
from app.modules.messaging.human_schemas import HumanHandoffRequest
from app.modules.messaging.models import HumanServiceTicket
from app.modules.messaging.service import MessagingService
from app.modules.orders.domain import OrderPolicySnapshot, available_action_codes
from app.modules.orders.models import Order, OrderItem
from app.modules.payments.models import Payment
from app.modules.stores.models import Store
from app.modules.stores.repository import StoreRepository
from app.modules.stores.service import StoreService

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
        self.stores = StoreRepository(session)
        self.after_sale = AfterSaleService(session, settings, security)
        self._call_counts: dict[str, int] = {}
        self.execution_records: list[dict[str, object]] = []

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
            or (
                tool_code == "cart.clear.commit"
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
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        self.execution_records.append(
            {
                "sequence": len(self.execution_records) + 1,
                "tool_code": tool_code,
                "arguments": audit_projection(arguments),
                "status": result.status,
                "result": audit_projection(result.data),
                "result_count": result_count(result.data),
                "error_code": result.error_code,
                "latency_ms": latency_ms,
            }
        )
        self.session.add(
            AgentToolAudit(
                audit_no=new_prefixed_ulid("taud_"),
                run_id=context.run.id,
                tool_code=tool_code,
                scope_snapshot=context.trusted_scope,
                arguments_hash=arguments_hash,
                outcome=result.status,
                error_code=result.error_code,
                latency_ms=latency_ms,
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
            # Only the shopper's current request may create a hard colour
            # constraint.  ``query`` can contain confirmed long-term memory
            # appended by the Agent as a soft ranking hint; treating that hint
            # as an explicit filter can erase an otherwise exact named-product
            # result (for example a remembered preference for dark blue while
            # the shopper asks to recommend a specific keyboard).
            requested_colors = _explicit_catalog_colors(fallback_query or query or "")
            preference_text = " ".join(value for value in (query, fallback_query) if value)
            preferred_colors = _preferred_catalog_colors(preference_text)
            requested_seasons = _requested_catalog_seasons(preference_text)
            requested_weight = _requested_catalog_weight(preference_text)
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
                if (
                    len(rows) >= constraints.requested_limit
                    and not requested_colors
                    and not preferred_colors
                    and not requested_seasons
                    and requested_weight is None
                ):
                    break
            # Hard product-kind constraints come from the shopper's natural
            # request, not from an exact product name recovered from a recent
            # card.  Otherwise a follow-up on an eraser whose official name
            # contains ``铅笔擦`` is incorrectly treated as a fresh pencil
            # search and the focused product is filtered out.
            kind_request_text = fallback_query or query or ""
            rows = [
                row
                for row in rows
                if _matches_explicit_catalog_kind(row[0].product_name, kind_request_text)
            ]
            excluded_terms = _excluded_catalog_terms(preference_text)
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
            if excluded_terms:
                rows = [
                    (product, store)
                    for product, store in rows
                    if not any(
                        term
                        in " ".join(
                            [
                                str(product.product_name or ""),
                                str(product.subtitle or ""),
                                *[
                                    str(sku.get("sku_name") or "")
                                    for sku in skus_by_product.get(product.id, [])
                                ],
                            ]
                        ).casefold()
                        for term in excluded_terms
                    )
                ]
            if requested_colors:
                rows = [
                    (product, store)
                    for product, store in rows
                    if any(
                        color
                        in " ".join(
                            [
                                str(product.product_name or ""),
                                str(product.subtitle or ""),
                                *[
                                    str(sku.get("sku_name") or "")
                                    for sku in skus_by_product.get(product.id, [])
                                ],
                            ]
                        ).casefold()
                        for color in requested_colors
                    )
                ][: constraints.requested_limit]
            if requested_seasons:
                rows = [
                    (product, store)
                    for product, store in rows
                    if _matches_requested_catalog_seasons(
                        " ".join(
                            [
                                str(product.product_name or ""),
                                str(product.subtitle or ""),
                                *[
                                    str(sku.get("sku_name") or "")
                                    for sku in skus_by_product.get(product.id, [])
                                ],
                            ]
                        ),
                        requested_seasons,
                    )
                ]
            if requested_weight is not None:
                rows = [
                    (product, store)
                    for product, store in rows
                    if any(
                        (_catalog_sku_weight_limit(str(sku.get("sku_name") or "")) or 0)
                        >= requested_weight
                        for sku in skus_by_product.get(product.id, [])
                    )
                ]
            if preferred_colors:
                rows.sort(
                    key=lambda row: _catalog_color_preference_score(
                        " ".join(
                            [
                                str(row[0].product_name or ""),
                                str(row[0].subtitle or ""),
                                *[
                                    str(sku.get("sku_name") or "")
                                    for sku in skus_by_product.get(row[0].id, [])
                                ],
                            ]
                        ),
                        preferred_colors,
                    ),
                    reverse=True,
                )
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
                    "colors": list(requested_colors),
                    "preferred_colors": list(preferred_colors),
                    "seasons": list(requested_seasons),
                    "weight_jin": requested_weight,
                    "excluded_terms": list(excluded_terms),
                },
                "as_of": utc_now().isoformat(),
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

    async def list_addresses(
        self,
        context: TrustedExclusiveAgentContext,
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            rows = list(
                (
                    await self.session.scalars(
                        select(UserAddress)
                        .where(
                            UserAddress.user_id == context.user.id,
                            UserAddress.deleted_at.is_(None),
                        )
                        .order_by(UserAddress.is_default.desc(), UserAddress.id.desc())
                    )
                ).all()
            )
            return {
                "items": [
                    {
                        "address_id": item.address_no,
                        "recipient_name": self.security.decrypt(
                            "address-recipient", item.recipient_name_ciphertext
                        ),
                        "phone": self.security.decrypt("address-phone", item.phone_ciphertext),
                        "country_code": item.country_code,
                        "province_code": item.province_code,
                        "city_code": item.city_code,
                        "district_code": item.district_code,
                        "address": self.security.decrypt(
                            "address-detail", item.address_ciphertext
                        ),
                        "is_default": item.is_default,
                        "version": item.version,
                    }
                    for item in rows
                ],
                "active_count": len(rows),
            }

        return await self.execute(context, "address.list_mine", {}, handler)

    async def get_wallet(self, context: TrustedExclusiveAgentContext) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            wallet = await self.session.scalar(
                select(UserWallet).where(
                    UserWallet.user_id == context.user.id,
                    UserWallet.currency == "CNY",
                )
            )
            transactions = (
                list(
                    (
                        await self.session.scalars(
                            select(WalletTransaction)
                            .where(WalletTransaction.wallet_id == wallet.id)
                            .order_by(
                                WalletTransaction.occurred_at.desc(),
                                WalletTransaction.id.desc(),
                            )
                            .limit(10)
                        )
                    ).all()
                )
                if wallet is not None
                else []
            )
            return {
                "balance": _money_projection(wallet.balance_amount if wallet else 0, "CNY"),
                "wallet_status": wallet.wallet_status if wallet else "active",
                "source_version": wallet.version if wallet else 0,
                "transactions": [
                    {
                        "transaction_type": item.transaction_type,
                        "direction": item.direction,
                        "amount": _money_projection(item.amount, item.currency),
                        "balance_after": _money_projection(item.balance_after, item.currency),
                        "channel": item.channel,
                        "description": item.description,
                        "occurred_at": item.occurred_at.isoformat(),
                    }
                    for item in transactions
                ],
            }

        return await self.execute(context, "account.wallet.get_mine", {}, handler)

    async def get_profile(self, context: TrustedExclusiveAgentContext) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            email_credential = await self.session.scalar(
                select(UserCredential)
                .where(
                    UserCredential.user_id == context.user.id,
                    UserCredential.credential_type == "email",
                    UserCredential.credential_status == "active",
                )
                .order_by(UserCredential.is_primary.desc(), UserCredential.id.desc())
                .limit(1)
            )
            email = None
            if (
                email_credential is not None
                and email_credential.identifier_ciphertext is not None
            ):
                email = self.security.decrypt(
                    "user-credential:email", email_credential.identifier_ciphertext
                )
            return {
                "profile": {
                    "username": context.user.username,
                    "nickname": context.user.nickname,
                    "email": email,
                    "locale": context.user.locale,
                    "timezone": context.user.timezone,
                },
                "as_of": utc_now().isoformat(),
            }

        return await self.execute(context, "account.profile.get_mine", {}, handler)

    async def list_favorites(self, context: TrustedExclusiveAgentContext) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            # A SQLAlchemy AsyncSession must not execute two statements
            # concurrently.  Keep the two user-scoped reads sequential while
            # still presenting them as one account-assets result.
            product_rows = await self.catalog.favorite_products(context.user.id, 8)
            store_rows = await self.stores.followed_stores(context.user.id, 8)
            return {
                "items": [
                    {
                        "product_id": product.product_no,
                        "store_id": store.store_no,
                        "store_name": store.store_name,
                        "name": product.product_name,
                        "price": {
                            "min_amount": product.min_price_amount,
                            "max_amount": product.max_price_amount,
                            "currency": product.currency,
                        },
                        "source_version": product.version,
                    }
                    for product, store in product_rows
                ],
                "followed_stores": [
                    {
                        "store_id": store.store_no,
                        "store_name": store.store_name,
                        "store_status": store.store_status,
                        "rating": str(store.rating_score),
                        "source_version": store.version,
                    }
                    for store in store_rows
                ],
                "favorite_product_count": len(product_rows),
                "followed_store_count": len(store_rows),
            }

        # Both reads belong to one account-assets intent and share the same trusted
        # user scope.  The public tool contract intentionally returns both lists so
        # a compound sentence cannot silently drop either kind of favorite.
        return await self.execute(
            context,
            "account.favorites.list_mine",
            {},
            handler,
        )

    async def set_product_favorite(
        self,
        context: TrustedExclusiveAgentContext,
        product_no: str,
        *,
        enabled: bool,
    ) -> StoreToolResult:
        tool_code = "favorite.add_product" if enabled else "favorite.remove_product"

        async def handler() -> dict[str, object]:
            await CatalogService(self.session, self.settings).set_favorite(
                context.user.id,
                product_no,
                enabled,
            )
            return {
                "product_id": product_no,
                "is_favorited": enabled,
                "changed": True,
            }

        return await self.execute(
            context,
            tool_code,
            {"product_id": product_no},
            handler,
        )

    async def set_store_favorite(
        self,
        context: TrustedExclusiveAgentContext,
        store_no: str,
        *,
        enabled: bool,
    ) -> StoreToolResult:
        tool_code = "favorite.add_store" if enabled else "favorite.remove_store"

        async def handler() -> dict[str, object]:
            await StoreService(self.session, self.settings).set_follow(
                context.user.id,
                store_no,
                enabled,
            )
            return {
                "store_id": store_no,
                "is_favorited": enabled,
                "changed": True,
            }

        return await self.execute(
            context,
            tool_code,
            {"target_store_public_id": store_no},
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
                "as_of": utc_now().isoformat(),
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
            filtered = _filter_order_rows(rows, items_by_order, query)
            payment_rows = (
                list(
                    (
                        await self.session.scalars(
                            select(Payment)
                            .where(
                                Payment.user_id == context.user.id,
                                Payment.trade_order_id.in_(
                                    [order.trade_order_id for order, _store in rows]
                                ),
                            )
                            .order_by(Payment.created_at.desc(), Payment.id.desc())
                        )
                    ).all()
                )
                if rows
                else []
            )
            payment_by_trade: dict[int, Payment] = {}
            for payment in payment_rows:
                payment_by_trade.setdefault(payment.trade_order_id, payment)
            state_counts = _requested_order_state_counts(filtered, items_by_order, query)
            per_state_limit = _requested_order_per_state_limit(query)
            if per_state_limit is not None and _requests_order_state_overview(query):
                filtered = _limit_orders_per_requested_state(
                    filtered,
                    items_by_order,
                    query,
                    per_state_limit,
                )
            result_limit = (
                20
                if _requests_order_spend_summary_query(query)
                else 8
                if _requests_order_state_overview(query)
                else _requested_order_limit(query)
            )
            rows = filtered[:result_limit]
            return {
                "items": [
                    self._order_projection(
                        order,
                        store,
                        items_by_order.get(order.id, []),
                        payment_by_trade.get(order.trade_order_id),
                    )
                    for order, store in rows
                ],
                "requested_state_counts": state_counts,
                "as_of": utc_now().isoformat(),
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

    async def create_cart_checkout_preview(
        self, context: TrustedExclusiveAgentContext
    ) -> StoreToolResult:
        cart = await CartService(self.session).get(context.user)
        cart_data = cart.model_dump(mode="json")
        selected_item_nos = [
            str(item["cart_item_id"])
            for group in cart_data.get("groups", [])
            if isinstance(group, dict)
            for item in group.get("items", [])
            if isinstance(item, dict)
            and item.get("is_selected") is True
            and item.get("is_valid") is True
            and isinstance(item.get("cart_item_id"), str)
        ]
        if not selected_item_nos:
            return StoreToolResult(
                "succeeded",
                {
                    "checkout_unavailable": "NO_VALID_SELECTED_CART_ITEMS",
                    "cart": cart_data,
                },
            )

        async def handler() -> dict[str, object]:
            view = await CheckoutService(self.session).create(
                context.user,
                CheckoutCreateRequest(
                    source=CartSource(
                        source_type="cart",
                        cart_item_ids=selected_item_nos,
                    ),
                    address_id=None,
                ),
                f"agent-checkout-{context.trigger.message_no}",
            )
            return view.model_dump(mode="json")

        return await self.execute(
            context,
            "checkout.create_session",
            {"selected_cart_item_count": len(selected_item_nos)},
            handler,
        )

    async def add_cart_item(
        self,
        context: TrustedExclusiveAgentContext,
        sku_no: str,
        quantity: int,
    ) -> StoreToolResult:
        safe_quantity = min(99, max(1, quantity))

        async def handler() -> dict[str, object]:
            view = await CartService(self.session).add(
                context.user,
                CartItemCreateRequest(sku_id=sku_no, quantity=safe_quantity),
                f"agent-cart-add-{context.trigger.message_no}",
            )
            return view.model_dump(mode="json")

        return await self.execute(
            context,
            "cart.add_item",
            {"sku_id": sku_no, "quantity": safe_quantity},
            handler,
        )

    async def update_cart_quantity(
        self,
        context: TrustedExclusiveAgentContext,
        item_no: str,
        quantity: int,
        cart_version: int,
    ) -> StoreToolResult:
        safe_quantity = min(99, max(1, quantity))

        async def handler() -> dict[str, object]:
            view = await CartService(self.session).patch(
                context.user,
                item_no,
                CartItemPatchRequest(quantity=safe_quantity),
                cart_version,
            )
            return view.model_dump(mode="json")

        return await self.execute(
            context,
            "cart.update_quantity",
            {"cart_item_id": item_no, "quantity": safe_quantity, "version": cart_version},
            handler,
        )

    async def remove_cart_item(
        self,
        context: TrustedExclusiveAgentContext,
        item_no: str,
        cart_version: int,
    ) -> StoreToolResult:
        async def handler() -> dict[str, object]:
            view = await CartService(self.session).delete(
                context.user,
                item_no,
                cart_version,
            )
            return view.model_dump(mode="json")

        return await self.execute(
            context,
            "cart.remove_item",
            {"cart_item_id": item_no, "version": cart_version},
            handler,
        )

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
                content = await self.catalog.published_content(product)
                image_ocr = await self.catalog.content_image_ocr_texts(
                    content.id if content else None,
                    exclude_file_nos=described_image_file_ids(
                        content.safe_blocks if content else None
                    ),
                )
                available = sum(_available_quantity(inventory) for _sku, inventory in sku_rows)
                items.append(
                    {
                        "product_id": product.product_no,
                        "name": product.product_name,
                        "subtitle": product.subtitle,
                        "description": product.description,
                        "safe_detail_text": _combined_detail_text(
                            content.safe_text if content else None, image_ocr
                        ),
                        "image_descriptions": [
                            {
                                "file_id": file_no,
                                "text": text[:2000],
                                "source": "product_detail_ocr",
                            }
                            for file_no, text in image_ocr[:10]
                        ],
                        "store_id": store.store_no,
                        "store_name": store.store_name,
                        "price": {
                            "min_amount": product.min_price_amount,
                            "max_amount": product.max_price_amount,
                            "currency": product.currency,
                        },
                        "available_stock": available,
                        "sku_count": len(sku_rows),
                        "skus": [
                            {
                                "sku_id": sku.sku_no,
                                "sku_name": sku.sku_name,
                                "price": _money_projection(
                                    sku.sale_price_amount, sku.currency
                                ),
                                "available_stock": _available_quantity(inventory),
                            }
                            for sku, inventory in sku_rows
                        ],
                        "rating": str(product.rating_score),
                        "sales_count": product.sales_count,
                        "source_version": product.version,
                    }
                )
            return {"items": items, "as_of": utc_now().isoformat()}

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
            payment = await self.session.scalar(
                select(Payment)
                .where(
                    Payment.user_id == context.user.id,
                    Payment.trade_order_id == order.trade_order_id,
                )
                .order_by(Payment.created_at.desc(), Payment.id.desc())
                .limit(1)
            )
            return self._order_projection(order, store, items, payment)

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

    async def shipments_for_orders(
        self, context: TrustedExclusiveAgentContext, order_nos: list[str]
    ) -> StoreToolResult:
        """Read a bounded set of this user's order shipments in one tool call."""

        scoped_order_nos = list(dict.fromkeys(order_nos))[:5]

        async def handler() -> dict[str, object]:
            service = LogisticsService(
                self.session,
                self.security,
                self.settings.security_hmac_secret.get_secret_value(),
            )
            items: list[dict[str, object]] = []
            for order_no in scoped_order_nos:
                result = await service.list_for_order(context.user, order_no)
                payload = result.model_dump(mode="json")
                for raw_item in payload.get("items", []):
                    if not isinstance(raw_item, dict):
                        continue
                    items.append({"order_id": order_no, **raw_item})
            return {
                "items": items,
                "order_ids": scoped_order_nos,
                "queried_order_count": len(scoped_order_nos),
            }

        return await self.execute(
            context,
            "logistics.get_user_order_shipments",
            {"order_ids": scoped_order_nos},
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
    def _order_projection(
        order: Order,
        store: Store,
        items: list[OrderItem],
        payment: Payment | None = None,
    ) -> dict[str, object]:
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
            "payment": (
                {
                    "method": payment.payment_method,
                    "provider": payment.provider,
                    "status": payment.payment_status,
                    "paid_amount": _money_projection(payment.paid_amount, payment.currency),
                    "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
                }
                if payment is not None
                else None
            ),
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
            "as_of": utc_now().isoformat(),
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
        r"(?:[\uff0c,\uff1b;\u3002]\s*(?:并且?|同时)?(?:请)?(?:列出|展示|显示|告诉我|说明)"
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
    keyword_source = _strip_catalog_request_syntax(_latest_catalog_subject(original))
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
            if (
                keywords
                and candidate is not None
                and not any(
                    keyword.casefold() in candidate.casefold()
                    or candidate.casefold() in keyword.casefold()
                    for keyword in keywords
                )
            ):
                # Once the server has extracted a concrete catalogue subject,
                # provider/raw fallbacks may only refine that subject.  Numeric
                # budget fragments (for example ``500``) and instruction words
                # such as ``帮我找`` must never become independent SQL queries,
                # otherwise a nonexistent product can degrade into unrelated
                # in-budget merchandise.
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


def catalog_query_with_inherited_constraints(
    current_text: str,
    previous_text: str | None,
) -> str:
    """Carry explicit catalogue constraints through a natural correction.

    A follow-up such as ``改要男装，预算和排序不变`` changes the subject but
    intentionally keeps the prior budget, result count and sort.  The dialogue
    is still untrusted: this helper only copies bounded presentation filters;
    the catalogue tool continues to enforce public/on-sale scope itself.
    """

    current = re.sub(r"\s+", " ", current_text.strip())[:240]
    previous = re.sub(r"\s+", " ", (previous_text or "").strip())[:240]
    compact = re.sub(r"\s+", "", current).casefold()
    if not previous or "不变" not in compact:
        return current

    prior = _catalog_search_constraints(None, previous)
    current_constraints = _catalog_search_constraints(None, current)
    additions: list[str] = []
    if current_constraints.price_min is None and prior.price_min is not None:
        additions.append(f"{prior.price_min / 100:g}元以上")
    if current_constraints.price_max is None and prior.price_max is not None:
        additions.append(f"{prior.price_max / 100:g}元以内")
    if not _has_explicit_catalog_sort(current) and _has_explicit_catalog_sort(previous):
        additions.append(
            {
                "price_asc": "价格从低到高",
                "price_desc": "价格从高到低",
                "newest": "最新上架",
                "sales": "销量排序",
            }[prior.sort]
        )
    if _extract_requested_count(current) == 5 and _extract_requested_count(previous) != 5:
        additions.append(f"{prior.requested_limit}件")
    prior_colors = _explicit_catalog_colors(previous)
    if not _explicit_catalog_colors(current) and prior_colors:
        additions.extend(prior_colors)
    # Preserve bounded semantic constraints that are not represented by the
    # price/sort parser.  A correction may explicitly remove one dimension
    # (for example “季节不限”) while keeping audience, weight and colour intent.
    current_compact = re.sub(r"\s+", "", current).casefold()
    previous_compact = re.sub(r"\s+", "", previous).casefold()
    audience_markers = (
        "女装",
        "男装",
        "童装",
        "女鞋",
        "男鞋",
        "文具",
        "办公用品",
    )
    if not any(marker in current_compact for marker in audience_markers):
        additions.extend(
            marker for marker in audience_markers if marker in previous_compact
        )
    if _requested_catalog_weight(current) is None:
        prior_weight = _requested_catalog_weight(previous)
        if prior_weight is not None:
            additions.append(f"适合{prior_weight}斤")
    if not _preferred_catalog_colors(current):
        if "深色" in previous_compact:
            additions.append("深色优先")
        elif "浅色" in previous_compact:
            additions.append("浅色优先")
    explicitly_unbounded_season = any(
        marker in current_compact
        for marker in ("季节不限", "不限季节", "四季均可", "季节放宽")
    )
    if not explicitly_unbounded_season and not _requested_catalog_seasons(current):
        season_labels = {
            "spring": "春季",
            "summer": "夏季",
            "autumn": "秋季",
            "winter": "冬季",
        }
        additions.extend(
            season_labels[season] for season in _requested_catalog_seasons(previous)
        )
    return " ".join([current, *additions]).strip()[:240]


def _latest_catalog_subject(text: str) -> str:
    """Prefer the shopper's last explicit correction over abandoned subjects."""

    value = text
    correction_parts = re.split(r"(?:算了|不对|改口了?|前面说错了)[，,:\uff1a\s]*", value)
    if len(correction_parts) > 1 and correction_parts[-1].strip():
        value = correction_parts[-1]
    replacement = re.search(r"(?:不要了|换一个|换一种).*?(?:改成|改为|换成)\s*(.+)", value)
    if replacement is not None and replacement.group(1).strip():
        value = replacement.group(1)
    return value.strip()


def _excluded_catalog_terms(text: str) -> tuple[str, ...]:
    """Extract explicit shopper exclusions without inventing negative preferences."""

    values: list[str] = []
    for match in re.finditer(
        r"(?:不要|排除|剔除|不看|别推荐|不是)(.{1,16}?)(?=，|,|。|;|预算|价格|排序|$)",
        text,
    ):
        phrase = re.sub(r"(?:的)?(?:商品|文具|款式|品类)$", "", match.group(1).strip())
        for term in re.split(r"(?:或者|或是|和|与|、)", phrase):
            normalized = re.sub(r"\s+", "", term).casefold().strip(" 的了")
            if 1 < len(normalized) <= 12 and normalized not in {"太贵", "修改", "提交"}:
                values.append(normalized)
    return tuple(dict.fromkeys(values))


def _has_explicit_catalog_sort(text: str) -> bool:
    compact = re.sub(r"\s+", "", text).casefold()
    return any(
        marker in compact
        for marker in (
            "价格从低到高",
            "价格升序",
            "便宜到贵",
            "低价优先",
            "最便宜",
            "价格不贵",
            "别太贵",
            "价格从高到低",
            "价格降序",
            "贵到便宜",
            "高价优先",
            "最贵",
            "不便宜",
            "最新上架",
            "最新优先",
            "按最新",
            "销量排序",
            "销量优先",
            "卖得最好",
            "最畅销",
        )
    )


def _extract_catalog_sort(text: str) -> str:
    compact = re.sub(r"\s+", "", text).casefold()
    if any(marker in compact for marker in ("不便宜", "价格高一些", "预算高一些")):
        return "price_desc"
    if any(
        marker in compact
        for marker in (
            "价格从低到高",
            "价格升序",
            "便宜到贵",
            "低价优先",
            "最便宜",
            "价格不贵",
            "别太贵",
        )
    ):
        return "price_asc"
    if any(
        marker in compact
        for marker in ("价格从高到低", "价格降序", "贵到便宜", "高价优先", "最贵")
    ):
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
    # Audience/category phrases are useful broad candidates before hard SKU,
    # season, colour and price filters are applied.  Without these bounded
    # candidates a natural sentence such as “适合130斤的夏季女装” becomes one
    # indivisible SQL substring and returns nothing despite eligible products.
    for marker in (
        "女装",
        "男装",
        "童装",
        "女鞋",
        "男鞋",
        "文具",
        "办公用品",
    ):
        if marker in normalized:
            result.append(marker)
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
    day_offset: int | None = None
    if "前两天" in normalized or "前天" in normalized:
        day_offset = 2
    elif "昨天" in normalized:
        day_offset = 1
    elif "今天" in normalized or "今日" in normalized:
        day_offset = 0
    if day_offset is not None:
        target_day = (utc_now() - timedelta(days=day_offset)).date()
        dated = [(order, store) for order, store in rows if order.created_at.date() == target_day]
        if dated:
            rows = dated
    requested_amounts = _requested_order_amounts(query)
    if requested_amounts:
        amount_matches = [
            (order, store)
            for order, store in rows
            if order.paid_amount in requested_amounts or order.payable_amount in requested_amounts
        ]
        if amount_matches:
            return amount_matches
    requested_states: set[str] = set()
    state_markers = (
        ("pending_payment", ("待付款", "待支付", "未付款")),
        ("pending_shipment", ("待发货", "备货")),
        ("shipped", ("运输中", "已发货", "物流中")),
        ("completed", ("已完成", "已收货")),
        ("pending_review", ("待评价", "未评价")),
        ("after_sale", ("售后中", "退款中", "售后订单")),
    )
    for state, markers in state_markers:
        if any(marker in normalized for marker in markers):
            requested_states.add(state)
    if requested_states:
        state_matches = []
        for order, store in rows:
            item_rows = items_by_order.get(order.id, [])
            matched = (
                order.order_status in requested_states
                or (
                    "pending_review" in requested_states
                    and order.order_status == "completed"
                    and any(item.review_status == "pending" for item in item_rows)
                )
                or (
                    "after_sale" in requested_states
                    and order.after_sale_status != "none"
                )
            )
            if matched:
                state_matches.append((order, store))
        return state_matches
    product_query = re.sub(
        r"(?:订单|刚刚|刚才|我的|这个|那个|状态|买的|购买的|查一下|看看)",
        "",
        normalized,
    )
    store_matches: list[tuple[Order, Store]] = []
    product_matches: list[tuple[Order, Store]] = []
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
        if store_match:
            store_matches.append((order, store))
        if product_match:
            product_matches.append((order, store))
    if store_matches and product_matches:
        product_ids = {order.id for order, _store in product_matches}
        intersection = [row for row in store_matches if row[0].id in product_ids]
        if intersection:
            return intersection
    return product_matches or store_matches or rows


def _requested_order_amounts(query: str | None) -> set[int]:
    amounts: set[int] = set()
    for raw in re.findall(r"(?:¥|￥)?(\d+(?:\.\d{1,2})?)元", query or ""):
        try:
            amounts.add(int(Decimal(raw) * 100))
        except (InvalidOperation, ValueError):
            continue
    return amounts


def _requests_order_state_overview(query: str | None) -> bool:
    normalized = re.sub(r"\s+", "", query or "").casefold()
    if any(
        marker in normalized
        for marker in (
            "按状态",
            "每种状态",
            "各种状态",
            "所有状态",
            "分别看看",
            "分别列出",
            "分别展示",
            "分别找",
        )
    ):
        return True
    state_groups = (
        ("待付款", "待支付", "未付款"),
        ("待发货", "备货"),
        ("运输中", "已发货", "物流中"),
        ("已完成", "已收货"),
        ("待评价", "未评价"),
        ("售后中", "退款中", "售后订单"),
    )
    return sum(any(marker in normalized for marker in markers) for markers in state_groups) >= 2


def _requests_order_spend_summary_query(query: str | None) -> bool:
    normalized = re.sub(r"\s+", "", query or "").casefold()
    return "订单" in normalized and any(
        marker in normalized
        for marker in (
            "累计实付",
            "总共实付",
            "一共实付",
            "累计消费",
            "一共花",
            "总共花",
            "累计花",
            "花了多少钱",
            "已退款多少",
            "退款总额",
            "净支出",
        )
    )


def _requested_order_limit(query: str | None) -> int:
    """Honor a shopper's explicit order-card count without broadening results."""

    text = query or ""
    chinese_numbers = {
        "一": 1,
        "两": 2,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
    }
    match = re.search(
        r"(?:最近|展示|列出|只(?:展示|看)?)?\s*(?<!哪)([一两二三四五六七八\d]+)\s*笔",
        text,
    )
    if match is None:
        normalized = re.sub(r"\s+", "", text)
        if "订单" in normalized and any(
            marker in normalized for marker in ("哪些", "哪几笔", "可以取消", "可以确认收货")
        ):
            return 8
        return 5
    raw = match.group(1)
    value = int(raw) if raw.isdigit() else chinese_numbers.get(raw, 5)
    return max(1, min(value, 8))


def _requested_order_per_state_limit(query: str | None) -> int | None:
    """Return an explicit per-state card cap such as “每种最多1笔”."""

    text = re.sub(r"\s+", "", query or "")
    match = re.search(
        r"每(?:种|类|个状态)(?:最多|至多|只(?:要|展示|看)?)?([一两二三四五\d]+)笔",
        text,
    )
    if match is None:
        return None
    chinese_numbers = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5}
    raw = match.group(1)
    value = int(raw) if raw.isdigit() else chinese_numbers.get(raw, 1)
    return max(1, min(value, 3))


def _limit_orders_per_requested_state(
    rows: list[tuple[Order, Store]],
    items_by_order: dict[int, list[OrderItem]],
    query: str | None,
    per_state_limit: int,
) -> list[tuple[Order, Store]]:
    """Keep the newest bounded cards for each state explicitly requested."""

    normalized = re.sub(r"\s+", "", query or "").casefold()
    state_groups = (
        ("pending_payment", ("待付款", "待支付", "未付款")),
        ("pending_shipment", ("待发货", "备货")),
        ("shipped", ("运输中", "已发货", "物流中")),
        ("pending_review", ("待评价", "未评价")),
        ("after_sale", ("售后中", "退款中", "售后订单")),
        ("completed", ("已完成", "已收货")),
    )
    selected: list[tuple[Order, Store]] = []
    seen_order_ids: set[int] = set()
    for state, markers in state_groups:
        if not any(marker in normalized for marker in markers):
            continue
        matched_count = 0
        for order, store in rows:
            if order.id in seen_order_ids:
                continue
            item_rows = items_by_order.get(order.id, [])
            matches = (
                (state == "pending_payment" and order.order_status == "pending_payment")
                or (state == "pending_shipment" and order.order_status == "pending_shipment")
                or (state == "shipped" and order.order_status == "shipped")
                or (state == "completed" and order.order_status == "completed")
                or (
                    state == "pending_review"
                    and order.order_status == "completed"
                    and any(item.review_status == "pending" for item in item_rows)
                )
                or (state == "after_sale" and order.after_sale_status != "none")
            )
            if not matches:
                continue
            selected.append((order, store))
            seen_order_ids.add(order.id)
            matched_count += 1
            if matched_count >= per_state_limit:
                break
    return selected


def _requested_order_state_counts(
    rows: list[tuple[Order, Store]],
    items_by_order: dict[int, list[OrderItem]],
    query: str | None,
) -> dict[str, int]:
    normalized = re.sub(r"\s+", "", query or "").casefold()
    requested = {
        "待付款": ("待付款", "待支付", "未付款"),
        "待发货": ("待发货", "备货"),
        "运输中": ("运输中", "已发货", "物流中"),
        "待评价": ("待评价", "未评价"),
        "售后中": ("售后中", "退款中", "售后订单"),
        "已完成": ("已完成", "已收货"),
    }
    counts: dict[str, int] = {}
    for label, markers in requested.items():
        if not any(marker in normalized for marker in markers):
            continue
        counts[label] = sum(
            1
            for order, _store in rows
            if (
                (label == "待付款" and order.order_status == "pending_payment")
                or (label == "待发货" and order.order_status == "pending_shipment")
                or (label == "运输中" and order.order_status == "shipped")
                or (label == "已完成" and order.order_status == "completed")
                or (
                    label == "待评价"
                    and order.order_status == "completed"
                    and any(
                        item.review_status == "pending"
                        for item in items_by_order.get(order.id, [])
                    )
                )
                or (label == "售后中" and order.after_sale_status != "none")
            )
        )
    return counts


def _meaningful_product_reference(product_name: str, normalized_query: str) -> bool:
    product = re.sub(r"\s+", "", product_name).casefold()
    if product in normalized_query:
        return True
    for token in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{2,}", normalized_query):
        if token in {"订单", "刚刚", "刚才", "我的", "这个", "那个", "状态", "买的"}:
            continue
        if token in product:
            return True
    # Chinese questions are commonly written without spaces, so the regex
    # above may produce one long sentence rather than the noun “铅笔/直尺”.
    # Match bounded character n-grams after excluding generic order language.
    compact = re.sub(
        r"(?:订单|刚刚|刚才|我的|这个|那个|状态|买的|购买的|我在|专卖店|店铺|"
        r"发货|签收|收货|物流|快递|评价|如果|已经|还能|不能|是否|能否|了吗|吗)",
        "",
        normalized_query,
    )
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", compact))
    for width in range(min(8, len(chinese)), 1, -1):
        if any(
            chinese[index : index + width] in product
            for index in range(len(chinese) - width + 1)
        ):
            return True
    return False


def _extract_price_bound(text: str, *, lower: bool) -> int | None:
    patterns: tuple[str, ...]
    if lower:
        patterns = (
            rf"{_PRICE_NUMBER}\s*元\s*(?:以上|起|起步|不少于|不低于)",
            rf"(?:至少|最低|不低于)\s*{_PRICE_NUMBER}\s*元?",
        )
    else:
        patterns = (
            rf"{_PRICE_NUMBER}\s*元\s*(?:以内|以下|内|封顶|不超过|最多)",
            rf"(?:不超过|最多|最高|预算(?:是|为|在|改成|改为)?|价格(?:改成|改为)|控制在)\s*{_PRICE_NUMBER}\s*元?(?!\s*(?:件|款|个|笔|种))",
            rf"(?:放宽|提高|调整|改|收紧)(?:到|为|成)\s*{_PRICE_NUMBER}\s*元?",
        )
    matches = [
        match
        for pattern in patterns
        if (match := re.search(pattern, text)) is not None
    ]
    for match in sorted(matches, key=lambda item: item.start()):
        try:
            return int(Decimal(match.group(1)) * 100)
        except (InvalidOperation, ValueError):
            continue
    return None


def _strip_catalog_request_syntax(text: str) -> str:
    # Presentation/evidence requests after a concrete product phrase are not
    # part of the catalogue subject.  Keep them out of SQL candidates while
    # leaving the answer layer free to expose verified sources.
    text = re.sub(
        r"(?:[，,；;]\s*)?(?:结果)?(?:需要|需|请|要)?(?:使用|用|以)?"
        r"(?:可点击的?)?(?:商品|订单)?(?:卡片|列表)(?:的?形式)?"
        r"(?:来)?(?:展示|显示|呈现)?.*$",
        " ",
        text,
    )
    text = re.sub(
        r"(?:[，,\uff1b;]\s*)?(?:并且?|同时)(?:请)?(?:说明|告诉我|展示|列出).*$",
        " ",
        text,
    )
    cleaned = re.sub(
        rf"{_PRICE_NUMBER}\s*元\s*(?:以内|以下|以上|内|起|起步|封顶|不超过|不少于|不低于|最多)?",
        " ",
        text,
    )
    cleaned = re.sub(
        rf"(?:不超过|最多|最高|最低|至少|预算(?:是|为|在|改成|改为)?|价格(?:改成|改为)|控制在)\s*{_PRICE_NUMBER}\s*元?",
        " ",
        cleaned,
    )
    cleaned = re.sub(
        rf"(?:放宽|提高|调整|改|收紧)(?:到|为|成)\s*{_PRICE_NUMBER}\s*元?",
        " ",
        cleaned,
    )
    cleaned = re.sub(
        r"(?:仅|只)?(?:在|从)?(?:本店|当前店铺|店内|全平台)(?:的?范围内)?",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"价格\s*(?:在|为|是)", " ", cleaned)
    cleaned = re.sub(
        r"(?:按)?价格(?:从低到高|从高到低|升序|降序)|便宜到贵|贵到便宜|低价优先|高价优先|"
        r"最新上架|最新优先|按最新|销量排序|销量优先|卖得最好|最畅销",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"[一两二三四五\d]+\s*(?:件|个|款)", " ", cleaned)
    cleaned = re.sub(
        r"(?:麻烦|请|帮我|给我|我想|想要|看看|一下|全平台|当前|在售|搜索|查找|找找|找|推荐|介绍|讲讲|了解)",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"(?:我)?(?:喜欢|偏好)", " ", cleaned)
    cleaned = re.sub(r"(?:几件|几款|几个|一些|一批)", " ", cleaned)
    cleaned = re.sub(
        r"(?:预算和排序|预算、排序|预算|价格|排序|数量|其他条件|其余条件)不变",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"(?:预算|排序)", " ", cleaned)
    cleaned = re.sub(r"(?:我)?(?:改要|改成|改为|换成)", " ", cleaned)
    cleaned = re.sub(r"(?:适合|用于|用来|使用|能用来|可以买来|的)", " ", cleaned)
    cleaned = re.sub(r"[\u3001\u3002\uff0c\uff01\uff1f\uff1a\uff1b,:;!?]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _explicit_catalog_colors(text: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", "", text).casefold()
    colors = (
        "红色",
        "橙色",
        "黄色",
        "绿色",
        "蓝色",
        "紫色",
        "粉色",
        "黑色",
        "白色",
        "灰色",
        "棕色",
        "米色",
        "藏青色",
    )
    return tuple(
        color
        for color in colors
        if color in normalized
        and re.search(rf"(?:更喜欢|偏爱|优先|倾向).{{0,4}}{re.escape(color)}", normalized)
        is None
    )


def _preferred_catalog_colors(text: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", "", text).casefold()
    if "深色" in normalized:
        return ("黑色", "藏青色", "藏蓝色", "深灰色", "灰色", "棕色")
    if "浅色" in normalized:
        return ("白色", "米色", "浅灰色", "粉色", "黄色")
    colors = (
        "红色",
        "橙色",
        "黄色",
        "绿色",
        "蓝色",
        "紫色",
        "粉色",
        "黑色",
        "白色",
        "灰色",
        "棕色",
        "米色",
        "藏青色",
    )
    preferred = tuple(
        color
        for color in colors
        if re.search(rf"(?:更喜欢|偏爱|优先|倾向).{{0,4}}{re.escape(color)}", normalized)
    )
    if preferred:
        return preferred
    return ()


def _requested_catalog_seasons(text: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", "", text).casefold()
    seasons: list[str] = []
    markers = {
        "spring": ("春天", "春季", "春装"),
        "summer": ("夏天", "夏季", "夏装"),
        "autumn": ("秋天", "秋季", "秋装"),
        "winter": ("冬天", "冬季", "冬装"),
    }
    for season, values in markers.items():
        if any(value in normalized for value in values):
            seasons.append(season)
    return tuple(seasons)


def _matches_requested_catalog_seasons(
    product_text: str,
    requested_seasons: tuple[str, ...],
) -> bool:
    normalized = re.sub(r"\s+", "", product_text).casefold()
    season_markers = {
        "spring": ("春季", "春装", "春秋"),
        "summer": ("夏季", "夏装", "春夏", "夏秋"),
        "autumn": ("秋季", "秋装", "春秋", "夏秋"),
        "winter": ("冬季", "冬装"),
    }
    declared = {
        season
        for season, markers in season_markers.items()
        if any(marker in normalized for marker in markers)
    }
    return not declared or bool(declared.intersection(requested_seasons))


def _catalog_color_preference_score(
    product_text: str,
    preferred_colors: tuple[str, ...],
) -> int:
    normalized = re.sub(r"\s+", "", product_text).casefold()
    return sum(color in normalized for color in preferred_colors)


def _requested_catalog_weight(text: str) -> int | None:
    normalized = re.sub(r"\s+", "", text).casefold()
    match = re.search(r"(?P<weight>\d{2,3})斤", normalized)
    return int(match.group("weight")) if match is not None else None


def _matches_explicit_catalog_kind(product_name: str, request_text: str) -> bool:
    """Keep an explicit product noun from being diluted by purpose expansions."""

    name = re.sub(r"\s+", "", product_name).casefold()
    request = re.sub(r"\s+", "", request_text).casefold()
    if "铅笔" in request and "文具" not in request:
        return "铅笔" in name and not any(term in name for term in ("铅笔擦", "橡皮", "擦除"))
    if "直尺" in request and "文具" not in request:
        return "直尺" in name or "尺子" in name
    if "橡皮" in request and "文具" not in request:
        return "橡皮" in name or "铅笔擦" in name
    if "笔芯" in request and "文具" not in request:
        return "笔芯" in name
    return True


def _catalog_sku_weight_limit(sku_name: str) -> int | None:
    limits = [int(value) for value in re.findall(r"(\d{2,3})斤(?:以下|以内)?", sku_name)]
    return max(limits) if limits else None


def _catalog_keywords(text: str) -> tuple[str, ...]:
    generic = {"", "商品", "东西", "一些", "一款", "几款", "看看"}
    result: list[str] = []
    for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text):
        token = re.sub(r"(?:商品|产品)$", "", token)
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
