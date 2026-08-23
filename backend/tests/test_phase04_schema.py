from sqlalchemy import CheckConstraint, UniqueConstraint

from app.database.base import MySQLBase
from app.modules.cart import models as cart_models  # noqa: F401


def test_permanent_cart_schema_has_public_ids_and_database_guards() -> None:
    carts = MySQLBase.metadata.tables["carts"]
    items = MySQLBase.metadata.tables["cart_items"]
    cart_uniques = {
        constraint.name
        for constraint in carts.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    item_uniques = {
        constraint.name
        for constraint in items.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    item_checks = {
        constraint.name
        for constraint in items.constraints
        if isinstance(constraint, CheckConstraint)
    }
    cart_checks = {
        constraint.name
        for constraint in carts.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert {"uk_carts_no", "uk_carts_user"} <= cart_uniques
    assert {"uk_cart_items_no", "uk_cart_items_cart_sku"} <= item_uniques
    assert "ck_cart_items_cart_item_quantity_range" in item_checks
    assert "ck_carts_cart_status_active" in cart_checks
