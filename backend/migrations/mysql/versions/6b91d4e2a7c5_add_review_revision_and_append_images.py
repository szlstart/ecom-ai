"""add review revision and append images

Revision ID: 6b91d4e2a7c5
Revises: f2c4a8d91e73
Create Date: 2026-08-23 18:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "6b91d4e2a7c5"
down_revision: str | Sequence[str] | None = "f2c4a8d91e73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_append_records",
        sa.Column("append_no", sa.String(length=40), nullable=True),
    )
    op.execute(
        "UPDATE review_append_records "
        "SET append_no = CONCAT('rpa_', LPAD(UPPER(HEX(id)), 26, '0')) "
        "WHERE append_no IS NULL"
    )
    op.alter_column(
        "review_append_records",
        "append_no",
        existing_type=sa.String(length=40),
        nullable=False,
    )
    op.create_unique_constraint(
        "uk_review_append_records_no",
        "review_append_records",
        ["append_no"],
    )

    op.create_table(
        "review_append_images",
        sa.Column("append_record_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.BINARY(length=32), nullable=False),
        sa.Column("width", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("height", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("sort_order", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("scan_status", sa.String(length=16), nullable=False),
        sa.Column("image_status", sa.String(length=16), nullable=False),
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
            ["append_record_id"],
            ["review_append_records.id"],
            name="fk_review_append_images_append_record_id_review_append_records",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_append_images"),
        sa.UniqueConstraint(
            "append_record_id",
            "sort_order",
            name="uk_review_append_images_order",
        ),
        sa.UniqueConstraint("object_key", name="uk_review_append_images_object_key"),
    )
    op.create_index(
        "idx_review_append_images_append",
        "review_append_images",
        ["append_record_id", "sort_order", "id"],
    )

    op.create_table(
        "review_revision_records",
        sa.Column("revision_no", sa.String(length=40), nullable=False),
        sa.Column("review_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("actor_user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("before_snapshot", sa.JSON(), nullable=False),
        sa.Column("after_snapshot", sa.JSON(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["review_id"], ["reviews.id"], name="fk_review_revision_records_review_id_reviews"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_review_revision_records_actor_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_revision_records"),
        sa.UniqueConstraint("revision_no", name="uk_review_revision_records_no"),
    )
    op.create_index(
        "idx_review_revision_records_review",
        "review_revision_records",
        ["review_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("review_revision_records")
    op.drop_table("review_append_images")
    op.drop_constraint(
        "uk_review_append_records_no",
        "review_append_records",
        type_="unique",
    )
    op.drop_column("review_append_records", "append_no")
