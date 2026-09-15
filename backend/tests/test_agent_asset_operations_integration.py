from __future__ import annotations

import hashlib
import os
import secrets
from decimal import Decimal

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.bootstrap.admin import provision_platform_super_admin
from app.bootstrap.ai_runtime import seed_ai_runtime
from app.bootstrap.merchant import provision_store_operator
from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.modules.agent_runtime.models import AgentDefinition, AgentRun, AgentVersion
from app.modules.agent_runtime.operations_approval import (
    build_operations_approval,
    execute_operations_approval,
    prepare_operations_action,
)
from app.modules.agent_runtime.operations_context import (
    ADMIN_TOOLS,
    MERCHANT_TOOLS,
    TrustedOperationsContext,
)
from app.modules.catalog.models import Category, Product, ProductImage, ProductSku
from app.modules.files.models import FileObject
from app.modules.identity.models import User
from app.modules.messaging.models import Conversation, Message
from app.modules.stores.models import Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_merchant_agent_asset_confirmation_replaces_only_selected_sku_image(
    client: AsyncClient,
) -> None:
    del client
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    username = f"asset_merchant_{suffix}"

    async for session in mysql_session():
        await seed_ai_runtime(session)
        operator = await provision_store_operator(
            session,
            security,
            username=username,
            password=f"Asset-{suffix}-Correct-Horse!",
            store_name=f"图片 Agent 店铺 {suffix}",
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
        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            category_name=f"图片分类 {suffix}",
            category_code=f"asset-{suffix}",
            path=f"/asset-{suffix}",
            level=1,
            sort_order=1,
            category_status="active",
        )
        session.add(category)
        await session.flush()
        product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=store.id,
            category_id=category.id,
            product_name=f"图片闭环商品 {suffix}",
            product_status="on_sale",
            min_price_amount=1200,
            max_price_amount=1500,
            currency="CNY",
            sales_count=0,
            review_count=0,
            rating_score=Decimal("0.00"),
            published_at=now,
        )
        session.add(product)
        await session.flush()
        sku_one = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=product.id,
            store_id=store.id,
            merchant_sku_code=f"ASSET-A-{suffix}",
            sku_name="蓝色款",
            spec_values=[{"name": "颜色", "value": "蓝色"}],
            spec_signature=hashlib.sha256(f"blue-{suffix}".encode()).digest(),
            sale_price_amount=1200,
            market_price_amount=1200,
            currency="CNY",
            sku_status="active",
        )
        sku_two = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=product.id,
            store_id=store.id,
            merchant_sku_code=f"ASSET-B-{suffix}",
            sku_name="红色款",
            spec_values=[{"name": "颜色", "value": "红色"}],
            spec_signature=hashlib.sha256(f"red-{suffix}".encode()).digest(),
            sale_price_amount=1500,
            market_price_amount=1500,
            currency="CNY",
            sku_status="active",
        )
        session.add_all([sku_one, sku_two])
        await session.flush()

        scoped_store_no = store.store_no

        def image_file(name: str, owner_no: str = scoped_store_no) -> FileObject:
            return FileObject(
                file_no=new_prefixed_ulid("file_"),
                bucket="public-assets",
                object_key=f"products/{owner_no}/{name}-{suffix}.webp",
                purpose="product",
                owner_type="store",
                owner_no=owner_no,
                declared_mime_type="image/webp",
                detected_mime_type="image/webp",
                size_bytes=4096,
                sha256=hashlib.sha256(f"{name}-{suffix}".encode()).digest(),
                width=1200,
                height=1200,
                visibility="public_derivative",
                sensitivity_level="S1",
                scan_status="safe",
                file_status="active",
                activated_at=now,
            )

        old_one = image_file("old-one")
        old_two = image_file("old-two")
        replacement = image_file("replacement")
        session.add_all([old_one, old_two, replacement])
        await session.flush()
        session.add_all(
            [
                ProductImage(
                    product_id=product.id,
                    sku_id=sku_one.id,
                    file_id=old_one.id,
                    object_key=old_one.object_key,
                    image_type="spec",
                    alt_text="蓝色款原图",
                    width=1200,
                    height=1200,
                    sort_order=0,
                    image_status="active",
                ),
                ProductImage(
                    product_id=product.id,
                    sku_id=sku_two.id,
                    file_id=old_two.id,
                    object_key=old_two.object_key,
                    image_type="spec",
                    alt_text="红色款原图",
                    width=1200,
                    height=1200,
                    sort_order=0,
                    image_status="active",
                ),
            ]
        )
        conversation = Conversation(
            conversation_no=new_prefixed_ulid("cv_"),
            user_id=merchant.id,
            conversation_type="exclusive",
            is_fixed=True,
            conversation_status="active",
            last_sequence_no=1,
            last_message_at=now,
        )
        session.add(conversation)
        await session.flush()
        trigger = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=1,
            sender_type="user",
            sender_id=merchant.id,
            message_type="agent_asset",
            content_payload={
                "type": "agent_asset",
                "purpose": "product_sku_image",
                "file_id": replacement.file_no,
                "store_id": store.store_no,
                "store_name": store.store_name,
                "product_id": product.product_no,
                "product_name": product.product_name,
                "sku_id": sku_one.sku_no,
                "sku_name": sku_one.sku_name,
                "image_url": f"/api/v1/files/{replacement.file_no}",
            },
            message_status="sent",
            moderation_status="passed",
            sent_at=now,
        )
        session.add(trigger)
        await session.flush()
        run = AgentRun(
            run_no=new_prefixed_ulid("run_"),
            conversation_id=conversation.id,
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
            conversation=conversation,
            trigger=trigger,
            user=merchant,
            agent_definition=definition,
            agent_version=version,
            allowed_tools=MERCHANT_TOOLS,
            audience="merchant",
            store=store,
        )
        prepared, error = await prepare_operations_action(session, context, "")
        assert error is None and prepared is not None
        assert prepared.action_type == "merchant_product_sku_image_replace"
        assert prepared.tool_code == "store_ops.catalog.skus.image.replace.commit"
        approval = await build_operations_approval(session, context, prepared)
        approval.approval_status = "approved"
        approval.decision = "approve"
        approval.decided_at = utc_now()
        approval.version += 1
        status, answer, result, error_code = await execute_operations_approval(
            session, context, approval
        )
        assert status == "succeeded" and error_code is None, (answer, result, error_code)
        assert result["sku_id"] == sku_one.sku_no
        assert result["image_count"] == 1
        assert "款式图片已替换" in answer
        await session.commit()
        product_no = product.product_no
        sku_one_no = sku_one.sku_no
        sku_two_no = sku_two.sku_no
        replacement_no = replacement.file_no
        old_two_no = old_two.file_no
        break

    async for session in mysql_session():
        rows = list(
            (
                await session.execute(
                    select(ProductSku.sku_no, FileObject.file_no)
                    .join(ProductImage, ProductImage.sku_id == ProductSku.id)
                    .join(FileObject, FileObject.id == ProductImage.file_id)
                    .join(Product, Product.id == ProductSku.product_id)
                    .where(Product.product_no == product_no)
                    .order_by(ProductSku.sku_no)
                )
            ).all()
        )
        assert sorted(rows) == sorted(
            [(sku_one_no, replacement_no), (sku_two_no, old_two_no)]
        )
        break


