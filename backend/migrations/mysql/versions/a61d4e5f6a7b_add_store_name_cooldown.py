"""add store name change cooldown timestamp

Revision ID: a61d4e5f6a7b
Revises: z50c3d4e5f6a
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "a61d4e5f6a7b"
down_revision = "z50c3d4e5f6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column("store_name_changed_at", mysql.DATETIME(fsp=6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stores", "store_name_changed_at")
