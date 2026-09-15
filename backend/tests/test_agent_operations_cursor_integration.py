from __future__ import annotations

import hashlib
import os
import secrets
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.bootstrap.ai_runtime import seed_ai_runtime
from app.bootstrap.merchant import provision_store_operator
from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.modules.agent_runtime.operations_agent import _snapshot
from app.modules.agent_runtime.operations_context import TrustedOperationsContext
from app.modules.catalog.models import Category, Product, ProductSku
from app.modules.identity.models import User
from app.modules.messaging.models import Conversation, Message
from app.modules.orders.models import Order, OrderItem, TradeOrder
from app.modules.reviews.models import Review
from app.modules.stores.models import Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def _persist_continuation(
    session: Any,
    conversation: Conversation,
    tool_code: str,
    pagination: dict[str, object],
) -> None:
    conversation.last_sequence_no += 1
    conversation.last_message_at = utc_now()
    session.add(
        Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=conversation.last_sequence_no,
            sender_type="agent",
            message_type="text",
            text_content="分页结果",
            content_payload={"continuations": {tool_code: pagination}},
            message_status="sent",
            moderation_status="passed",
            sent_at=utc_now(),
        )
    )
    await session.flush()


async def test_merchant_agent_review_and_conversation_lists_continue_without_duplicates(
    client: AsyncClient,
) -> None:
    del client
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    async for session in mysql_session():
        await seed_ai_runtime(session)
        operator_result = await provision_store_operator(
            session,
            security,
            username=f"cursor_merchant_{suffix}",
            password=f"Cursor-{suffix}-Correct-Horse!",
            store_name=f"游标测试店铺 {suffix}",
        )
        operator = await session.scalar(
            select(User).where(User.user_no == operator_result.user_no)
        )
        store = await session.scalar(
            select(Store).where(Store.store_no == operator_result.store_no)
        )
        assert operator is not None and store is not None

        customer = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"cursor_customer_{suffix}",
            username_normalized=f"cursor_customer_{suffix}",
            nickname="游标顾客",
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            permission_version=1,
            registered_at=now,
        )
        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            category_name=f"游标分类 {suffix}",
            category_code=f"cursor-{suffix}",
            path=f"/cursor-{suffix}",
            level=1,
            sort_order=1,
            category_status="active",
        )
        session.add_all([customer, category])
        await session.flush()
        product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=store.id,
            category_id=category.id,
            product_name=f"游标测试商品 {suffix}",
            product_status="on_sale",
            min_price_amount=100,
            max_price_amount=100,
            currency="CNY",
            sales_count=7,
            review_count=7,
            rating_score=Decimal("5.00"),
            published_at=now,
        )
        session.add(product)
        await session.flush()
        sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=product.id,
            store_id=store.id,
            merchant_sku_code=f"CURSOR-{suffix}",
            sku_name="标准款",
            spec_values=[],
            spec_signature=hashlib.sha256(f"cursor-{suffix}".encode()).digest(),
            sale_price_amount=100,
            market_price_amount=100,
            currency="CNY",
            sku_status="active",
        )
        session.add(sku)
        await session.flush()
        trade = TradeOrder(
            trade_no=new_prefixed_ulid("trd_"),
            checkout_session_id=None,
            checkout_no_snapshot=new_prefixed_ulid("chk_"),
            checkout_snapshot_hash=hashlib.sha256(f"checkout-{suffix}".encode()).digest(),
            user_id=customer.id,
            order_source="buy_now",
            trade_status="paid",
            goods_amount=700,
            freight_amount=0,
            payable_amount=700,
            adjustment_amount=0,
            paid_amount=700,
            refunded_amount=0,
            currency="CNY",
            original_order_count=1,
            expires_at=now + timedelta(hours=1),
            paid_at=now,
        )
        session.add(trade)
        await session.flush()
        order = Order(
            order_no=new_prefixed_ulid("ord_"),
            trade_order_id=trade.id,
            user_id=customer.id,
            store_id=store.id,
            order_status="completed",
            payment_status="paid",
            fulfillment_status="received",
            after_sale_status="none",
            goods_amount=700,
            freight_amount=0,
            payable_amount=700,
            adjustment_amount=0,
            paid_amount=700,
            refunded_amount=0,
            currency="CNY",
            policy_snapshot={"version": 1},
            expires_at=now + timedelta(hours=1),
            paid_at=now,
            completed_at=now,
        )
        session.add(order)
        await session.flush()
        for index in range(7):
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
                unit_price_amount=100,
                market_price_amount=100,
                gross_amount=100,
                payable_amount=100,
                adjustment_amount=0,
                refunded_quantity=0,
                refunded_amount=0,
                currency="CNY",
                review_status="reviewed",
                after_sale_status="none",
            )
            session.add(item)
            await session.flush()
            session.add(
                Review(
                    review_no=new_prefixed_ulid("rev_"),
                    order_id=order.id,
                    order_item_id=item.id,
                    user_id=customer.id,
                    store_id=store.id,
                    product_id=product.id,
                    sku_id=sku.id,
                    rating=5,
                    content=f"游标评价 {index}",
                    review_status="published",
                    moderation_status="passed",
                    published_at=now - timedelta(minutes=index),
                )
            )

        operation_conversation = Conversation(
            conversation_no=new_prefixed_ulid("conv_"),
            user_id=operator.id,
            conversation_type="exclusive",
            is_fixed=True,
            conversation_status="active",
            last_sequence_no=0,
        )
        session.add(operation_conversation)
        for index in range(10):
            if index == 0:
                conversation_customer = customer
            else:
                conversation_customer = User(
                    user_no=new_prefixed_ulid("usr_"),
                    username=f"cursor_chat_{index}_{suffix}",
                    username_normalized=f"cursor_chat_{index}_{suffix}",
                    nickname=f"会话顾客 {index}",
                    user_status="active",
                    locale="zh-CN",
                    timezone="Asia/Shanghai",
                    permission_version=1,
                    registered_at=now,
                )
                session.add(conversation_customer)
                await session.flush()
            session.add(
                Conversation(
                    conversation_no=new_prefixed_ulid("conv_"),
                    user_id=conversation_customer.id,
                    store_id=store.id,
                    conversation_type="store",
                    is_fixed=False,
                    conversation_status="active",
                    last_sequence_no=0,
                    last_message_at=now - timedelta(minutes=index),
                )
            )
        await session.flush()

        context = cast(
            TrustedOperationsContext,
            SimpleNamespace(
                audience="merchant",
                user=operator,
                store=store,
                conversation=operation_conversation,
            ),
        )
        first_reviews = await _snapshot(
            session,
            context,
            "store_ops.reviews.list",
            query_text="列出本店评价",
        )
        first_review_ids = {
            str(item["review_id"])
            for item in cast(list[dict[str, object]], first_reviews["recent_reviews"])
        }
        assert len(first_review_ids) == 5
        review_pagination = cast(dict[str, object], first_reviews["pagination"])
        assert review_pagination["has_more"] is True
        await _persist_continuation(
            session,
            operation_conversation,
            "store_ops.reviews.list",
            review_pagination,
        )
        second_reviews = await _snapshot(
            session,
            context,
            "store_ops.reviews.list",
            query_text="下一页",
        )
        second_review_ids = {
            str(item["review_id"])
            for item in cast(list[dict[str, object]], second_reviews["recent_reviews"])
        }
        assert len(second_review_ids) == 2
        assert first_review_ids.isdisjoint(second_review_ids)
        assert cast(dict[str, object], second_reviews["pagination"])["has_more"] is False

        first_conversations = await _snapshot(
            session,
            context,
            "store_ops.conversations.list",
            query_text="列出顾客会话",
        )
        first_conversation_ids = {
            str(item["conversation_id"])
            for item in cast(list[dict[str, object]], first_conversations["conversations"])
        }
        assert len(first_conversation_ids) == 8
        conversation_pagination = cast(dict[str, object], first_conversations["pagination"])
        assert conversation_pagination["has_more"] is True
        await _persist_continuation(
            session,
            operation_conversation,
            "store_ops.conversations.list",
            conversation_pagination,
        )
        second_conversations = await _snapshot(
            session,
            context,
            "store_ops.conversations.list",
            query_text="下一页",
        )
        second_conversation_ids = {
            str(item["conversation_id"])
            for item in cast(list[dict[str, object]], second_conversations["conversations"])
        }
        assert len(second_conversation_ids) == 2
        assert first_conversation_ids.isdisjoint(second_conversation_ids)
        assert cast(dict[str, object], second_conversations["pagination"])["has_more"] is False
        await session.rollback()
        break
