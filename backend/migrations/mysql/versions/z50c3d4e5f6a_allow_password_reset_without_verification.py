"""allow password reset after exact recovery email match

Revision ID: z50c3d4e5f6a
Revises: y49b2c3d4e5f
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "z50c3d4e5f6a"
down_revision = "y49b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "password_reset_records",
        "verification_id",
        existing_type=mysql.BIGINT(unsigned=True),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM password_reset_records WHERE verification_id IS NULL"))
    op.alter_column(
        "password_reset_records",
        "verification_id",
        existing_type=mysql.BIGINT(unsigned=True),
        nullable=False,
    )
