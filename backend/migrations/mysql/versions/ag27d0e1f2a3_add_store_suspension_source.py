"""track whether a store pause was requested by merchant or platform

Revision ID: ag27d0e1f2a3
Revises: af16c9d0e1f2
"""

import sqlalchemy as sa
from alembic import op

revision = "ag27d0e1f2a3"
down_revision = "af16c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stores", sa.Column("suspension_source", sa.String(32), nullable=True))
    op.execute(
        sa.text(
            "UPDATE stores SET suspension_source = 'platform' "
            "WHERE store_status = 'suspended' AND suspension_source IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("stores", "suspension_source")
