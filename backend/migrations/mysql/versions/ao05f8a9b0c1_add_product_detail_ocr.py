"""add product detail image OCR and enqueue legacy images

Revision ID: ao05f8a9b0c1
Revises: an94e7f8a9b0
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "ao05f8a9b0c1"
down_revision: str | Sequence[str] | None = "an94e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "file_objects",
        sa.Column(
            "ocr_status",
            sa.String(length=20),
            server_default="not_requested",
            nullable=False,
        ),
    )
    op.add_column("file_objects", sa.Column("ocr_text", mysql.MEDIUMTEXT(), nullable=True))
    op.add_column("file_objects", sa.Column("ocr_engine", sa.String(length=64), nullable=True))
    op.add_column("file_objects", sa.Column("ocr_language", sa.String(length=32), nullable=True))
    op.add_column("file_objects", sa.Column("ocr_processed_at", sa.DateTime(), nullable=True))
    op.add_column("file_objects", sa.Column("ocr_error_code", sa.String(length=64), nullable=True))
    op.create_index(
        "idx_file_objects_ocr",
        "file_objects",
        ["ocr_status", "purpose", "id"],
        unique=False,
    )

    connection = op.get_bind()
    # Product detail references store a public derivative. OCR runs once on its
    # immutable private source and then copies the result to every derivative.
    connection.execute(
        sa.text(
            """UPDATE file_objects AS source
               JOIN file_objects AS derived ON derived.parent_file_id=source.id
               JOIN product_content_version_files AS refs ON refs.file_id=derived.id
               SET source.ocr_status='pending',
                   source.ocr_text=NULL,
                   source.ocr_engine=NULL,
                   source.ocr_language=NULL,
                   source.ocr_processed_at=NULL,
                   source.ocr_error_code=NULL,
                   source.version=source.version + 1
               WHERE source.parent_file_id IS NULL
                 AND source.file_status='active'
                 AND source.scan_status='safe'"""
        )
    )
    connection.execute(
        sa.text(
            """UPDATE file_objects AS derived
               JOIN product_content_version_files AS refs ON refs.file_id=derived.id
               SET derived.ocr_status='pending',
                   derived.ocr_text=NULL,
                   derived.ocr_engine=NULL,
                   derived.ocr_language=NULL,
                   derived.ocr_processed_at=NULL,
                   derived.ocr_error_code=NULL,
                   derived.version=derived.version + 1
               WHERE derived.file_status='active'
                 AND derived.scan_status='safe'"""
        )
    )


def downgrade() -> None:
    op.drop_index("idx_file_objects_ocr", table_name="file_objects")
    op.drop_column("file_objects", "ocr_error_code")
    op.drop_column("file_objects", "ocr_processed_at")
    op.drop_column("file_objects", "ocr_language")
    op.drop_column("file_objects", "ocr_engine")
    op.drop_column("file_objects", "ocr_text")
    op.drop_column("file_objects", "ocr_status")