async def test_merchant_agent_asset_rejects_cross_store_target(client: AsyncClient) -> None:
    del client
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    async for session in mysql_session():
        await seed_ai_runtime(session)
        operator = await provision_store_operator(
            session,
            security,
            username=f"asset_scope_a_{suffix}",
            password=f"Asset-A-{suffix}-Correct-Horse!",
            store_name=f"图片归属店铺 A {suffix}",
        )
        foreign = await provision_store_operator(
            session,
            security,
            username=f"asset_scope_b_{suffix}",
            password=f"Asset-B-{suffix}-Correct-Horse!",
            store_name=f"图片归属店铺 B {suffix}",
        )
        merchant = await session.scalar(select(User).where(User.user_no == operator.user_no))
        store = await session.scalar(select(Store).where(Store.store_no == operator.store_no))
        foreign_store = await session.scalar(
            select(Store).where(Store.store_no == foreign.store_no)
        )
        definition = await session.scalar(
            select(AgentDefinition).where(AgentDefinition.agent_code == "merchant_copilot")
        )
        assert all((merchant, store, foreign_store, definition))
        assert definition is not None and merchant is not None and store is not None
        assert foreign_store is not None
        version = await session.scalar(
            select(AgentVersion)
            .where(
                AgentVersion.agent_id == definition.id,
                AgentVersion.version_status == "published",
            )
            .order_by(AgentVersion.version_no.desc())
        )
        assert version is not None
        image = FileObject(
            file_no=new_prefixed_ulid("file_"),
            bucket="public-assets",
            object_key=f"stores/{foreign_store.store_no}/logo-{suffix}.webp",
            purpose="store_logo",
            owner_type="store",
            owner_no=foreign_store.store_no,
            declared_mime_type="image/webp",
            detected_mime_type="image/webp",
            size_bytes=2048,
            sha256=hashlib.sha256(f"foreign-{suffix}".encode()).digest(),
            width=600,
            height=600,
            visibility="public_derivative",
            sensitivity_level="S1",
            scan_status="safe",
            file_status="active",
            activated_at=now,
        )
        session.add(image)
        conversation = Conversation(
            conversation_no=new_prefixed_ulid("cv_"),
            user_id=merchant.id,
            conversation_type="exclusive",
            is_fixed=True,
            conversation_status="active",
            last_sequence_no=1,
            last_message_at=now,
        )
        session.add(conversation)
        await session.flush()
        trigger = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=1,
            sender_type="user",
            sender_id=merchant.id,
            message_type="agent_asset",
            content_payload={
                "type": "agent_asset",
                "purpose": "store_logo",
                "file_id": image.file_no,
                "store_id": foreign_store.store_no,
            },
            message_status="sent",
            moderation_status="passed",
            sent_at=now,
        )
        session.add(trigger)
        await session.flush()
        run = AgentRun(
            run_no=new_prefixed_ulid("run_"),
            conversation_id=conversation.id,
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
            conversation=conversation,
            trigger=trigger,
            user=merchant,
            agent_definition=definition,
            agent_version=version,
            allowed_tools=MERCHANT_TOOLS,
            audience="merchant",
            store=store,
        )
        prepared, error = await prepare_operations_action(session, context, "")
        assert prepared is None
        assert error == "只能修改当前商家自己的店铺图片。"
        await session.rollback()
        break


