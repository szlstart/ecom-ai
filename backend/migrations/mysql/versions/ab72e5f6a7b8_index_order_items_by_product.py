"""index order items by product

Revision ID: ab72e5f6a7b8
Revises: aa61d4e5f6a7
"""

from alembic import op

revision = "ab72e5f6a7b8"
down_revision = "aa61d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_order_items_product", "order_items", ["product_id", "id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_order_items_product", table_name="order_items")
