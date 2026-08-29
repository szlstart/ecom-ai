"""add public review read model

Revision ID: a4d8e2f19b7c
Revises: 9c8f7e6d5a4b
Create Date: 2026-08-23 09:25:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "a4d8e2f19b7c"
down_revision: str | Sequence[str] | None = "9c8f7e6d5a4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reviews",
        sa.Column("review_no", sa.String(length=40), nullable=False),
        # Phase four creates orders/order_items and adds the two deferred FKs.
        sa.Column("order_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("order_item_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("store_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("product_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("sku_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("rating", mysql.TINYINT(unsigned=True), nullable=False),
        sa.Column("content", sa.String(length=500), nullable=True),
        sa.Column("is_anonymous", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("moderation_status", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("hidden_at", sa.DateTime(), nullable=True),
        sa.Column(
            "helpful_count", mysql.INTEGER(unsigned=True), server_default="0", nullable=False
        ),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_reviews_user_id_users"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_reviews_store_id_stores"),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_reviews_product_id_products"
        ),
        sa.ForeignKeyConstraint(
            ["sku_id"], ["product_skus.id"], name="fk_reviews_sku_id_product_skus"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reviews"),
        sa.UniqueConstraint("review_no", name="uk_reviews_no"),
        sa.UniqueConstraint("order_item_id", name="uk_reviews_order_item"),
    )
    op.create_index(
        "idx_reviews_product_published",
        "reviews",
        ["product_id", "review_status", "published_at", "id"],
    )
    op.create_index("idx_reviews_user_time", "reviews", ["user_id", "created_at", "id"])

    op.create_table(
        "review_images",
        sa.Column("review_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.BINARY(length=32), nullable=False),
        sa.Column("width", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("height", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("sort_order", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("scan_status", sa.String(length=16), nullable=False),
        sa.Column("image_status", sa.String(length=16), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["review_id"], ["reviews.id"], name="fk_review_images_review_id_reviews"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_images"),
        sa.UniqueConstraint("review_id", "sort_order", name="uk_review_images_order"),
        sa.UniqueConstraint("object_key", name="uk_review_images_object_key"),
    )
    op.create_index("idx_review_images_review", "review_images", ["review_id", "sort_order", "id"])

    op.create_table(
        "review_replies",
        sa.Column("review_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("store_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("replier_user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("content", sa.String(length=2000), nullable=False),
        sa.Column("reply_status", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("hidden_at", sa.DateTime(), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["review_id"], ["reviews.id"], name="fk_review_replies_review_id_reviews"
        ),
        sa.ForeignKeyConstraint(
            ["store_id"], ["stores.id"], name="fk_review_replies_store_id_stores"
        ),
        sa.ForeignKeyConstraint(
            ["replier_user_id"],
            ["users.id"],
            name="fk_review_replies_replier_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_replies"),
        sa.UniqueConstraint("review_id", name="uk_review_replies_review"),
    )

    op.create_table(
        "review_append_records",
        sa.Column("review_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("content", sa.String(length=500), nullable=False),
        sa.Column("append_status", sa.String(length=16), nullable=False),
        sa.Column("moderation_status", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["review_id"], ["reviews.id"], name="fk_review_append_records_review_id_reviews"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_review_append_records_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_append_records"),
        sa.UniqueConstraint("review_id", name="uk_review_append_review"),
    )


def downgrade() -> None:
    op.drop_table("review_append_records")
    op.drop_table("review_replies")
    op.drop_table("review_images")
    op.drop_table("reviews")