async def test_admin_agent_asset_confirmation_updates_only_selected_user_avatar(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    admin_name = f"asset_admin_{suffix}"
    async for session in mysql_session():
        await seed_ai_runtime(session)
        provisioned = await provision_platform_super_admin(
            session,
            security,
            username=admin_name,
            password=f"Asset-Admin-{suffix}-Correct-Horse!",
        )
        login = await client.post(
            "/api/v1/admin/auth/login",
            json={
                "identifier": admin_name,
                "password": f"Asset-Admin-{suffix}-Correct-Horse!",
                "client": {"client_type": "web", "device_name": "Agent avatar test"},
            },
        )
        assert login.status_code == 200, login.text
        mfa = await client.post(
            "/api/v1/admin/auth/mfa-verifications",
            headers={"Idempotency-Key": f"asset-avatar-mfa-{suffix}"},
            json={
                "challenge_id": login.json()["data"]["challenge_id"],
                "method": "totp",
                "code": pyotp.TOTP(provisioned.totp_secret).now(),
            },
        )
        assert mfa.status_code == 200, mfa.text
        admin = await session.scalar(select(User).where(User.user_no == provisioned.user_no))
        definition = await session.scalar(
            select(AgentDefinition).where(AgentDefinition.agent_code == "admin_copilot")
        )
        assert admin is not None and definition is not None
        version = await session.scalar(
            select(AgentVersion)
            .where(
                AgentVersion.agent_id == definition.id,
                AgentVersion.version_status == "published",
            )
            .order_by(AgentVersion.version_no.desc())
        )
        assert version is not None
        target = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"avatar_target_{suffix}",
            username_normalized=f"avatar_target_{suffix}",
            nickname=f"avatar_target_{suffix}",
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            registered_at=now,
        )
        other = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"avatar_other_{suffix}",
            username_normalized=f"avatar_other_{suffix}",
            nickname=f"avatar_other_{suffix}",
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            registered_at=now,
        )
        session.add_all([target, other])
        await session.flush()
        avatar = FileObject(
            file_no=new_prefixed_ulid("file_"),
            bucket="public-assets",
            object_key=f"users/{target.user_no}/avatar-{suffix}.webp",
            purpose="user_avatar",
            owner_type="user",
            owner_no=target.user_no,
            declared_mime_type="image/webp",
            detected_mime_type="image/webp",
            size_bytes=2048,
            sha256=hashlib.sha256(f"avatar-{suffix}".encode()).digest(),
            width=600,
            height=600,
            visibility="public_derivative",
            sensitivity_level="S1",
            scan_status="safe",
            file_status="active",
            activated_at=now,
        )
        session.add(avatar)
        conversation = Conversation(
            conversation_no=new_prefixed_ulid("cv_"),
            user_id=admin.id,
            conversation_type="exclusive",
            is_fixed=True,
            conversation_status="active",
            last_sequence_no=1,
            last_message_at=now,
        )
        session.add(conversation)
        await session.flush()
        trigger = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=1,
            sender_type="user",
            sender_id=admin.id,
            message_type="agent_asset",
            content_payload={
                "type": "agent_asset",
                "purpose": "user_avatar",
                "file_id": avatar.file_no,
                "user_id": target.user_no,
                "username": target.username,
                "image_url": f"/api/v1/files/{avatar.file_no}",
            },
            message_status="sent",
            moderation_status="passed",
            sent_at=now,
        )
        session.add(trigger)
        await session.flush()
        run = AgentRun(
            run_no=new_prefixed_ulid("run_"),
            conversation_id=conversation.id,
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
            conversation=conversation,
            trigger=trigger,
            user=admin,
            agent_definition=definition,
            agent_version=version,
            allowed_tools=ADMIN_TOOLS,
            audience="admin",
            store=None,
        )
        prepared, error = await prepare_operations_action(session, context, "")
        assert error is None and prepared is not None
        assert prepared.action_type == "admin_user_avatar_update"
        assert prepared.tool_code == "governance.users.avatar.update.commit"
        approval = await build_operations_approval(session, context, prepared)
        approval.approval_status = "approved"
        approval.decision = "approve"
        approval.decided_at = utc_now()
        approval.version += 1
        status, answer, result, error_code = await execute_operations_approval(
            session, context, approval
        )
        assert status == "succeeded" and error_code is None, (answer, result, error_code)
        assert result["user_id"] == target.user_no
        assert "头像已更新" in answer
        await session.commit()
        target_no = target.user_no
        other_no = other.user_no
        avatar_key = avatar.object_key
        break

    async for session in mysql_session():
        updated = await session.scalar(select(User).where(User.user_no == target_no))
        untouched = await session.scalar(select(User).where(User.user_no == other_no))
        assert updated is not None and updated.avatar_object_key == avatar_key
        assert untouched is not None and untouched.avatar_object_key is None
        break
