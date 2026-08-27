"""add product logical deletion

Revision ID: aa61d4e5f6a7
Revises: a61d4e5f6a7b
"""

import sqlalchemy as sa
from alembic import op

revision = "aa61d4e5f6a7"
down_revision = "a61d4e5f6a7b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.create_index(
        "idx_products_store_deleted_status",
        "products",
        ["store_id", "deleted_at", "product_status", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_products_store_deleted_status", table_name="products")
    op.drop_column("products", "deleted_at")
