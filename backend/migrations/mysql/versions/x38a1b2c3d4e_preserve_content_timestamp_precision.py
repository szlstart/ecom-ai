"""preserve platform content timestamp precision

Revision ID: x38a1b2c3d4e
Revises: w27f0a1b2c3d
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "x38a1b2c3d4e"
down_revision = "w27f0a1b2c3d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "platform_content_versions",
        "effective_at",
        existing_type=sa.DateTime(),
        type_=mysql.DATETIME(fsp=6),
        existing_nullable=False,
    )
    op.alter_column(
        "platform_content_versions",
        "expires_at",
        existing_type=sa.DateTime(),
        type_=mysql.DATETIME(fsp=6),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "platform_content_versions",
        "expires_at",
        existing_type=mysql.DATETIME(fsp=6),
        type_=sa.DateTime(),
        existing_nullable=True,
    )
    op.alter_column(
        "platform_content_versions",
        "effective_at",
        existing_type=mysql.DATETIME(fsp=6),
        type_=sa.DateTime(),
        existing_nullable=False,
    )
