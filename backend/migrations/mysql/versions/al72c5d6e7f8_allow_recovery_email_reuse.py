"""allow the same recovery email to be used by multiple accounts

Revision ID: al72c5d6e7f8
Revises: ak61b4c5d6e7
"""

from collections.abc import Sequence

from alembic import op

revision: str = "al72c5d6e7f8"
down_revision: str | Sequence[str] | None = "ak61b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uk_credentials_active_identifier",
        "user_credentials",
        type_="unique",
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "uk_credentials_active_identifier",
        "user_credentials",
        ["credential_type", "active_identifier_hash"],
    )
