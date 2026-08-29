import hashlib
import os
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.database.mysql import mysql_session
from app.modules.catalog.models import Category, Product, ProductSku
from app.modules.identity.models import User
from app.modules.orders.models import Order, OrderItem, TradeOrder
from app.modules.reviews.models import Review
from app.modules.reviews.moderation import ReviewModerationProcessor
from app.modules.stores.models import Store
from app.modules.system.models import OutboxEvent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_review_moderation_publishes_safe_and_queues_ambiguous(
    client: AsyncClient,
) -> None:
    del client
    suffix = secrets.token_hex(5)
    now = utc_now()
    async for session in mysql_session():
        user = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"review_worker_{suffix}",
            username_normalized=f"review_worker_{suffix}",
            nickname="Review Worker",
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            permission_version=1,
            registered_at=now,
        )
        session.add(user)
        await session.flush()
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=user.id,
            store_name=f"review-store-{suffix}",
            store_name_normalized=f"review-store-{suffix}",
            store_status="active",
            rating_score=Decimal("0.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=now,
        )
        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            category_name=f"Review category {suffix}",
            category_code=f"review-category-{suffix}",
            path=f"/review-category-{suffix}",
            level=1,
            sort_order=0,
            category_status="active",
        )
        session.add_all([store, category])
        await session.flush()
        product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=store.id,
            category_id=category.id,
            product_name=f"评价审核商品 {suffix}",
            product_status="on_sale",
            min_price_amount=1000,
            max_price_amount=1000,
            currency="CNY",
            rating_score=Decimal("0.00"),
            review_count=0,
            published_at=now,
        )
        session.add(product)
        await session.flush()
        sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=product.id,
            store_id=store.id,
            merchant_sku_code=f"REVIEW-{suffix}",
            sku_name="标准款",
            spec_values=[],
            spec_signature=hashlib.sha256(suffix.encode()).digest(),
            sale_price_amount=1000,
            market_price_amount=1000,
            currency="CNY",
            sku_status="active",
        )
        session.add(sku)
        await session.flush()
        trade = TradeOrder(
            trade_no=new_prefixed_ulid("trd_"),
            checkout_session_id=None,
            checkout_no_snapshot=new_prefixed_ulid("chk_"),
            checkout_snapshot_hash=hashlib.sha256(f"review-{suffix}".encode()).digest(),
            user_id=user.id,
            order_source="buy_now",
            trade_status="paid",
            goods_amount=2000,
            freight_amount=0,
            payable_amount=2000,
            adjustment_amount=0,
            paid_amount=2000,
            refunded_amount=0,
            currency="CNY",
            order_count=1,
            expires_at=now + timedelta(hours=1),
            paid_at=now,
        )
        session.add(trade)
        await session.flush()
        order = Order(
            order_no=new_prefixed_ulid("ord_"),
            trade_order_id=trade.id,
            user_id=user.id,
            store_id=store.id,
            order_status="completed",
            payment_status="paid",
            fulfillment_status="received",
            after_sale_status="none",
            goods_amount=2000,
            freight_amount=0,
            payable_amount=2000,
            adjustment_amount=0,
            paid_amount=2000,
            refunded_amount=0,
            currency="CNY",
            policy_snapshot={"version": "review-test"},
            expires_at=now + timedelta(hours=1),
            paid_at=now,
            completed_at=now,
        )
        session.add(order)
        await session.flush()
        reviews: list[Review] = []
        for index, content in enumerate(("包装很好，符合预期", "加微信了解后续"), start=1):
            item = OrderItem(
                order_item_no=new_prefixed_ulid("oit_"),
                order_id=order.id,
                product_id=product.id,
                sku_id=sku.id,
                product_no=product.product_no,
                sku_no=sku.sku_no,
                product_name=product.product_name,
                sku_name=sku.sku_name,
                spec_snapshot=[],
                quantity=1,
                unit_price_amount=1000,
                market_price_amount=1000,
                gross_amount=1000,
                payable_amount=1000,
                adjustment_amount=0,
                refunded_quantity=0,
                refunded_amount=0,
                currency="CNY",
                review_status="reviewed",
                after_sale_status="none",
            )
            session.add(item)
            await session.flush()
            review = Review(
                review_no=new_prefixed_ulid("rev_"),
                order_id=order.id,
                order_item_id=item.id,
                user_id=user.id,
                store_id=store.id,
                product_id=product.id,
                sku_id=sku.id,
                rating=5 if index == 1 else 1,
                content=content,
                is_anonymous=False,
                review_status="pending",
                moderation_status="pending",
                helpful_count=0,
            )
            session.add(review)
            await session.flush()
            session.add(
                OutboxEvent(
                    event_no=new_prefixed_ulid("evt_"),
                    event_type="review.submitted.v1",
                    aggregate_type="review",
                    aggregate_no=review.review_no,
                    aggregate_version=review.version,
                    payload={"review_id": review.review_no},
                    event_status="pending",
                    available_at=now,
                    attempt_count=0,
                    trace_id=new_prefixed_ulid("trc_"),
                )
            )
            if index == 1:
                session.add(
                    OutboxEvent(
                        event_no=new_prefixed_ulid("evt_"),
                        event_type="review.submitted.v1",
                        aggregate_type="review",
                        aggregate_no=review.review_no,
                        aggregate_version=review.version,
                        payload={"review_id": review.review_no},
                        event_status="pending",
                        available_at=now,
                        attempt_count=0,
                        trace_id=new_prefixed_ulid("trc_"),
                    )
                )
            reviews.append(review)
        await session.commit()

        assert await ReviewModerationProcessor(session).process_batch(10) == 3
        safe = await session.scalar(select(Review).where(Review.id == reviews[0].id))
        manual = await session.scalar(select(Review).where(Review.id == reviews[1].id))
        refreshed_product = await session.get(Product, product.id)
        refreshed_store = await session.get(Store, store.id)
        assert safe is not None and safe.review_status == "published"
        assert safe.moderation_status == "passed" and safe.published_at is not None
        assert manual is not None and manual.review_status == "pending"
        assert manual.moderation_status == "manual"
        assert refreshed_product is not None and refreshed_product.review_count == 1
        assert refreshed_product.rating_score == Decimal("5.00")
        assert refreshed_store is not None and refreshed_store.rating_count == 1
        assert refreshed_store.rating_score == Decimal("5.00")
        break
