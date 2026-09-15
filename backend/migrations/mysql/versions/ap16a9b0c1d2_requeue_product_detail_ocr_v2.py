"""requeue product detail OCR after confidence filtering upgrade

Revision ID: ap16a9b0c1d2
Revises: ao05f8a9b0c1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ap16a9b0c1d2"
down_revision: str | Sequence[str] | None = "ao05f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """UPDATE file_objects AS source
               JOIN file_objects AS derived ON derived.parent_file_id=source.id
               JOIN product_content_version_files AS refs ON refs.file_id=derived.id
               SET source.ocr_status='pending', source.ocr_text=NULL,
                   source.ocr_engine=NULL, source.ocr_language=NULL,
                   source.ocr_processed_at=NULL, source.ocr_error_code=NULL,
                   source.version=source.version + 1
               WHERE source.parent_file_id IS NULL
                 AND source.file_status='active' AND source.scan_status='safe'"""
        )
    )
    connection.execute(
        sa.text(
            """UPDATE file_objects AS derived
               JOIN product_content_version_files AS refs ON refs.file_id=derived.id
               SET derived.ocr_status='pending', derived.ocr_text=NULL,
                   derived.ocr_engine=NULL, derived.ocr_language=NULL,
                   derived.ocr_processed_at=NULL, derived.ocr_error_code=NULL,
                   derived.version=derived.version + 1
               WHERE derived.file_status='active' AND derived.scan_status='safe'"""
        )
    )


def downgrade() -> None:
    # OCR text is derived data. A downgrade keeps the latest valid results.
    pass
