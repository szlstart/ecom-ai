"""rename trade order count as immutable creation snapshot

Revision ID: ak61b4c5d6e7
Revises: aj50a3b4c5d6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ak61b4c5d6e7"
down_revision: str | Sequence[str] | None = "aj50a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "trade_orders",
        "order_count",
        existing_type=sa.SmallInteger(),
        existing_nullable=False,
        new_column_name="original_order_count",
    )


def downgrade() -> None:
    op.alter_column(
        "trade_orders",
        "original_order_count",
        existing_type=sa.SmallInteger(),
        existing_nullable=False,
        new_column_name="order_count",
    )
