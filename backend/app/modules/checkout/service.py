from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import canonical_request_hash, utc_now
from app.modules.catalog.models import ProductFulfillmentProfile, ProductSku
from app.modules.catalog.schemas import Money, ServiceEstimate
from app.modules.checkout.models import CheckoutSession, CheckoutSnapshot
from app.modules.checkout.repository import CheckoutRepository, ItemContext
from app.modules.checkout.schemas import (
    CheckoutAmounts,
    CheckoutCreateRequest,
    CheckoutIssue,
    CheckoutItemView,
    CheckoutPatchRequest,
    CheckoutStoreGroupView,
    CheckoutView,
    DeliveryOptionView,
)
from app.modules.identity.models import User, UserAddress
from app.modules.inventory.models import Inventory
from app.modules.stores.models import ShippingTemplate

CHECKOUT_TTL_MINUTES = 30
PRICING_POLICY_VERSION = "pricing_v2_free_shipping"


@dataclass(frozen=True)
class CheckoutSubmissionContext:
    session: CheckoutSession
    snapshot: CheckoutSnapshot
    address: UserAddress
    source: dict[str, Any]
    contexts: list[tuple[ItemContext, int]]
    view: CheckoutView


class CheckoutService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = CheckoutRepository(session)
        self.idempotency = IdempotencyService(session)

    async def create(self, user: User, payload: CheckoutCreateRequest, key: str) -> CheckoutView:
        claim = await self.idempotency.begin(
            scope_key=f"checkout:create:{user.user_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="checkout_session",
        )
        if claim.replayed:
            if claim.record.response_body is None:
                raise _conflict("CHECKOUT_REPLAY_UNAVAILABLE", "结算会话尚未创建完成。")
            return CheckoutView.model_validate(claim.record.response_body)

        address = await self._address(user.id, payload.address_id)
        source_spec = payload.source.model_dump(mode="json")
        contexts = await self._contexts(user.id, source_spec)
        checkout_no = new_prefixed_ulid("chk_")
        expires_at = utc_now() + timedelta(minutes=CHECKOUT_TTL_MINUTES)
        view = await self._price(
            checkout_no,
            payload.source.source_type,
            contexts,
            address,
            {},
            expires_at,
            PRICING_POLICY_VERSION,
            0,
        )
        view_payload = view.model_dump(mode="json")
        snapshot_payload: dict[str, object] = {"view": view_payload, "source": source_spec}
        digest = canonical_request_hash(snapshot_payload)
        row = CheckoutSession(
            checkout_no=checkout_no,
            user_id=user.id,
            source_type=payload.source.source_type,
            checkout_status="active",
            selected_address_id=address.id if address else None,
            goods_amount=int(view.amounts.goods_amount.minor_units),
            freight_amount=int(view.amounts.freight_amount.minor_units),
            payable_amount=int(view.amounts.payable_amount.minor_units),
            currency="CNY",
            pricing_version=PRICING_POLICY_VERSION,
            snapshot_hash=digest,
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        self.session.add(
            CheckoutSnapshot(
                checkout_session_id=row.id,
                snapshot_version=1,
                schema_version=1,
                snapshot_payload=snapshot_payload,
                snapshot_hash=digest,
            )
        )
        self.idempotency.complete(
            claim,
            response_status=201,
            resource_no=checkout_no,
            response_body=cast(dict[str, object], view_payload),
        )
        await self.session.commit()
        return view

    async def get(self, user: User, checkout_no: str) -> CheckoutView:
        row = await self._checkout(user.id, checkout_no)
        snapshot = await self.repository.current_snapshot(row.id)
        if snapshot is None:
            raise _conflict("CHECKOUT_SNAPSHOT_MISSING", "结算快照不可用，请重新结算。")
        return CheckoutView.model_validate(snapshot.snapshot_payload["view"])

    async def patch(
        self,
        user: User,
        checkout_no: str,
        payload: CheckoutPatchRequest,
        expected_version: int,
    ) -> CheckoutView:
        row = await self._checkout(user.id, checkout_no, for_update=True)
        self._require_version(row, expected_version)
        snapshot = await self._snapshot(row)
        source = cast(dict[str, Any], snapshot.snapshot_payload["source"])
        current_view = CheckoutView.model_validate(snapshot.snapshot_payload["view"])
        address_no = (
            payload.address_id if payload.address_id is not None else current_view.address_id
        )
        address = await self._address(user.id, address_no)
        remarks = {group.store_id: group.buyer_remark or "" for group in current_view.store_groups}
        if payload.buyer_remarks is not None:
            remarks = {item.store_id: item.content for item in payload.buyer_remarks}
        return await self._replace(row, snapshot, source, address, remarks, "checkout_updated")

    async def reprice(self, user: User, checkout_no: str, key: str) -> CheckoutView:
        claim = await self.idempotency.begin(
            scope_key=f"checkout:reprice:{user.user_no}:{checkout_no}",
            idempotency_key=key,
            payload={"checkout_id": checkout_no},
            resource_type="checkout_repricing",
        )
        if claim.replayed:
            if claim.record.response_body is None:
                raise _conflict("CHECKOUT_REPLAY_UNAVAILABLE", "重新计价尚未完成。")
            return CheckoutView.model_validate(claim.record.response_body)
        row = await self._checkout(user.id, checkout_no, for_update=True)
        snapshot = await self._snapshot(row)
        source = cast(dict[str, Any], snapshot.snapshot_payload["source"])
        current_view = CheckoutView.model_validate(snapshot.snapshot_payload["view"])
        address = await self._address(user.id, current_view.address_id)
        remarks = {group.store_id: group.buyer_remark or "" for group in current_view.store_groups}
        view = await self._replace(
            row, snapshot, source, address, remarks, "repriced", commit=False
        )
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=checkout_no,
            response_body=cast(dict[str, object], view.model_dump(mode="json")),
        )
        await self.session.commit()
        return view

    async def submission_context(
        self, user: User, checkout_no: str, expected_version: int
    ) -> CheckoutSubmissionContext:
        row = await self._checkout(user.id, checkout_no, for_update=True)
        if row.version != expected_version:
            raise ApplicationError(
                status=412,
                code="CHECKOUT_VERSION_MISMATCH",
                title="Checkout version mismatch",
                detail="结算信息已变化，请刷新并重新确认。",
            )
        snapshot = await self._snapshot(row)
        source = cast(dict[str, Any], snapshot.snapshot_payload["source"])
        previous = CheckoutView.model_validate(snapshot.snapshot_payload["view"])
        address = await self._address(user.id, previous.address_id)
        if address is None:
            raise _conflict("ADDRESS_REQUIRED", "请选择有效的收货地址。")
        contexts = await self._contexts(user.id, source)
        remarks = {group.store_id: group.buyer_remark or "" for group in previous.store_groups}
        current = await self._price(
            row.checkout_no,
            row.source_type,
            contexts,
            address,
            remarks,
            row.expires_at,
            PRICING_POLICY_VERSION,
            row.version,
        )
        if current.blocking_issues or _commercial_fingerprint(current) != _commercial_fingerprint(
            previous
        ):
            raise ApplicationError(
                status=412,
                code="CHECKOUT_VERSION_MISMATCH",
                title="Checkout requires repricing",
                detail="价格、库存或配送信息已变化，请刷新结算信息后再次确认。",
                errors=[
                    {
                        "pointer": "/checkout_id",
                        "code": "REPRICE_REQUIRED",
                        "message": "请重新计价并确认最新结算信息。",
                    }
                ],
            )
        return CheckoutSubmissionContext(row, snapshot, address, source, contexts, previous)

    async def _replace(
        self,
        row: CheckoutSession,
        old: CheckoutSnapshot,
        source: dict[str, Any],
        address: UserAddress | None,
        remarks: dict[str, str],
        reason: str,
        *,
        commit: bool = True,
    ) -> CheckoutView:
        contexts = await self._contexts(row.user_id, source)
        next_snapshot_version = old.snapshot_version + 1
        next_version = row.version + 1
        view = await self._price(
            row.checkout_no,
            row.source_type,
            contexts,
            address,
            remarks,
            row.expires_at,
            PRICING_POLICY_VERSION,
            next_version,
        )
        payload: dict[str, object] = {
            "view": view.model_dump(mode="json"),
            "source": source,
        }
        digest = canonical_request_hash(payload)
        old.invalidated_at = utc_now()
        old.invalid_reason = reason
        row.selected_address_id = address.id if address else None
        row.goods_amount = int(view.amounts.goods_amount.minor_units)
        row.freight_amount = int(view.amounts.freight_amount.minor_units)
        row.payable_amount = int(view.amounts.payable_amount.minor_units)
        row.snapshot_hash = digest
        row.pricing_version = PRICING_POLICY_VERSION
        row.version = next_version
        self.session.add(
            CheckoutSnapshot(
                checkout_session_id=row.id,
                snapshot_version=next_snapshot_version,
                schema_version=1,
                snapshot_payload=payload,
                snapshot_hash=digest,
            )
        )
        if commit:
            await self.session.commit()
        return view

    async def _price(
        self,
        checkout_no: str,
        source_type: str,
        contexts: list[tuple[ItemContext, int]],
        address: UserAddress | None,
        remarks: dict[str, str],
        expires_at: Any,
        pricing_version: str,
        version: int,
    ) -> CheckoutView:
        policy_versions = await self.repository.policy_versions(
            {item[0][3].id for item in contexts}
        )
        groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
        blocking: list[CheckoutIssue] = []
        goods_total = freight_total = 0
        if address is None:
            blocking.append(
                CheckoutIssue(code="ADDRESS_REQUIRED", message="请选择有效的收货地址。")
            )
        for context, quantity in contexts:
            _, sku, product, store, inventory, profile, template = context
            available = _available(inventory)
            if store.store_status != "active":
                blocking.append(
                    _issue("STORE_UNAVAILABLE", "店铺当前不可用。", store.store_no, sku.sku_no)
                )
            elif product.product_status != "on_sale":
                blocking.append(
                    _issue("PRODUCT_OFF_SHELF", "商品已下架。", store.store_no, sku.sku_no)
                )
            elif sku.sku_status != "active":
                blocking.append(
                    _issue("SKU_UNAVAILABLE", "商品款式不可购买。", store.store_no, sku.sku_no)
                )
            elif available < quantity:
                blocking.append(
                    _issue(
                        "INSUFFICIENT_STOCK", "库存不足，请调整数量。", store.store_no, sku.sku_no
                    )
                )
            subtotal = sku.sale_price_amount * quantity
            goods_total += subtotal
            group = groups.setdefault(
                store.store_no,
                {
                    "store": store,
                    "items": [],
                    "goods": 0,
                    "freight": 0,
                    "options": [],
                    "delivery_inputs": [],
                },
            )
            group["goods"] += subtotal
            group["items"].append(
                CheckoutItemView(
                    product_id=product.product_no,
                    sku_id=sku.sku_no,
                    product_name=product.product_name,
                    sku_name=sku.sku_name,
                    quantity=quantity,
                    unit_price=_money(sku.sale_price_amount),
                    subtotal=_money(subtotal),
                    available_quantity=available,
                )
            )
            group["delivery_inputs"].append((template, profile, sku, quantity))
        views: list[CheckoutStoreGroupView] = []
        for store_no, value in groups.items():
            store = value["store"]
            fee, option = _delivery_group(value["delivery_inputs"], address)
            if option is None:
                blocking.append(
                    CheckoutIssue(
                        code="DELIVERY_UNAVAILABLE",
                        message="当前地址暂无可用配送方式。",
                        store_id=store_no,
                    )
                )
            else:
                value["freight"] = fee
                value["options"] = [option]
                freight_total += fee
            option_id = value["options"][0].option_id if value["options"] else None
            views.append(
                CheckoutStoreGroupView(
                    store_id=store_no,
                    store_name=store.store_name,
                    items=value["items"],
                    goods_amount=_money(value["goods"]),
                    freight_amount=_money(value["freight"]),
                    delivery_options=value["options"],
                    selected_delivery_option=option_id,
                    buyer_remark=remarks.get(store_no) or None,
                    policy_versions=policy_versions.get(store.id, {}),
                    customer_service_context={"store_id": store_no, "entry": "store_support"},
                )
            )
        actions = ["change_address", "reprice"]
        if not blocking:
            actions.append("create_order")
        return CheckoutView(
            checkout_id=checkout_no,
            source_type=cast(Any, source_type),
            status="active",
            address_id=address.address_no if address else None,
            expires_at=expires_at,
            store_groups=views,
            amounts=CheckoutAmounts(
                goods_amount=_money(goods_total),
                freight_amount=_money(freight_total),
                payable_amount=_money(goods_total + freight_total),
            ),
            warnings=[],
            blocking_issues=blocking,
            available_actions=actions,
            pricing_version=pricing_version,
            version=version,
        )

    async def _contexts(
        self, user_id: int, source: dict[str, Any]
    ) -> list[tuple[ItemContext, int]]:
        if source["source_type"] == "buy_now":
            context = await self.repository.buy_now_context(source["sku_id"])
            if context is None:
                raise _not_found("SKU_NOT_FOUND", "未找到该商品款式。")
            return [(context, int(source["quantity"]))]
        item_nos = cast(list[str], source["cart_item_ids"])
        rows = await self.repository.cart_contexts(user_id, item_nos)
        if len(rows) != len(item_nos):
            raise _conflict("CART_ITEMS_CHANGED", "购物车商品已变化，请返回购物车后重试。")
        by_no = {row[0].cart_item_no: row for row in rows if row[0] is not None}
        result: list[tuple[ItemContext, int]] = []
        for item_no in item_nos:
            context = by_no[item_no]
            cart_item = context[0]
            if cart_item is None:  # only buy-now contexts omit the cart item
                raise _conflict("CART_ITEMS_CHANGED", "购物车商品已变化，请返回购物车后重试。")
            result.append((context, cart_item.quantity))
        return result

    async def _address(self, user_id: int, address_no: str | None) -> UserAddress | None:
        if address_no is None:
            return await self.repository.default_address(user_id)
        address = await self.repository.address(user_id, address_no)
        if address is None:
            raise _not_found("ADDRESS_NOT_FOUND", "未找到该收货地址。")
        return address

    async def _checkout(
        self, user_id: int, checkout_no: str, *, for_update: bool = False
    ) -> CheckoutSession:
        row = await self.repository.checkout(user_id, checkout_no, for_update=for_update)
        if row is None:
            raise _not_found("CHECKOUT_NOT_FOUND", "未找到该结算会话。")
        if row.checkout_status != "active":
            raise _conflict("CHECKOUT_NOT_ACTIVE", "结算会话已不可使用，请重新结算。")
        if row.expires_at <= utc_now():
            row.checkout_status = "expired"
            row.version += 1
            raise ApplicationError(
                status=410,
                code="CHECKOUT_EXPIRED",
                title="Checkout expired",
                detail="结算会话已过期，请重新结算。",
            )
        return row

    async def _snapshot(self, row: CheckoutSession) -> CheckoutSnapshot:
        snapshot = await self.repository.current_snapshot(row.id)
        if snapshot is None:
            raise _conflict("CHECKOUT_SNAPSHOT_MISSING", "结算快照不可用，请重新结算。")
        return snapshot

    @staticmethod
    def _require_version(row: CheckoutSession, expected: int) -> None:
        if row.version != expected:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="结算信息已变化，请刷新后重试。",
            )


