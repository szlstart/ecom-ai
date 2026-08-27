import asyncio
import hashlib
import os
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.modules.cart.models import Cart, CartItem
from app.modules.catalog.models import Category, Product, ProductSku
from app.modules.identity.models import AuthSession, User
from app.modules.inventory.models import Inventory
from app.modules.rbac.models import Role, UserRole
from app.modules.stores.models import Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_permanent_cart_idempotency_etag_and_invalid_cleanup(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    now = utc_now()
    security = SecurityService(get_settings())
    async for session in mysql_session():
        user = User(
            user_no=new_prefixed_ulid("usr_"),
            username=f"cart_{suffix}",
            username_normalized=f"cart_{suffix}",
            nickname=f"Cart {suffix}",
            user_status="active",
            locale="zh-CN",
            timezone="Asia/Shanghai",
            permission_version=1,
            registered_at=now,
        )
        session.add(user)
        await session.flush()
        consumer_role = await session.scalar(select(Role).where(Role.role_code == "user"))
        assert consumer_role is not None
        session.add(
            UserRole(
                user_id=user.id,
                role_id=consumer_role.id,
                grant_no=new_prefixed_ulid("grt_"),
                scope_type="platform",
                scope_id=0,
                grant_status="active",
                active_grant_key=security.keyed_hash(
                    "active-role-grant", f"{user.id}:{consumer_role.id}:platform:0"
                ),
                granted_by=user.id,
                granted_at=now,
                grant_reason="cart_integration_consumer",
            )
        )
        auth_session = AuthSession(
            session_no=new_prefixed_ulid("ses_"),
            user_id=user.id,
            refresh_token_hash=security.keyed_hash("refresh-token", secrets.token_urlsafe()),
            token_family_no=new_prefixed_ulid("tfa_"),
            device_no=new_prefixed_ulid("dev_"),
            device_name="Cart integration",
            client_type="web",
            audience="user",
            csrf_token_hash=security.keyed_hash("csrf-token", secrets.token_urlsafe()),
            authenticated_at=now,
            authentication_methods=["password"],
            assurance_level="aal1",
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
        )
        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            category_name=f"购物车分类 {suffix}",
            category_code=f"cart-{suffix}",
            path="/cart",
            level=1,
            category_status="active",
        )
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=user.id,
            store_name=f"购物车店铺 {suffix}",
            store_name_normalized=f"cart-store-{suffix}",
            store_status="active",
            rating_score=Decimal("0.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=now,
        )
        session.add_all([auth_session, category, store])
        await session.flush()
        product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=store.id,
            category_id=category.id,
            product_name=f"购物车商品 {suffix}",
            product_status="on_sale",
            min_price_amount=2500,
            max_price_amount=2500,
            currency="CNY",
            rating_score=Decimal("0.00"),
            published_at=now,
        )
        session.add(product)
        await session.flush()
        sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=product.id,
            store_id=store.id,
            merchant_sku_code=f"CART-{suffix}",
            sku_name="标准款",
            spec_values=[{"name": "规格", "value": "标准"}],
            spec_signature=hashlib.sha256(suffix.encode()).digest(),
            sale_price_amount=2500,
            market_price_amount=3000,
            currency="CNY",
            sku_status="active",
        )
        session.add(sku)
        await session.flush()
        session.add(Inventory(sku_id=sku.id, on_hand_quantity=10, inventory_status="active"))
        await session.commit()
        user_no, user_id, session_no, sku_no, product_id = (
            user.user_no,
            user.id,
            auth_session.session_no,
            sku.sku_no,
            product.id,
        )

    token, _ = security.create_access_token(
        user_no=user_no, session_no=session_no, audience="user", permission_version=1
    )
    auth = {"Authorization": f"Bearer {token}"}
    empty = await client.get("/api/v1/users/me/cart", headers=auth)
    assert empty.status_code == 200
    assert empty.json()["data"]["cart_id"] is None
    assert empty.headers["etag"] == '"v0"'
    invalid_public_id = await client.patch(
        "/api/v1/users/me/cart/items/1",
        headers={**auth, "If-Match": '"v0"'},
        json={"quantity": 1},
    )
    assert invalid_public_id.status_code == 422

    added = await client.post(
        "/api/v1/users/me/cart/items",
        headers={**auth, "Idempotency-Key": f"cart-add-{suffix}-0001"},
        json={"sku_id": sku_no, "quantity": 2},
    )
    assert added.status_code == 200, added.text
    assert added.json()["data"]["cart_total_quantity"] == 2
    public_item = added.json()["data"]["groups"][0]["items"][0]
    assert "spec_values" not in public_item
    item_id = public_item["cart_item_id"]
    assert item_id.startswith("ci_")

    replayed = await client.post(
        "/api/v1/users/me/cart/items",
        headers={**auth, "Idempotency-Key": f"cart-add-{suffix}-0001"},
        json={"sku_id": sku_no, "quantity": 2},
    )
    assert replayed.status_code == 200
    assert replayed.json()["data"]["cart_total_quantity"] == 2

    patched = await client.patch(
        f"/api/v1/users/me/cart/items/{item_id}",
        headers={**auth, "If-Match": added.headers["etag"]},
        json={"quantity": 3, "is_selected": False},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["cart_total_quantity"] == 3
    assert patched.json()["data"]["selected_quantity"] == 0

    late_replay = await client.post(
        "/api/v1/users/me/cart/items",
        headers={**auth, "Idempotency-Key": f"cart-add-{suffix}-0001"},
        json={"sku_id": sku_no, "quantity": 2},
    )
    assert late_replay.status_code == 200
    assert late_replay.json()["data"]["cart_total_quantity"] == 2

    stale = await client.patch(
        f"/api/v1/users/me/cart/items/{item_id}",
        headers={**auth, "If-Match": added.headers["etag"]},
        json={"quantity": 4},
    )
    assert stale.status_code == 412

    concurrent = await asyncio.gather(
        client.post(
            "/api/v1/users/me/cart/items",
            headers={**auth, "Idempotency-Key": f"cart-concurrent-{suffix}-0001"},
            json={"sku_id": sku_no, "quantity": 1},
        ),
        client.post(
            "/api/v1/users/me/cart/items",
            headers={**auth, "Idempotency-Key": f"cart-concurrent-{suffix}-0002"},
            json={"sku_id": sku_no, "quantity": 1},
        ),
    )
    assert all(response.status_code == 200 for response in concurrent)
    after_concurrent = await client.get("/api/v1/users/me/cart", headers=auth)
    assert after_concurrent.json()["data"]["cart_total_quantity"] == 5

    async for session in mysql_session():
        changed_product = await session.get(Product, product_id)
        assert changed_product is not None
        changed_product.product_status = "off_shelf"
        changed_product.version += 1
        await session.commit()
    invalid = await client.get("/api/v1/users/me/cart", headers=auth)
    assert invalid.json()["data"]["groups"][0]["items"][0]["invalid_reason"] == (
        "PRODUCT_OFF_SHELF"
    )
    cleared = await client.delete(
        "/api/v1/users/me/cart/invalid-items",
        headers={**auth, "If-Match": invalid.headers["etag"]},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["groups"] == []

    async for session in mysql_session():
        carts = list((await session.scalars(select(Cart).where(Cart.user_id == user_id))).all())
        items = list(
            (await session.scalars(select(CartItem).where(CartItem.cart_id == carts[0].id))).all()
        )
        assert len(carts) == 1
        assert carts[0].item_count == 0
        assert items == []
