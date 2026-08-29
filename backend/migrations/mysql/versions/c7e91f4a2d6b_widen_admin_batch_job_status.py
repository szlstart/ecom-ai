"""widen admin batch job status

Revision ID: c7e91f4a2d6b
Revises: a4d8e2f19b7c
Create Date: 2026-08-23 11:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e91f4a2d6b"
down_revision: str | Sequence[str] | None = "a4d8e2f19b7c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "admin_batch_jobs",
        "job_status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "admin_batch_jobs",
        "job_status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
