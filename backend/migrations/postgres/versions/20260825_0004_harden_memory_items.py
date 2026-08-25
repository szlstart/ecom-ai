"""Harden user memory lifecycle and encrypted content.

Revision ID: pg_20260825_0004
Revises: pg_20260824_0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "pg_20260825_0004"
down_revision: str | None = "pg_20260824_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("ALTER TABLE memory.items RENAME COLUMN agent_scope TO namespace")
    op.execute("ALTER TABLE memory.items ALTER COLUMN safe_text DROP NOT NULL")
    op.execute(
        """
        ALTER TABLE memory.items
          ADD COLUMN memory_key VARCHAR(128),
          ADD COLUMN content_ciphertext BYTEA,
          ADD COLUMN content_hash BYTEA,
          ADD COLUMN dedupe_fingerprint BYTEA,
          ADD COLUMN key_version SMALLINT NOT NULL DEFAULT 1,
          ADD COLUMN source_type VARCHAR(32),
          ADD COLUMN source_ref VARCHAR(128),
          ADD COLUMN source_conversation_no VARCHAR(64),
          ADD COLUMN source_message_no VARCHAR(64),
          ADD COLUMN consent_policy_version VARCHAR(32),
          ADD COLUMN validation_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN salience NUMERIC(4,3) NOT NULL DEFAULT 0.500,
          ADD COLUMN data_classification VARCHAR(4) NOT NULL DEFAULT 'L2',
          ADD COLUMN memory_risk_level VARCHAR(16) NOT NULL DEFAULT 'low',
          ADD COLUMN valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
          ADD COLUMN valid_until TIMESTAMPTZ,
          ADD COLUMN supersedes_memory_id BIGINT REFERENCES memory.items(id),
          ADD COLUMN last_accessed_at TIMESTAMPTZ,
          ADD COLUMN access_count BIGINT NOT NULL DEFAULT 0,
          ADD COLUMN version BIGINT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """UPDATE memory.items SET
        memory_key = 'legacy.' || memory_no,
        content_hash = digest(coalesce(safe_text, ''), 'sha256'),
        dedupe_fingerprint = digest(memory_no, 'sha256'),
        source_type = 'legacy_import', source_ref = memory_no,
        consent_policy_version = 'legacy'
        WHERE memory_key IS NULL"""
    )
    op.execute(
        """ALTER TABLE memory.items
        ALTER COLUMN memory_key SET NOT NULL,
        ALTER COLUMN content_hash SET NOT NULL,
        ALTER COLUMN dedupe_fingerprint SET NOT NULL,
        ALTER COLUMN source_type SET NOT NULL,
        ALTER COLUMN source_ref SET NOT NULL,
        ALTER COLUMN consent_policy_version SET NOT NULL"""
    )
    op.execute("ALTER TABLE memory.items DROP CONSTRAINT IF EXISTS items_memory_status_check")
    op.execute(
        """ALTER TABLE memory.items ADD CONSTRAINT items_memory_status_check
        CHECK (memory_status IN ('candidate','active','superseded','revoked','expired','deleted'))"""
    )
    op.execute(
        """CREATE UNIQUE INDEX uk_memory_items_active_semantic_key
        ON memory.items(user_no, namespace, coalesce(store_no, ''), memory_type, memory_key)
        WHERE memory_status = 'active'"""
    )
    op.execute("ALTER TABLE memory.events ADD COLUMN user_no VARCHAR(64)")
    op.execute("ALTER TABLE memory.events ADD COLUMN from_status VARCHAR(16)")
    op.execute("ALTER TABLE memory.events ADD COLUMN to_status VARCHAR(16)")
    op.execute("ALTER TABLE memory.events ADD COLUMN actor_no VARCHAR(64)")
    op.execute("ALTER TABLE memory.events ADD COLUMN content_hash_before BYTEA")
    op.execute("ALTER TABLE memory.events ADD COLUMN content_hash_after BYTEA")
    op.execute("ALTER TABLE memory.events ADD COLUMN trace_id VARCHAR(64)")
    op.execute("ALTER TABLE memory.events ADD COLUMN metadata_redacted JSONB NOT NULL DEFAULT '{}'::jsonb")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS memory.uk_memory_items_active_semantic_key")
    for column in (
        "metadata_redacted", "trace_id", "content_hash_after", "content_hash_before",
        "actor_no", "to_status", "from_status", "user_no",
    ):
        op.execute(f"ALTER TABLE memory.events DROP COLUMN {column}")
    for column in (
        "version", "access_count", "last_accessed_at", "supersedes_memory_id", "valid_until",
        "valid_from", "memory_risk_level", "data_classification", "salience",
        "validation_snapshot", "consent_policy_version", "source_message_no",
        "source_conversation_no", "source_ref", "source_type", "key_version",
        "dedupe_fingerprint", "content_hash", "content_ciphertext", "memory_key",
    ):
        op.execute(f"ALTER TABLE memory.items DROP COLUMN {column}")
    op.execute("ALTER TABLE memory.items ALTER COLUMN safe_text SET NOT NULL")
    op.execute("ALTER TABLE memory.items RENAME COLUMN namespace TO agent_scope")
