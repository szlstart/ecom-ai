"""add review order foreign keys

Revision ID: f2c4a8d91e73
Revises: e7a4c2d91b63
Create Date: 2026-08-23 17:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2c4a8d91e73"
down_revision: str | Sequence[str] | None = "e7a4c2d91b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _validate_existing_references() -> None:
    connection = op.get_bind()
    invalid = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM reviews AS r
            LEFT JOIN orders AS o ON o.id = r.order_id
            LEFT JOIN order_items AS oi ON oi.id = r.order_item_id
            WHERE o.id IS NULL
               OR oi.id IS NULL
               OR oi.order_id <> r.order_id
            """
        )
    ).scalar_one()
    if invalid:
        raise RuntimeError(
            "Cannot add review order foreign keys: reviews contains "
            f"{invalid} orphaned or mismatched order references"
        )


def upgrade() -> None:
    _validate_existing_references()
    op.create_foreign_key(
        "fk_reviews_order_id_orders",
        "reviews",
        "orders",
        ["order_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_reviews_order_item_id_order_items",
        "reviews",
        "order_items",
        ["order_item_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_reviews_order_item_id_order_items",
        "reviews",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_reviews_order_id_orders",
        "reviews",
        type_="foreignkey",
    )
