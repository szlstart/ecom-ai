"""bind every storefront product image to a concrete SKU

Revision ID: af16c9d0e1f2
Revises: ae05b8c9d0e1
"""

from alembic import op
from sqlalchemy.dialects import mysql

revision = "af16c9d0e1f2"
down_revision = "ae05b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Older releases stored one product-level main image. Move that image to
    # the default active SKU so existing storefronts keep their cover image.
    op.execute(
        """UPDATE product_images image
        JOIN products product ON product.id=image.product_id
        SET image.sku_id=product.default_sku_id, image.image_type='spec'
        WHERE image.sku_id IS NULL AND product.default_sku_id IS NOT NULL
        """
    )
    # A product without any SKU cannot display or sell an image. Such orphaned
    # legacy rows are removed; their file objects remain governed by file GC.
    op.execute("DELETE FROM product_images WHERE sku_id IS NULL")
    op.execute("UPDATE product_images SET image_type='spec' WHERE image_type<>'spec'")
    op.alter_column(
        "product_images",
        "sku_id",
        existing_type=mysql.BIGINT(unsigned=True),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "product_images",
        "sku_id",
        existing_type=mysql.BIGINT(unsigned=True),
        nullable=True,
    )
    # The previous model required one SPU main image. Recreate it from the
    # default SKU's first image where possible while retaining SKU galleries.
    op.execute(
        """UPDATE product_images image
        JOIN (
            SELECT product_id, MIN(id) AS selected_id
            FROM product_images GROUP BY product_id
        ) selected ON selected.selected_id=image.id
        SET image.sku_id=NULL, image.image_type='main'
        """
    )
