"""widen file visibility for explicit public derivative state

Revision ID: 9c8f7e6d5a4b
Revises: 146fb74fcd9e
Create Date: 2026-08-23 05:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c8f7e6d5a4b"
down_revision: str | Sequence[str] | None = "146fb74fcd9e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "file_objects",
        "visibility",
        existing_type=sa.String(length=16),
        type_=sa.String(length=24),
        existing_nullable=False,
    )
    op.execute(
        "UPDATE file_objects SET visibility = 'public_derivative' "
        "WHERE visibility = 'public'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE file_objects SET visibility = 'public' "
        "WHERE visibility = 'public_derivative'"
    )
    op.alter_column(
        "file_objects",
        "visibility",
        existing_type=sa.String(length=24),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
