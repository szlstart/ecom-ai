"""create dead letter governance resource

Revision ID: u05d8e9f0a1b
Revises: t94c7d8e9f0a
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "u05d8e9f0a1b"
down_revision = "t94c7d8e9f0a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dead_letter_events",
        sa.Column("dead_letter_no", sa.String(40), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_no", sa.String(64), nullable=False),
        sa.Column(
            "active_source_key",
            sa.String(128),
            sa.Computed(
                "CASE WHEN dead_status IN ('open', 'replaying') "
                "THEN CONCAT(source_type, ':', source_no) ELSE NULL END"
            ),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("payload_redacted", sa.JSON(), nullable=True),
        sa.Column("payload_hash", sa.BINARY(32), nullable=False),
        sa.Column(
            "failure_count", mysql.INTEGER(unsigned=True), server_default="1", nullable=False
        ),
        sa.Column("first_failed_at", sa.DateTime(), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(), nullable=False),
        sa.Column("last_error_code", sa.String(64), nullable=False),
        sa.Column("last_error", sa.String(1000), nullable=False),
        sa.Column("dead_status", sa.String(16), server_default="open", nullable=False),
        sa.Column("resolved_by", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_note", sa.String(1000), nullable=True),
        sa.Column("replay_count", mysql.INTEGER(unsigned=True), server_default="0", nullable=False),
        sa.Column("last_replay_at", sa.DateTime(), nullable=True),
        sa.Column("original_trace_id", sa.String(64), nullable=True),
        sa.Column("replay_trace_id", sa.String(64), nullable=True),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(UTC_TIMESTAMP(6))"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(UTC_TIMESTAMP(6))"),
            nullable=False,
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["resolved_by"], ["users.id"], name="fk_dead_letter_events_resolved_by_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dead_letter_events"),
        sa.UniqueConstraint("dead_letter_no", name="uk_dead_letter_events_no"),
        sa.UniqueConstraint(
            "active_source_key", name="uk_dead_letter_events_active_source"
        ),
        sa.CheckConstraint(
            "dead_status IN ('open', 'replaying', 'resolved', 'ignored')",
            name="ck_dead_letter_events_status",
        ),
    )
    op.create_index(
        "idx_dead_letter_events_status",
        "dead_letter_events",
        ["dead_status", "last_failed_at", "id"],
    )
    op.create_index(
        "idx_dead_letter_events_scope",
        "dead_letter_events",
        ["scope_type", "scope_id", "dead_status", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_dead_letter_events_scope", table_name="dead_letter_events")
    op.drop_index("idx_dead_letter_events_status", table_name="dead_letter_events")
    op.drop_table("dead_letter_events")
