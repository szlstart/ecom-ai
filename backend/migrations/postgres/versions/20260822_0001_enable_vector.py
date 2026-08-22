"""Enable pgvector and create AI schemas.

Revision ID: pg_20260822_0001
Revises: None
"""
from collections.abc import Sequence

from alembic import op

revision: str = "pg_20260822_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS knowledge")
    op.execute("CREATE SCHEMA IF NOT EXISTS memory")
    op.execute("CREATE SCHEMA IF NOT EXISTS agent_runtime")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS agent_runtime")
    op.execute("DROP SCHEMA IF EXISTS memory")
    op.execute("DROP SCHEMA IF EXISTS knowledge")
    # The vector extension is shared infrastructure and is intentionally retained.

