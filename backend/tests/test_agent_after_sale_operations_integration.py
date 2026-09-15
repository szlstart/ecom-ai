from __future__ import annotations

import hashlib
import os
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.bootstrap.ai_runtime import seed_ai_runtime
from app.bootstrap.merchant import provision_store_operator
from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.modules.after_sale.models import RefundApplication, RefundEvent
from app.modules.agent_runtime.models import AgentDefinition, AgentRun, AgentVersion
from app.modules.agent_runtime.operations_approval import (
    build_operations_approval,
    execute_operations_approval,
    prepare_operations_action,
)
from app.modules.agent_runtime.operations_context import MERCHANT_TOOLS, TrustedOperationsContext
from app.modules.catalog.models import Category, Product, ProductSku
from app.modules.identity.models import AuthSession, User
from app.modules.messaging.models import Conversation, Message
from app.modules.orders.models import Order, OrderItem, TradeOrder
from app.modules.stores.models import Store
from app.modules.system.models import OutboxEvent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_merchant_agent_requests_after_sale_materials_without_deciding_refund(
    client: AsyncClient,
) -> None:
    del client
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    async for session in mysql_session():
        await seed_ai_runtime(session)
        operator = await provision_store_operator(
            session,
            security,
            username=f"evidence_merchant_{suffix}",
            password=f"Evidence-{suffix}-Correct-Horse!",
            store_name=f"售后补证店铺 {suffix}",
        )
        merchant = await session.scalar(select(User).where(User.user_no == operator.user_no))
        store = await session.scalar(select(Store).where(Store.store_no == operator.store_no))
        definition = await session.scalar(
            select(AgentDefinition).where(AgentDefinition.agent_code == "merchant_copilot")
        )
        assert merchant is not None and store is not None and definition is not None
        version = await session.scalar(
            select(AgentVersion)
            .where(
                AgentVersion.agent_id == definition.id,
                AgentVersion.version_status == "published",
            )
            .order_by(AgentVersion.version_no.desc())
        )
        assert version is not None
        merchant_session = AuthSession(
            session_no=new_prefixed_ulid("ses_"),
            user_id=merchant.id,
            refresh_token_hash=security.keyed_hash("refresh-token", secrets.token_urlsafe()),
            token_family_no=new_prefixed_ulid("tfa_"),
            device_no=new_prefixed_ulid("dev_"),
            device_name="Agent after-sale integration",
            client_type="merchant",
            audience="admin",
            csrf_token_hash=security.keyed_hash("csrf-token", secrets.token_urlsafe()),
            authenticated_at=now,
            authentication_methods=["password"],
            assurance_level="aal1",
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
        )
        customer = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"evidence_customer_{suffix}",
            username_normalized=f"evidence_customer_{suffix}",
            nickname="补证顾客",
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            permission_version=1,
            registered_at=now,
        )
        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            category_name=f"售后测试分类 {suffix}",
            category_code=f"after-sale-{suffix}",
            path=f"/after-sale-{suffix}",
            level=1,
            sort_order=1,
            category_status="active",
        )
        session.add_all([merchant_session, customer, category])
        await session.flush()
        product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=store.id,
            category_id=category.id,
            product_name=f"售后测试商品 {suffix}",
            product_status="on_sale",
            min_price_amount=1290,
            max_price_amount=1290,
            currency="CNY",
            sales_count=1,
            review_count=0,
            rating_score=Decimal("0.00"),
            published_at=now,
        )
        session.add(product)
        await session.flush()
        sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=product.id,
            store_id=store.id,
            merchant_sku_code=f"EVIDENCE-{suffix}",
            sku_name="标准款",
            spec_values=[],
            spec_signature=hashlib.sha256(f"evidence-{suffix}".encode()).digest(),
            sale_price_amount=1290,
            market_price_amount=1290,
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
            goods_amount=1290,
            freight_amount=0,
            payable_amount=1290,
            adjustment_amount=0,
            paid_amount=1290,
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
            after_sale_status="in_progress",
            goods_amount=1290,
            freight_amount=0,
            payable_amount=1290,
            adjustment_amount=0,
            paid_amount=1290,
            refunded_amount=0,
            currency="CNY",
            policy_snapshot={"version": 1},
            expires_at=now + timedelta(hours=1),
            paid_at=now,
            completed_at=now,
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
            spec_snapshot=[],
            quantity=1,
            unit_price_amount=1290,
            market_price_amount=1290,
            gross_amount=1290,
            payable_amount=1290,
            adjustment_amount=0,
            refunded_quantity=0,
            refunded_amount=0,
            currency="CNY",
            review_status="pending",
            after_sale_status="in_progress",
        )
        session.add(order_item)
        await session.flush()
        refund = RefundApplication(
            refund_no=new_prefixed_ulid("ref_"),
            order_id=order.id,
            user_id=customer.id,
            store_id=store.id,
            refund_type="refund_only",
            refund_status="submitted",
            reason_code="DAMAGED",
            reason_detail="商品外观破损",
            requested_amount=1290,
            approved_amount=0,
            currency="CNY",
            policy_snapshot={"version": 1},
            submitted_at=now,
        )
        customer_conversation = Conversation(
            conversation_no=new_prefixed_ulid("conv_"),
            user_id=customer.id,
            store_id=store.id,
            conversation_type="store",
            is_fixed=False,
            conversation_status="active",
            last_sequence_no=0,
        )
        operator_conversation = Conversation(
            conversation_no=new_prefixed_ulid("conv_"),
            user_id=merchant.id,
            conversation_type="exclusive",
            is_fixed=True,
            conversation_status="active",
            last_sequence_no=1,
            last_message_at=now,
        )
        session.add_all([refund, customer_conversation, operator_conversation])
        await session.flush()
        trigger = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=operator_conversation.id,
            sequence_no=1,
            sender_type="user",
            sender_id=merchant.id,
            message_type="text",
            text_content=(
                f"要求售后 {refund.refund_no} 补充材料：商品破损照片和外包装照片"
            ),
            message_status="sent",
            moderation_status="passed",
            sent_at=now,
        )
        session.add(trigger)
        await session.flush()
        run = AgentRun(
            run_no=new_prefixed_ulid("run_"),
            conversation_id=operator_conversation.id,
            trigger_message_id=trigger.id,
            agent_version_id=version.id,
            run_status="running",
            current_phase="executing",
            trace_id=new_prefixed_ulid("trc_"),
            context_snapshot=[],
        )
        session.add(run)
        await session.flush()
        context = TrustedOperationsContext(
            run=run,
            conversation=operator_conversation,
            trigger=trigger,
            user=merchant,
            agent_definition=definition,
            agent_version=version,
            allowed_tools=MERCHANT_TOOLS,
            audience="merchant",
            store=store,
        )
        prepared, error = await prepare_operations_action(session, context, trigger.text_content or "")
        assert error is None and prepared is not None
        assert prepared.action_type == "merchant_refund_more_info"
        approval = await build_operations_approval(session, context, prepared)
        approval.approval_status = "approved"
        approval.decision = "approve"
        approval.decided_at = now
        approval.version += 1
        status, answer, result, error_code = await execute_operations_approval(
            session, context, approval
        )
        assert status == "succeeded" and error_code is None, (answer, result, error_code)
        assert "没有批准、拒绝或退款" in answer
        assert result["status"] == "merchant_review"
        await session.commit()
        refund_no = refund.refund_no
        customer_conversation_no = customer_conversation.conversation_no
        break

    async for session in mysql_session():
        persisted_refund = await session.scalar(
            select(RefundApplication).where(RefundApplication.refund_no == refund_no)
        )
        assert persisted_refund is not None
        assert persisted_refund.refund_status == "merchant_review"
        event = await session.scalar(
            select(RefundEvent).where(
                RefundEvent.refund_id == persisted_refund.id,
                RefundEvent.event_code == "more_info_requested",
            )
        )
        assert event is not None
        assert event.from_status == "submitted" and event.to_status == "merchant_review"
        persisted_conversation = await session.scalar(
            select(Conversation).where(Conversation.conversation_no == customer_conversation_no)
        )
        assert persisted_conversation is not None and persisted_conversation.last_sequence_no == 1
        notice = await session.scalar(
            select(Message).where(
                Message.conversation_id == persisted_conversation.id,
                Message.message_type == "system",
            )
        )
        assert notice is not None and notice.content_payload is not None
        assert notice.content_payload["event"] == "refund_more_info_requested"
        assert "商品破损照片和外包装照片" in str(notice.text_content)
        outbox_types = set(
            (
                await session.scalars(
                    select(OutboxEvent.event_type).where(
                        OutboxEvent.aggregate_no.in_(
                            [refund_no, customer_conversation_no]
                        )
                    )
                )
            ).all()
        )
        assert {"refund.more_info_requested.v1", "message.sent.v1"} <= outbox_types
        break
