"""move unpaid cancellations to user-only navigation records

Revision ID: ah38e1f2a3b4
Revises: ag27d0e1f2a3
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "ah38e1f2a3b4"
down_revision = "ag27d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cancelled_order_records",
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("trade_no", sa.String(length=32), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("cancellation_reason_code", sa.String(length=64), nullable=False),
        sa.Column("cancellation_reason", sa.String(length=500), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=False),
        sa.Column("search_text", sa.String(length=2000), nullable=False),
        sa.Column("snapshot_payload", mysql.JSON(), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_cancelled_order_records_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cancelled_order_records"),
        sa.UniqueConstraint("order_no", name="uk_cancelled_order_records_no"),
    )
    op.create_index(
        "idx_cancelled_order_records_user_time",
        "cancelled_order_records",
        ["user_id", "created_at", "id"],
        unique=False,
    )
    _archive_existing_unpaid_cancellations()


def _archive_existing_unpaid_cancellations() -> None:
    connection = op.get_bind()
    orders = connection.execute(
        sa.text(
            """
            SELECT o.id, o.order_no, o.user_id, o.order_status, o.payment_status,
                   o.fulfillment_status, o.after_sale_status, o.goods_amount,
                   o.freight_amount, o.adjustment_amount, o.payable_amount,
                   o.paid_amount, o.refunded_amount, o.currency, o.created_at,
                   o.expires_at, o.closed_at, o.version, t.trade_no, t.order_source,
                   s.store_no, s.store_name, s.logo_object_key
              FROM orders o
              JOIN trade_orders t ON t.id = o.trade_order_id
              JOIN stores s ON s.id = o.store_id
             WHERE o.order_status IN ('cancelled', 'closed')
               AND o.payment_status = 'unpaid' AND o.paid_amount = 0
             ORDER BY o.id
            """
        )
    ).mappings().all()
    order_ids: list[int] = []
    for order in orders:
        order_ids.append(int(order["id"]))
        item_rows = connection.execute(
            sa.text(
                """
                SELECT oi.order_item_no, oi.product_no, oi.sku_no, oi.product_name,
                       oi.sku_name, oi.spec_snapshot, oi.image_object_key, oi.quantity,
                       oi.unit_price_amount, oi.gross_amount, oi.payable_amount,
                       oi.refunded_amount, oi.refunded_quantity, oi.currency,
                       oi.review_status, oi.after_sale_status,
                       CASE WHEN p.deleted_at IS NULL AND p.product_status = 'on_sale'
                            THEN 1 ELSE 0 END AS product_available
                  FROM order_items oi
                  LEFT JOIN products p ON p.id = oi.product_id
                 WHERE oi.order_id = :order_id
                 ORDER BY oi.id
                """
            ),
            {"order_id": order["id"]},
        ).mappings().all()
        logo_url = _file_url(connection, order["logo_object_key"], thumbnail=False)
        items: list[dict[str, Any]] = []
        for item in item_rows:
            spec_snapshot = item["spec_snapshot"]
            if isinstance(spec_snapshot, str):
                spec_snapshot = json.loads(spec_snapshot)
            items.append(
                {
                    "order_item_id": item["order_item_no"],
                    "product_id": item["product_no"],
                    "product_available": bool(item["product_available"]),
                    "sku_id": item["sku_no"],
                    "product_name": item["product_name"],
                    "sku_name": item["sku_name"],
                    "spec_snapshot": spec_snapshot,
                    "image_url": _file_url(
                        connection, item["image_object_key"], thumbnail=True
                    ),
                    "quantity": int(item["quantity"]),
                    "unit_price": _money(item["unit_price_amount"], item["currency"]),
                    "gross_amount": _money(item["gross_amount"], item["currency"]),
                    "payable_amount": _money(item["payable_amount"], item["currency"]),
                    "refunded_amount": _money(item["refunded_amount"], item["currency"]),
                    "refunded_quantity": int(item["refunded_quantity"]),
                    "review_status": item["review_status"],
                    "after_sale_status": item["after_sale_status"],
                }
            )
        snapshot = _snapshot(order, items, logo_url)
        cancelled_at = order["closed_at"] or order["created_at"]
        search_text = " ".join(
            [
                order["order_no"],
                order["store_name"],
                *(str(item["product_name"]) for item in item_rows),
            ]
        )[:2000]
        connection.execute(
            sa.text(
                """
                INSERT INTO cancelled_order_records
                    (order_no, trade_no, user_id, cancellation_reason_code,
                     cancellation_reason, cancelled_at, search_text, snapshot_payload,
                     created_at, updated_at, version)
                VALUES
                    (:order_no, :trade_no, :user_id, 'legacy_cancelled',
                     '历史取消订单', :cancelled_at, :search_text, :snapshot_payload,
                     :created_at, :cancelled_at, 0)
                """
            ),
            {
                "order_no": order["order_no"],
                "trade_no": order["trade_no"],
                "user_id": order["user_id"],
                "cancelled_at": cancelled_at,
                "search_text": search_text,
                "snapshot_payload": json.dumps(snapshot, ensure_ascii=False),
                "created_at": order["created_at"],
            },
        )
    if not order_ids:
        return
    ids = ",".join(str(value) for value in order_ids)
    connection.execute(sa.text(f"DELETE FROM inventory_reservations WHERE order_id IN ({ids})"))
    connection.execute(sa.text(f"DELETE FROM order_addresses WHERE order_id IN ({ids})"))
    connection.execute(sa.text(f"DELETE FROM order_status_logs WHERE order_id IN ({ids})"))
    connection.execute(sa.text(f"DELETE FROM order_operation_logs WHERE order_id IN ({ids})"))
    connection.execute(sa.text(f"DELETE FROM order_items WHERE order_id IN ({ids})"))
    connection.execute(sa.text(f"DELETE FROM orders WHERE id IN ({ids})"))


def _file_url(connection: Any, object_key: str | None, *, thumbnail: bool) -> str | None:
    if not object_key:
        return None
    file_no = connection.execute(
        sa.text(
            "SELECT file_no FROM file_objects "
            "WHERE object_key = :object_key AND file_status = 'active' LIMIT 1"
        ),
        {"object_key": object_key},
    ).scalar_one_or_none()
    if not file_no:
        return None
    suffix = "?variant=thumbnail" if thumbnail else ""
    return f"/api/v1/files/{file_no}{suffix}"


def _money(amount: int, currency: str) -> dict[str, str]:
    return {"minor_units": str(amount), "currency": currency}


def _snapshot(order: Any, items: list[dict[str, Any]], logo_url: str | None) -> dict[str, Any]:
    currency = order["currency"]
    return {
        "order_id": order["order_no"],
        "trade_order_id": order["trade_no"],
        "order_source": order["order_source"],
        "store": {
            "store_id": order["store_no"],
            "store_name": order["store_name"],
            "logo_url": logo_url,
        },
        "order_status": "cancelled",
        "payment_status": "unpaid",
        "fulfillment_status": order["fulfillment_status"],
        "after_sale_status": "none",
        "matched_views": ["cancelled"],
        "items": items,
        "item_count": len(items),
        "total_quantity": sum(int(item["quantity"]) for item in items),
        "amounts": {
            "goods_amount": _money(order["goods_amount"], currency),
            "freight_amount": _money(order["freight_amount"], currency),
            "adjustment_amount": _money(order["adjustment_amount"], currency),
            "payable_amount": _money(order["payable_amount"], currency),
            "paid_amount": _money(0, currency),
            "refunded_amount": _money(0, currency),
        },
        "created_at": order["created_at"].isoformat(),
        "expires_at": order["expires_at"].isoformat(),
        "available_actions": [_delete_action(order["order_no"])],
        "version": 0,
    }


def _delete_action(order_no: str) -> dict[str, Any]:
    return {
        "code": "delete_order",
        "enabled": True,
        "reason_code": None,
        "reason_message": None,
        "requires_confirmation": True,
        "target": {"type": "route", "name": "my-orders", "params": {"orderId": order_no}},
    }


def downgrade() -> None:
    # CancelledOrderRecord is intentionally a user navigation snapshot, not a recoverable
    # transactional order. Recreating business orders from it would violate inventory and
    # payment invariants, so downgrade removes only the derived records and schema.
    op.drop_index("idx_cancelled_order_records_user_time", table_name="cancelled_order_records")
    op.drop_table("cancelled_order_records")
