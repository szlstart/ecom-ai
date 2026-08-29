"""index order items by product

Revision ID: ab72e5f6a7b8
Revises: aa61d4e5f6a7
"""

import sqlalchemy as sa
from alembic import op

revision = "ab72e5f6a7b8"
down_revision = "aa61d4e5f6a7"
branch_labels = None
depends_on = None


def _index_exists(index_name: str) -> bool:
    connection = op.get_bind()
    return bool(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                  FROM information_schema.statistics
                 WHERE table_schema = DATABASE()
                   AND table_name = 'order_items'
                   AND index_name = :index_name
                """
            ),
            {"index_name": index_name},
        ).scalar_one()
    )


def upgrade() -> None:
    op.create_index("idx_order_items_product", "order_items", ["product_id", "id"], unique=False)
    # A downgrade creates this single-column index so MySQL can keep enforcing the
    # product foreign key while the composite index is removed. Remove that temporary
    # replacement on re-upgrade to keep the head schema identical to a fresh upgrade.
    if _index_exists("idx_order_items_product_fk"):
        op.drop_index("idx_order_items_product_fk", table_name="order_items")


def downgrade() -> None:
    # MySQL may choose the composite index as the supporting index for the product_id
    # foreign key and remove its original implicit index. Create a replacement before
    # dropping the composite index, otherwise downgrade fails with error 1553.
    if not _index_exists("idx_order_items_product_fk"):
        op.create_index(
            "idx_order_items_product_fk", "order_items", ["product_id"], unique=False
        )
    op.drop_index("idx_order_items_product", table_name="order_items")
