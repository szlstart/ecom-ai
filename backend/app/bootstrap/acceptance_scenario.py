from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.admin import provision_platform_super_admin
from app.bootstrap.merchant import provision_store_operator
from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.core.testing_safety import validate_integration_test_environment
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.modules.catalog.models import Category, Product, ProductFulfillmentProfile, ProductSku
from app.modules.finance.models import UserWallet
from app.modules.identity.models import User, UserAddress, UserCredential
from app.modules.inventory.models import Inventory
from app.modules.messaging.models import Conversation
from app.modules.rbac.models import Role, UserRole
from app.modules.stores.models import ShippingTemplate, ShippingTemplateRule, Store

SCENARIO_VERSION = "commerce-three-portal-v1"
CONSUMER_USERNAME = "acceptance_user"
MERCHANT_USERNAME = "acceptance_merchant"
ADMIN_USERNAME = "acceptance_admin"
TEST_PASSWORD = "Acceptance-only-password-2026!"
STORE_NAME = "验收文具店"
PRODUCT_NAME = "三端联动验收笔记本"
MERCHANT_SKU_CODE = "ACCEPTANCE-NOTEBOOK-V1"


@dataclass(frozen=True)
class AcceptanceScenario:
    scenario_version: str
    consumer_username: str
    consumer_user_id: str
    merchant_username: str
    merchant_user_id: str
    administrator_username: str
    administrator_user_id: str
    store_id: str
    product_id: str
    sku_id: str
    address_id: str


async def seed_acceptance_scenario(session: AsyncSession) -> AcceptanceScenario:
    settings = get_settings()
    validate_integration_test_environment(settings, file_integration_enabled=False)
    security = SecurityService(settings)

    existing_consumer = await session.scalar(
        select(User).where(User.username_normalized == CONSUMER_USERNAME)
    )
    existing_merchant = await session.scalar(
        select(User).where(User.username_normalized == MERCHANT_USERNAME)
    )
    existing_admin = await session.scalar(
        select(User).where(User.username_normalized == ADMIN_USERNAME)
    )
    if any(item is not None for item in (existing_consumer, existing_merchant, existing_admin)):
        existing_identities = (existing_consumer, existing_merchant, existing_admin)
        if not all(item is not None for item in existing_identities):
            raise RuntimeError("acceptance scenario is incomplete; recreate the isolated database")
        return await _existing_scenario(
            session,
            consumer=existing_consumer,
            merchant=existing_merchant,
            administrator=existing_admin,
        )

    administrator = await provision_platform_super_admin(
        session,
        security,
        username=ADMIN_USERNAME,
        password=TEST_PASSWORD,
    )
    merchant = await provision_store_operator(
        session,
        security,
        username=MERCHANT_USERNAME,
        password=TEST_PASSWORD,
        store_name=STORE_NAME,
    )
    consumer, address = await _create_consumer(session, security)
    store = await session.scalar(select(Store).where(Store.store_no == merchant.store_no))
    if store is None:
        raise RuntimeError("acceptance merchant store was not created")
    product, sku = await _create_product(session, store)
    await session.commit()
    return AcceptanceScenario(
        scenario_version=SCENARIO_VERSION,
        consumer_username=consumer.username,
        consumer_user_id=consumer.user_no,
        merchant_username=merchant.username,
        merchant_user_id=merchant.user_no,
        administrator_username=ADMIN_USERNAME,
        administrator_user_id=administrator.user_no,
        store_id=store.store_no,
        product_id=product.product_no,
        sku_id=sku.sku_no,
        address_id=address.address_no,
    )


async def _create_consumer(
    session: AsyncSession,
    security: SecurityService,
) -> tuple[User, UserAddress]:
    now = utc_now()
    consumer = User(
        user_no=new_prefixed_ulid("usr_"),
        username=CONSUMER_USERNAME,
        username_normalized=CONSUMER_USERNAME,
        nickname="验收用户",
        user_status="active",
        locale="zh-CN",
        timezone="Asia/Shanghai",
        permission_version=1,
        registered_at=now,
    )
    session.add(consumer)
    await session.flush()
    email = "acceptance-user@example.test"
    session.add_all(
        [
            UserCredential(
                user_id=consumer.id,
                credential_type="password",
                secret_hash=security.hash_password(TEST_PASSWORD),
                algorithm="argon2id",
                is_primary=True,
                is_verified=True,
                verified_at=now,
                password_changed_at=now,
                credential_status="active",
            ),
            UserCredential(
                user_id=consumer.id,
                credential_type="email",
                identifier_ciphertext=security.encrypt("user-credential:email", email),
                identifier_hash=security.keyed_hash("credential:email", email),
                key_version=1,
                is_primary=True,
                is_verified=True,
                verified_at=now,
                credential_status="active",
            ),
            UserWallet(
                wallet_no=new_prefixed_ulid("wal_"),
                user_id=consumer.id,
                balance_amount=200_000,
                total_recharged_amount=200_000,
                currency="CNY",
                wallet_status="active",
            ),
            Conversation(
                conversation_no=new_prefixed_ulid("con_"),
                user_id=consumer.id,
                conversation_type="exclusive",
                is_fixed=True,
                conversation_status="active",
            ),
        ]
    )
    role = await session.scalar(select(Role).where(Role.role_code == "user"))
    if role is None:
        raise RuntimeError("consumer role is not seeded")
    session.add(
        UserRole(
            user_id=consumer.id,
            role_id=role.id,
            grant_no=new_prefixed_ulid("grt_"),
            scope_type="platform",
            scope_id=0,
            grant_status="active",
            active_grant_key=security.keyed_hash(
                "active-role-grant",
                f"{consumer.id}:{role.id}:platform:0",
            ),
            granted_by=consumer.id,
            granted_at=now,
            grant_reason="acceptance_scenario_v1",
        )
    )
    address = UserAddress(
        address_no=new_prefixed_ulid("addr_"),
        user_id=consumer.id,
        recipient_name_ciphertext=security.encrypt("address-recipient", "验收用户"),
        phone_ciphertext=security.encrypt("address-phone", "+8613812345678"),
        phone_last4="5678",
        country_code="CN",
        province_code="CN-44",
        city_code="CN-4401",
        district_code="CN-440106",
        address_ciphertext=security.encrypt("address-detail", "验收路 1 号"),
        is_default=True,
        key_version=1,
    )
    session.add(address)
    await session.flush()
    return consumer, address


