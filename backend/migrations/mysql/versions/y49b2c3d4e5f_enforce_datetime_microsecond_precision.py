"""enforce datetime microsecond precision

Revision ID: y49b2c3d4e5f
Revises: x38a1b2c3d4e
"""

from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "y49b2c3d4e5f"
down_revision = "x38a1b2c3d4e"
branch_labels = None
depends_on = None


def _datetime_columns(precision: int) -> Iterable[tuple[str, str, str, str | None]]:
    bind = op.get_bind()
    return bind.execute(
        sa.text(
            """
            SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND DATA_TYPE = 'datetime'
              AND DATETIME_PRECISION = :precision
            ORDER BY TABLE_NAME, ORDINAL_POSITION
            """
        ),
        {"precision": precision},
    ).tuples()


def _change_precision(
    source: int, target: int, *, exclusions: set[tuple[str, str]] | None = None
) -> None:
    source_type = mysql.DATETIME(fsp=source)
    target_type = mysql.DATETIME(fsp=target)
    for table_name, column_name, nullable, default in list(_datetime_columns(source)):
        if exclusions and (table_name, column_name) in exclusions:
            continue
        existing_default = sa.text(f"({default})") if default is not None else None
        op.alter_column(
            table_name,
            column_name,
            existing_type=source_type,
            type_=target_type,
            existing_nullable=nullable == "YES",
            existing_server_default=existing_default,
        )


def upgrade() -> None:
    _change_precision(0, 6)


def downgrade() -> None:
    _change_precision(
        6,
        0,
        exclusions={
            ("platform_content_versions", "effective_at"),
            ("platform_content_versions", "expires_at"),
        },
    )