def _available(inventory: Inventory | None) -> int:
    if inventory is None or inventory.inventory_status != "active":
        return 0
    return max(
        0,
        inventory.on_hand_quantity - inventory.reserved_quantity - inventory.safety_stock_quantity,
    )


def _money(value: int) -> Money:
    return Money(minor_units=str(value), currency="CNY")


def _issue(code: str, message: str, store_no: str, sku_no: str) -> CheckoutIssue:
    return CheckoutIssue(code=code, message=message, store_id=store_no, sku_id=sku_no)


def _delivery_group(
    inputs: list[tuple[ShippingTemplate | None, ProductFulfillmentProfile | None, ProductSku, int]],
    address: UserAddress | None,
) -> tuple[int, DeliveryOptionView | None]:
    if address is None or not inputs:
        return 0, None
    min_days: list[int] = []
    max_days: list[int] = []
    updated_at = None
    for template, profile, _sku, _quantity in inputs:
        if template is None or profile is None or template.template_status != "effective":
            return 0, None
        # The storefront currently uses one platform-wide policy: every physical
        # product is mailed free of charge. Region-specific template fees are kept
        # in the database for future policy versions but do not affect checkout.
        min_days.append(max(1, math.ceil(profile.dispatch_min_hours / 24)) + 2)
        max_days.append(max(1, math.ceil(profile.dispatch_max_hours / 24)) + 5)
        if updated_at is None or template.updated_at > updated_at:
            updated_at = template.updated_at
    now = utc_now()
    estimate = ServiceEstimate(
        estimate_type="delivery",
        status="available",
        min_at=now + timedelta(days=max(min_days)),
        max_at=now + timedelta(days=max(max_days)),
        source="shipping_template",
        source_updated_at=updated_at,
        calculated_at=now,
        timezone="Asia/Shanghai",
        disclaimer_code="ESTIMATE_NOT_GUARANTEE",
    )
    return 0, DeliveryOptionView(
        option_id="postal_delivery",
        name="邮寄",
        freight=_money(0),
        estimate=estimate,
    )


def _commercial_fingerprint(view: CheckoutView) -> tuple[object, ...]:
    groups = tuple(
        (
            group.store_id,
            group.goods_amount.minor_units,
            group.freight_amount.minor_units,
            tuple(sorted(group.policy_versions.items())),
            tuple(
                (
                    item.product_id,
                    item.sku_id,
                    item.quantity,
                    item.unit_price.minor_units,
                    item.subtotal.minor_units,
                )
                for item in group.items
            ),
        )
        for group in view.store_groups
    )
    return (
        view.address_id,
        view.pricing_version,
        view.amounts.goods_amount.minor_units,
        view.amounts.freight_amount.minor_units,
        view.amounts.payable_amount.minor_units,
        groups,
    )


def _not_found(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=404, code=code, title="Resource not found", detail=detail)


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Checkout conflict", detail=detail)