async def _create_product(
    session: AsyncSession,
    store: Store,
) -> tuple[Product, ProductSku]:
    category = await session.scalar(
        select(Category)
        .where(Category.category_status == "active")
        .order_by(Category.id)
        .limit(1)
    )
    if category is None:
        raise RuntimeError("reference category is not seeded")
    now = utc_now()
    shipping = ShippingTemplate(
        template_no=new_prefixed_ulid("sht_"),
        template_family_no=new_prefixed_ulid("sht_"),
        store_id=store.id,
        template_name="验收包邮",
        delivery_type="express",
        charge_mode="item",
        currency="CNY",
        template_status="effective",
        dispatch_min_hours=1,
        dispatch_max_hours=24,
        policy_version=1,
    )
    product = Product(
        product_no=new_prefixed_ulid("prd_"),
        store_id=store.id,
        category_id=category.id,
        product_name=PRODUCT_NAME,
        description="固定的三端商城验收商品，只存在于隔离测试数据库。",
        product_status="on_sale",
        min_price_amount=1_299,
        max_price_amount=1_299,
        currency="CNY",
        rating_score=Decimal("5.00"),
        published_at=now,
    )
    session.add_all([shipping, product])
    await session.flush()
    session.add_all(
        [
            ShippingTemplateRule(
                shipping_template_id=shipping.id,
                region_scope={"include": ["CN"]},
                first_unit=1,
                additional_unit=1,
                first_fee_amount=0,
                additional_fee_amount=0,
                estimated_min_days=1,
                estimated_max_days=3,
                rule_status="active",
            ),
            ProductFulfillmentProfile(
                product_id=product.id,
                shipping_template_id=shipping.id,
                origin_region_code="CN-44",
                dispatch_min_hours=1,
                dispatch_max_hours=24,
                profile_version=1,
            ),
        ]
    )
    sku = ProductSku(
        sku_no=new_prefixed_ulid("sku_"),
        product_id=product.id,
        store_id=store.id,
        merchant_sku_code=MERCHANT_SKU_CODE,
        sku_name="墨绿色",
        spec_values=[{"name": "款式", "value": "墨绿色"}],
        spec_signature=hashlib.sha256(MERCHANT_SKU_CODE.encode()).digest(),
        sale_price_amount=1_299,
        market_price_amount=1_299,
        currency="CNY",
        weight_grams=300,
        sku_status="active",
    )
    session.add(sku)
    await session.flush()
    product.default_sku_id = sku.id
    session.add(Inventory(sku_id=sku.id, on_hand_quantity=50, inventory_status="active"))
    return product, sku


async def _existing_scenario(
    session: AsyncSession,
    *,
    consumer: User | None,
    merchant: User | None,
    administrator: User | None,
) -> AcceptanceScenario:
    assert consumer is not None and merchant is not None and administrator is not None
    store = await session.scalar(select(Store).where(Store.owner_user_id == merchant.id))
    product = await session.scalar(
        select(Product)
        .join(Store, Store.id == Product.store_id)
        .where(Store.owner_user_id == merchant.id, Product.product_name == PRODUCT_NAME)
    )
    sku = await session.scalar(
        select(ProductSku).where(ProductSku.merchant_sku_code == MERCHANT_SKU_CODE)
    )
    address = await session.scalar(
        select(UserAddress).where(UserAddress.user_id == consumer.id, UserAddress.is_default)
    )
    if store is None or product is None or sku is None or address is None:
        raise RuntimeError("acceptance scenario is incomplete; recreate the isolated database")
    return AcceptanceScenario(
        scenario_version=SCENARIO_VERSION,
        consumer_username=consumer.username,
        consumer_user_id=consumer.user_no,
        merchant_username=merchant.username,
        merchant_user_id=merchant.user_no,
        administrator_username=administrator.username,
        administrator_user_id=administrator.user_no,
        store_id=store.store_no,
        product_id=product.product_no,
        sku_id=sku.sku_no,
        address_id=address.address_no,
    )


async def run() -> AcceptanceScenario:
    settings = get_settings()
    validate_integration_test_environment(settings, file_integration_enabled=False)
    initialize_mysql(settings.mysql_dsn)
    try:
        async for session in mysql_session():
            scenario = await seed_acceptance_scenario(session)
            return scenario
    finally:
        await close_mysql()
    raise RuntimeError("MySQL session factory did not yield a session")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the isolated three-portal scenario")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("../artifacts/acceptance/current/scenario.json"),
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    created = asyncio.run(run())
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(asdict(created), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"scenario_version": created.scenario_version, "ready": True}))
