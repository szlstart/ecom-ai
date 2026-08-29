from sqlalchemy import CheckConstraint, UniqueConstraint

from app.database.base import MySQLBase
from app.modules.cart import models as cart_models  # noqa: F401
from app.modules.checkout import models as checkout_models  # noqa: F401
from app.modules.orders import models as order_models  # noqa: F401


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


def test_checkout_schema_has_snapshot_and_lifecycle_guards() -> None:
    sessions = MySQLBase.metadata.tables["checkout_sessions"]
    snapshots = MySQLBase.metadata.tables["checkout_snapshots"]
    session_uniques = {
        item.name for item in sessions.constraints if isinstance(item, UniqueConstraint)
    }
    snapshot_uniques = {
        item.name for item in snapshots.constraints if isinstance(item, UniqueConstraint)
    }
    checks = {item.name for item in sessions.constraints if isinstance(item, CheckConstraint)}
    assert "uk_checkout_sessions_no" in session_uniques
    assert {"uk_checkout_snapshot_version", "uk_checkout_snapshot_hash"} <= snapshot_uniques
    assert {
        "ck_checkout_sessions_checkout_source_type",
        "ck_checkout_sessions_checkout_status",
    } <= checks


def test_order_schema_has_trade_split_snapshot_and_amount_guards() -> None:
    expected = {
        "trade_orders",
        "orders",
        "order_items",
        "order_addresses",
        "order_status_logs",
        "order_operation_logs",
        "cancelled_order_records",
    }
    assert expected <= set(MySQLBase.metadata.tables)
    trades = MySQLBase.metadata.tables["trade_orders"]
    orders = MySQLBase.metadata.tables["orders"]
    items = MySQLBase.metadata.tables["order_items"]
    cancelled_records = MySQLBase.metadata.tables["cancelled_order_records"]
    trade_uniques = {item.name for item in trades.constraints if isinstance(item, UniqueConstraint)}
    order_checks = {item.name for item in orders.constraints if isinstance(item, CheckConstraint)}
    item_checks = {item.name for item in items.constraints if isinstance(item, CheckConstraint)}
    cancelled_uniques = {
        item.name
        for item in cancelled_records.constraints
        if isinstance(item, UniqueConstraint)
    }
    assert {
        "uk_trade_orders_no",
        "uk_trade_orders_checkout",
        "uk_trade_orders_checkout_snapshot",
    } <= trade_uniques
    assert {
        "ck_orders_order_status",
        "ck_orders_order_amounts",
        "ck_orders_order_paid_refunded",
    } <= order_checks
    assert {
        "ck_order_items_order_item_gross",
        "ck_order_items_order_item_payable",
        "ck_order_items_order_item_refunded",
    } <= item_checks
    assert "uk_cancelled_order_records_no" in cancelled_uniques
