"""Add refund claim ownership, appeal events and immutable review fields.

Revision ID: i83d4e6f7a8b
Revises: h72c9d3e5f6a
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "i83d4e6f7a8b"
down_revision = "h72c9d3e5f6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refund_applications", sa.Column("decided_by", mysql.BIGINT(unsigned=True)))
    op.add_column("refund_applications", sa.Column("claimed_by", mysql.BIGINT(unsigned=True)))
    op.add_column("refund_applications", sa.Column("claimed_at", sa.DateTime()))
    op.create_foreign_key(
        "fk_refund_applications_decided_by", "refund_applications", "users", ["decided_by"], ["id"]
    )
    op.create_foreign_key(
        "fk_refund_applications_claimed_by", "refund_applications", "users", ["claimed_by"], ["id"]
    )
    op.add_column(
        "refund_appeals", sa.Column("store_id", mysql.BIGINT(unsigned=True), nullable=True)
    )
    op.add_column("refund_appeals", sa.Column("claimed_by", mysql.BIGINT(unsigned=True)))
    op.add_column("refund_appeals", sa.Column("claimed_at", sa.DateTime()))
    op.add_column("refund_appeals", sa.Column("reviewed_by", mysql.BIGINT(unsigned=True)))
    op.add_column(
        "refund_appeals",
        sa.Column("reason_code", sa.String(64), nullable=False, server_default="USER_APPEAL"),
    )
    op.add_column("refund_appeals", sa.Column("resolution_code", sa.String(64)))
    op.add_column("refund_appeals", sa.Column("resolution_detail", sa.String(2000)))
    op.execute(
        "UPDATE refund_appeals ra JOIN refund_applications r ON r.id=ra.refund_id "
        "SET ra.store_id=r.store_id"
    )
    op.alter_column(
        "refund_appeals", "store_id", existing_type=mysql.BIGINT(unsigned=True), nullable=False
    )
    op.create_foreign_key(
        "fk_refund_appeals_store", "refund_appeals", "stores", ["store_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_refund_appeals_claimed_by", "refund_appeals", "users", ["claimed_by"], ["id"]
    )
    op.create_foreign_key(
        "fk_refund_appeals_reviewed_by", "refund_appeals", "users", ["reviewed_by"], ["id"]
    )
    op.create_table(
        "refund_appeal_events",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("event_no", sa.String(40), nullable=False),
        sa.Column("appeal_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(24)),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", mysql.BIGINT(unsigned=True)),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("remark", sa.String(1000)),
        sa.Column("appeal_version", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("trace_id", sa.String(64)),
        sa.ForeignKeyConstraint(["appeal_id"], ["refund_appeals.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.UniqueConstraint("event_no", name="uk_refund_appeal_events_no"),
    )
    op.create_index(
        "idx_refund_appeal_events_appeal_time",
        "refund_appeal_events",
        ["appeal_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("refund_appeal_events")
    for name in (
        "fk_refund_appeals_reviewed_by",
        "fk_refund_appeals_claimed_by",
        "fk_refund_appeals_store",
    ):
        op.drop_constraint(name, "refund_appeals", type_="foreignkey")
    for column in (
        "resolution_detail",
        "resolution_code",
        "reason_code",
        "reviewed_by",
        "claimed_at",
        "claimed_by",
        "store_id",
    ):
        op.drop_column("refund_appeals", column)
    for name in ("fk_refund_applications_claimed_by", "fk_refund_applications_decided_by"):
        op.drop_constraint(name, "refund_applications", type_="foreignkey")
    for column in ("claimed_at", "claimed_by", "decided_by"):
        op.drop_column("refund_applications", column)
