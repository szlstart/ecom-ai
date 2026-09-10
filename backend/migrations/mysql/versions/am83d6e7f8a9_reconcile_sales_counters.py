"""Reconcile denormalized product and store sales counters.

Revision ID: am83d6e7f8a9
Revises: al72c5d6e7f8
"""

from collections.abc import Sequence

from alembic import op

revision: str = "am83d6e7f8a9"
down_revision: str | Sequence[str] | None = "al72c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE products AS product
        SET product.sales_count = (
            SELECT COALESCE(SUM(inventory.sold_quantity), 0)
            FROM product_skus AS sku
            JOIN inventories AS inventory ON inventory.sku_id = sku.id
            WHERE sku.product_id = product.id
        )
        """
    )
    op.execute(
        """
        UPDATE stores AS store
        SET store.sales_count = (
            SELECT COALESCE(SUM(product.sales_count), 0)
            FROM products AS product
            WHERE product.store_id = store.id
        )
        """
    )


def downgrade() -> None:
    # The old counters were already inconsistent and cannot be reconstructed.
    pass
