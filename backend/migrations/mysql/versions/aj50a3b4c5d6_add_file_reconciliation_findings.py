"""add durable file reconciliation findings

Revision ID: aj50a3b4c5d6
Revises: ai49f2a3b4c5
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "aj50a3b4c5d6"
down_revision = "ai49f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "file_reconciliation_findings",
        sa.Column("finding_no", sa.String(length=40), nullable=False),
        sa.Column("finding_key", mysql.BINARY(length=32), nullable=False),
        sa.Column("finding_type", sa.String(length=40), nullable=False),
        sa.Column("bucket", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("file_no", sa.String(length=40), nullable=True),
        sa.Column("expected_reference_count", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("actual_reference_count", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("finding_status", sa.String(length=24), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("quarantine_until", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_code", sa.String(length=64), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_file_reconciliation_findings"),
        sa.UniqueConstraint("finding_no", name="uk_file_reconciliation_findings_no"),
        sa.UniqueConstraint("finding_key", name="uk_file_reconciliation_findings_key"),
    )
    op.create_index(
        "idx_file_reconciliation_findings_status",
        "file_reconciliation_findings",
        ["finding_status", "last_seen_at", "id"],
        unique=False,
    )
    op.create_index(
        "idx_file_reconciliation_findings_storage",
        "file_reconciliation_findings",
        ["bucket", "finding_type", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_file_reconciliation_findings_storage",
        table_name="file_reconciliation_findings",
    )
    op.drop_index(
        "idx_file_reconciliation_findings_status",
        table_name="file_reconciliation_findings",
    )
    op.drop_table("file_reconciliation_findings")
