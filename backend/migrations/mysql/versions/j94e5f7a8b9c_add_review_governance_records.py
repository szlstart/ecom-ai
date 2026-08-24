"""add immutable review governance records

Revision ID: j94e5f7a8b9c
Revises: i83d4e6f7a8b
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "j94e5f7a8b9c"
down_revision: str | Sequence[str] | None = "i83d4e6f7a8b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_governance_records",
        sa.Column("governance_no", sa.String(40), nullable=False),
        sa.Column("review_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("from_status", sa.String(16), nullable=False),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("rule_code", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("actor_user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("review_version", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.UniqueConstraint("governance_no", name="uk_review_governance_records_no"),
    )
    op.create_index(
        "idx_review_governance_records_review",
        "review_governance_records",
        ["review_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("review_governance_records")
