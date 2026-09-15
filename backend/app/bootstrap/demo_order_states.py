"""Create idempotent local acceptance orders for every customer-facing state.

This module is intentionally not called by the normal bootstrap command.  It is
for local browser/Agent acceptance only and refuses to run outside development.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.modules.after_sale.models import RefundApplication, RefundEvent, RefundItem
from app.modules.catalog.models import Product, ProductImage, ProductSku
from app.modules.checkout.models import CheckoutSession as _CheckoutSession  # noqa: F401
from app.modules.files.models import FileObject as _FileObject  # noqa: F401
from app.modules.identity.models import User, UserAddress
from app.modules.logistics.models import Shipment, ShipmentItem, ShipmentTrack
from app.modules.orders.models import (
    Order,
    OrderAddress,
    OrderItem,
    OrderStatusLog,
    TradeOrder,
)
from app.modules.payments.models import Payment, PaymentEvent
from app.modules.stores.models import Store


@dataclass(frozen=True)
class DemoState:
    code: str
    order_status: str
    payment_status: str
    fulfillment_status: str
    after_sale_status: str
    trade_status: str
    age: timedelta


STATES = (
    DemoState(
        "pending_payment",
        "pending_payment",
        "unpaid",
        "unfulfilled",
        "none",
        "pending_payment",
        timedelta(minutes=15),
    ),
    DemoState(
        "pending_shipment",
        "pending_shipment",
        "paid",
        "unfulfilled",
        "none",
        "paid",
        timedelta(hours=5),
    ),
    DemoState(
        "in_transit",
        "shipped",
        "paid",
        "shipped",
        "none",
        "paid",
        timedelta(days=2),
    ),
    DemoState(
        "pending_review",
        "completed",
        "paid",
        "received",
        "none",
        "paid",
        timedelta(days=6),
    ),
    DemoState(
        "after_sale",
        "completed",
        "paid",
        "received",
        "in_progress",
        "paid",
        timedelta(days=8),
    ),
)


async def seed_demo_order_states(
    session: AsyncSession,
    settings: Settings,
    *,
    username: str = "tulubi",
) -> dict[str, str]:
    if settings.environment != "development":
        raise RuntimeError("demo order states may only be created in development")
    user = await session.scalar(
        select(User).where(
            User.username_normalized == username.casefold(),
            User.deleted_at.is_(None),
        )
    )
    if user is None:
        raise RuntimeError(f"development user {username!r} was not found")
    address = await session.scalar(
        select(UserAddress)
        .where(UserAddress.user_id == user.id, UserAddress.deleted_at.is_(None))
        .order_by(UserAddress.is_default.desc(), UserAddress.id)
    )
    if address is None:
        raise RuntimeError(f"development user {username!r} has no shipping address")

    product_rows = list(
        (
            await session.execute(
                select(Product, ProductSku, Store)
                .join(ProductSku, ProductSku.product_id == Product.id)
                .join(Store, Store.id == Product.store_id)
                .where(
                    Product.deleted_at.is_(None),
                    Product.product_status == "on_sale",
                    ProductSku.sku_status == "active",
                    Store.store_status == "active",
                )
                .order_by(Store.id, Product.id, ProductSku.id)
            )
        ).all()
    )
    if not product_rows:
        raise RuntimeError("no active product/SKU is available for demo orders")

    now = utc_now()
    security = SecurityService(settings)
    created: dict[str, str] = {}
    for index, state in enumerate(STATES):
        fixture_key = f"demo-{username}-{state.code}-v1"
        existing = await session.scalar(
            select(TradeOrder).where(TradeOrder.checkout_no_snapshot == fixture_key)
        )
        if existing is not None:
            order_no = await session.scalar(
                select(Order.order_no).where(Order.trade_order_id == existing.id)
            )
            if order_no is not None:
                created[state.code] = order_no
            continue
        product, sku, store = product_rows[index % len(product_rows)]
        image_key = await session.scalar(
            select(ProductImage.object_key)
            .where(
                ProductImage.product_id == product.id,
                ProductImage.sku_id == sku.id,
                ProductImage.image_status == "active",
            )
            .order_by(ProductImage.sort_order, ProductImage.id)
        )
        created_at = now - state.age
        amount = max(1, sku.sale_price_amount)
        paid_amount = amount if state.payment_status == "paid" else 0
        paid_at = created_at + timedelta(minutes=2) if paid_amount else None
        shipped_at = (
            created_at + timedelta(hours=8)
            if state.fulfillment_status in {"shipped", "received"}
            else None
        )
        completed_at = (
            created_at + timedelta(days=2)
            if state.fulfillment_status == "received"
            else None
        )
        trade = TradeOrder(
            trade_no=new_prefixed_ulid("trd_"),
            checkout_session_id=None,
            checkout_no_snapshot=fixture_key,
            checkout_snapshot_hash=hashlib.sha256(fixture_key.encode()).digest(),
            user_id=user.id,
            order_source="buy_now",
            trade_status=state.trade_status,
            goods_amount=amount,
            freight_amount=0,
            payable_amount=amount,
            adjustment_amount=0,
            paid_amount=paid_amount,
            refunded_amount=0,
            currency="CNY",
            original_order_count=1,
            expires_at=(
                now + timedelta(hours=2)
                if not paid_amount
                else created_at + timedelta(hours=2)
            ),
            paid_at=paid_at,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(trade)
        await session.flush()
        order = Order(
            order_no=new_prefixed_ulid("ord_"),
            trade_order_id=trade.id,
            user_id=user.id,
            store_id=store.id,
            order_status=state.order_status,
            payment_status=state.payment_status,
            fulfillment_status=state.fulfillment_status,
            after_sale_status=state.after_sale_status,
            goods_amount=amount,
            freight_amount=0,
            payable_amount=amount,
            adjustment_amount=0,
            paid_amount=paid_amount,
            refunded_amount=0,
            currency="CNY",
            buyer_remark="AI 与页面验收样本",
            policy_snapshot={
                "schema_version": 1,
                "fixture": True,
                "delivery_option": {"label": "邮寄", "freight_amount": "0"},
            },
            expires_at=trade.expires_at,
            paid_at=paid_at,
            shipped_at=shipped_at,
            completed_at=completed_at,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(order)
        await session.flush()
        order_item = OrderItem(
            order_item_no=new_prefixed_ulid("oit_"),
            order_id=order.id,
            product_id=product.id,
            sku_id=sku.id,
            product_no=product.product_no,
            sku_no=sku.sku_no,
            product_name=product.product_name,
            sku_name=sku.sku_name,
            spec_snapshot=sku.spec_values,
            image_object_key=image_key,
            quantity=1,
            unit_price_amount=amount,
            market_price_amount=max(amount, sku.market_price_amount),
            gross_amount=amount,
            payable_amount=amount,
            adjustment_amount=0,
            refunded_quantity=0,
            refunded_amount=0,
            currency="CNY",
            review_status="pending",
            after_sale_status=("in_progress" if state.code == "after_sale" else "none"),
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(order_item)
        await session.flush()
        session.add(_address_snapshot(order.id, address, created_at))
        _add_order_timeline(session, order, user, state, created_at)

        if paid_amount:
            payment = Payment(
                payment_no=new_prefixed_ulid("pay_"),
                trade_order_id=trade.id,
                user_id=user.id,
                provider="fake",
                payment_method="fake_balance",
                provider_trade_no=f"fixture:{fixture_key}",
                payment_status="succeeded",
                requested_amount=amount,
                paid_amount=amount,
                refunded_amount=0,
                currency="CNY",
                expires_at=trade.expires_at,
                paid_at=paid_at,
                created_at=paid_at,
                updated_at=paid_at,
            )
            session.add(payment)
            await session.flush()
            session.add(
                PaymentEvent(
                    event_no=new_prefixed_ulid("evt_"),
                    payment_id=payment.id,
                    event_type="succeeded",
                    from_status="pending",
                    to_status="succeeded",
                    amount=amount,
                    currency="CNY",
                    source_type="fixture",
                    source_no=fixture_key,
                    provider_occurred_at=paid_at,
                    trace_id=fixture_key,
                    created_at=paid_at,
                )
            )

        if state.fulfillment_status in {"shipped", "received"}:
            shipment = _shipment(
                security,
                order,
                store,
                state,
                created_at,
                fixture_key,
            )
            session.add(shipment)
            await session.flush()
            session.add(
                ShipmentItem(
                    shipment_id=shipment.id,
                    order_item_id=order_item.id,
                    quantity=1,
                )
            )
            _add_tracks(session, shipment, state, created_at, fixture_key)

        if state.code == "after_sale":
            submitted_at = (completed_at or created_at) + timedelta(hours=4)
            refund = RefundApplication(
                refund_no=new_prefixed_ulid("ref_"),
                order_id=order.id,
                user_id=user.id,
                store_id=store.id,
                refund_type="refund_only",
                refund_status="merchant_review",
                reason_code="NOT_AS_EXPECTED",
                reason_detail="AI 与页面售后状态验收样本",
                requested_amount=amount,
                approved_amount=0,
                currency="CNY",
                policy_snapshot={"version": "refund-policy-v1", "fixture": True},
                submitted_at=submitted_at,
                created_at=submitted_at,
                updated_at=submitted_at,
            )
            session.add(refund)
            await session.flush()
            session.add(
                RefundItem(
                    refund_id=refund.id,
                    order_item_id=order_item.id,
                    quantity=1,
                    requested_amount=amount,
                    succeeded_amount=0,
                    refund_status="active",
                    created_at=submitted_at,
                    updated_at=submitted_at,
                )
            )
            session.add(
                RefundEvent(
                    event_no=new_prefixed_ulid("evt_"),
                    refund_id=refund.id,
                    from_status="submitted",
                    to_status="merchant_review",
                    event_code="refund.merchant_review_started",
                    actor_type="system",
                    actor_user_id=None,
                    reason="已提交店铺审核",
                    request_id=fixture_key,
                    created_at=submitted_at,
                )
            )
        created[state.code] = order.order_no
    await session.commit()
    return created


def _address_snapshot(order_id: int, address: UserAddress, created_at: datetime) -> OrderAddress:
    digest = hashlib.sha256()
    for value in (
        address.recipient_name_ciphertext,
        address.phone_ciphertext,
        address.country_code.encode(),
        address.province_code.encode(),
        address.city_code.encode(),
        address.district_code.encode(),
        address.address_ciphertext,
        (address.postal_code or "").encode(),
    ):
        digest.update(len(value).to_bytes(4, "big"))
        digest.update(value)
    return OrderAddress(
        order_id=order_id,
        source_address_no=address.address_no,
        recipient_name_ciphertext=address.recipient_name_ciphertext,
        phone_ciphertext=address.phone_ciphertext,
        phone_last4=address.phone_last4,
        country_code=address.country_code,
        province_code=address.province_code,
        city_code=address.city_code,
        district_code=address.district_code,
        address_ciphertext=address.address_ciphertext,
        postal_code=address.postal_code,
        address_hash=digest.digest(),
        key_version=address.key_version,
        created_at=created_at,
    )


def _add_order_timeline(
    session: AsyncSession,
    order: Order,
    user: User,
    state: DemoState,
    created_at: datetime,
) -> None:
    events: list[tuple[str, str | None, str, str, datetime]] = [
        ("order", None, "pending_payment", "order.created", created_at),
        ("payment", None, "unpaid", "order.created", created_at),
        ("fulfillment", None, "unfulfilled", "order.created", created_at),
        ("after_sale", None, "none", "order.created", created_at),
    ]
    if state.payment_status == "paid":
        paid_at = created_at + timedelta(minutes=2)
        events.extend(
            (
                ("payment", "unpaid", "paid", "payment.succeeded", paid_at),
                ("order", "pending_payment", "paid", "order.payment_succeeded", paid_at),
                ("order", "paid", "pending_shipment", "order.fulfillment_initialized", paid_at),
            )
        )
    if state.fulfillment_status in {"shipped", "received"}:
        shipped_at = created_at + timedelta(hours=8)
        events.extend(
            (
                ("fulfillment", "unfulfilled", "shipped", "fulfillment.shipped", shipped_at),
                ("order", "pending_shipment", "shipped", "order.shipped", shipped_at),
            )
        )
    if state.fulfillment_status == "received":
        completed_at = created_at + timedelta(days=2)
        events.extend(
            (
                ("fulfillment", "shipped", "received", "fulfillment.received", completed_at),
                ("order", "shipped", "completed", "order.completed", completed_at),
            )
        )
    if state.after_sale_status == "in_progress":
        events.append(
            (
                "after_sale",
                "none",
                "in_progress",
                "refund.merchant_review_started",
                created_at + timedelta(days=2, hours=4),
            )
        )
    for index, (dimension, from_status, to_status, event_code, event_at) in enumerate(events):
        session.add(
            OrderStatusLog(
                order_id=order.id,
                state_dimension=dimension,
                from_status=from_status,
                to_status=to_status,
                event_code=event_code,
                actor_type=("user" if index < 4 else "system"),
                actor_id=(user.id if index < 4 else None),
                order_version=index,
                request_id=f"fixture:{order.order_no}",
                trace_id=f"fixture:{order.order_no}",
                created_at=event_at,
            )
        )


def _shipment(
    security: SecurityService,
    order: Order,
    store: Store,
    state: DemoState,
    created_at: datetime,
    fixture_key: str,
) -> Shipment:
    tracking_no = f"FX{hashlib.sha256(fixture_key.encode()).hexdigest()[:14].upper()}"
    delivered_at = (
        created_at + timedelta(days=2)
        if state.fulfillment_status == "received"
        else None
    )
    return Shipment(
        shipment_no=new_prefixed_ulid("shp_"),
        order_id=order.id,
        store_id=store.id,
        carrier_code="fake_express",
        carrier_name="模拟快递",
        tracking_no_ciphertext=security.encrypt("shipment-tracking-no", tracking_no),
        tracking_no_hash=security.keyed_hash("shipment-tracking-no", tracking_no),
        tracking_no_masked=f"{tracking_no[:4]}****{tracking_no[-4:]}",
        shipment_status=("delivered" if delivered_at else "in_transit"),
        provider_status=("SIGNED" if delivered_at else "IN_TRANSIT"),
        estimated_delivery_min_at=created_at + timedelta(days=2),
        estimated_delivery_max_at=created_at + timedelta(days=4),
        estimate_source="shipping_template",
        estimate_updated_at=created_at + timedelta(hours=8),
        shipped_at=created_at + timedelta(hours=8),
        delivered_at=delivered_at,
        last_track_at=delivered_at or created_at + timedelta(days=1),
        key_version=1,
        created_at=created_at + timedelta(hours=8),
        updated_at=delivered_at or created_at + timedelta(days=1),
    )


def _add_tracks(
    session: AsyncSession,
    shipment: Shipment,
    state: DemoState,
    created_at: datetime,
    fixture_key: str,
) -> None:
    tracks = [
        ("picked_up", "包裹已由快递员揽收", "商家发货地", created_at + timedelta(hours=9)),
        ("in_transit", "包裹运输中", "运输途中", created_at + timedelta(days=1)),
    ]
    if state.fulfillment_status == "received":
        tracks.append(
            ("delivered", "包裹已签收", "收货地址", created_at + timedelta(days=2))
        )
    for index, (status, description, location, occurred_at) in enumerate(tracks, start=1):
        payload = f"{fixture_key}:{index}:{status}:{occurred_at.isoformat()}"
        session.add(
            ShipmentTrack(
                shipment_id=shipment.id,
                provider_event_id=f"{fixture_key}:{index}",
                track_status=status,
                provider_status=status.upper(),
                description=description,
                location_text=location,
                occurred_at=occurred_at,
                payload_hash=hashlib.sha256(payload.encode()).digest(),
                created_at=occurred_at,
            )
        )


async def run() -> None:
    settings = get_settings()
    initialize_mysql(settings.mysql_dsn)
    try:
        async for session in mysql_session():
            result = await seed_demo_order_states(session, settings)
            print("demo order states ready:", ", ".join(sorted(result)))
            break
    finally:
        await close_mysql()


if __name__ == "__main__":
    asyncio.run(run())
