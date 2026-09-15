"""preserve product content files and repair collected derivatives

Revision ID: an94e7f8a9b0
Revises: am83d6e7f8a9
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "an94e7f8a9b0"
down_revision: str | Sequence[str] | None = "am83d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FILE_ID = re.compile(r"file_[0-9A-HJKMNP-TV-Z]{26}")


def _file_ids(safe_blocks: object, safe_html: object) -> set[str]:
    values: set[str] = set()
    if safe_blocks is not None:
        if isinstance(safe_blocks, str):
            try:
                safe_blocks = json.loads(safe_blocks)
            except json.JSONDecodeError:
                safe_blocks = None
        if safe_blocks is not None:
            values.update(_FILE_ID.findall(json.dumps(safe_blocks, ensure_ascii=True)))
    if safe_html:
        values.update(_FILE_ID.findall(str(safe_html)))
    return values


def upgrade() -> None:
    op.create_table(
        "product_content_version_files",
        sa.Column("content_version_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("file_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["content_version_id"],
            ["product_content_versions.id"],
            name="fk_pcv_files_version",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["file_objects.id"],
            name="fk_pcv_files_file",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_product_content_version_files"),
        sa.UniqueConstraint(
            "content_version_id", "file_id", name="uk_product_content_version_files_version_file"
        ),
    )
    op.create_index(
        "idx_product_content_version_files_file",
        "product_content_version_files",
        ["file_id", "content_version_id"],
        unique=False,
    )

    connection = op.get_bind()
    versions = connection.execute(
        sa.text("SELECT id, safe_blocks, safe_html FROM product_content_versions")
    ).mappings()
    for version in versions:
        for file_no in _file_ids(version["safe_blocks"], version["safe_html"]):
            connection.execute(
                sa.text(
                    """INSERT IGNORE INTO product_content_version_files
                       (content_version_id, file_id, version)
                       SELECT :content_version_id, id, 0
                       FROM file_objects WHERE file_no=:file_no"""
                ),
                {"content_version_id": version["id"], "file_no": file_no},
            )

    connection.execute(
        sa.text(
            """UPDATE file_objects AS file
               JOIN (
                   SELECT file_id, COUNT(*) AS total
                   FROM product_content_version_files
                   GROUP BY file_id
               ) AS refs ON refs.file_id=file.id
               SET file.reference_count=file.reference_count + refs.total,
                   file.version=file.version + 1"""
        )
    )

    # Older releases omitted these references, so the collector may already
    # have removed a derivative. Its retained private source can recreate the
    # same variant while preserving the public file number stored in content.
    connection.execute(
        sa.text(
            """UPDATE file_objects AS source
               JOIN file_objects AS derived ON derived.parent_file_id=source.id
               JOIN product_content_version_files AS refs ON refs.file_id=derived.id
               SET source.file_status='scanning',
                   source.scan_status='pending',
                   source.version=source.version + 1
               WHERE derived.file_status <> 'active'
                  OR derived.scan_status <> 'safe'
                  OR derived.visibility <> 'public_derivative'"""
        )
    )


def downgrade() -> None:
    op.drop_table("product_content_version_files")
