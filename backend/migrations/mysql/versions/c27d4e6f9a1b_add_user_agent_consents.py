"""add user agent consents

Revision ID: c27d4e6f9a1b
Revises: b16c3d5e8f0a
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "c27d4e6f9a1b"
down_revision: str | Sequence[str] | None = "b16c3d5e8f0a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_agent_consents",
        sa.Column("consent_no", sa.String(40), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("consent_type", sa.String(32), nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_no", sa.String(64), nullable=True),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("consent_status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_user_agent_consents"),
        sa.UniqueConstraint("consent_no", name="uk_user_agent_consents_no"),
    )
    op.create_index(
        "idx_user_agent_consents_user",
        "user_agent_consents",
        ["user_id", "consent_status", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("user_agent_consents")
