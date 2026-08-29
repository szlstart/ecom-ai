"""Align memory storage with the encrypted, versioned design contract.

Revision ID: pg_20260825_0005
Revises: pg_20260825_0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "pg_20260825_0005"
down_revision: str | None = "pg_20260825_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM memory.items WHERE safe_text IS NOT NULL) THEN
          RAISE EXCEPTION 'memory plaintext backfill must finish before pg_20260825_0005';
        END IF;
        END $$"""
    )
    op.execute("DROP INDEX IF EXISTS memory.idx_memory_items_embedding_hnsw")
    op.execute(
        """ALTER TABLE memory.items
        ADD COLUMN id_uuid UUID NOT NULL DEFAULT gen_random_uuid(),
        ADD COLUMN supersedes_memory_uuid UUID"""
    )
    op.execute(
        """UPDATE memory.items child SET supersedes_memory_uuid=parent.id_uuid
        FROM memory.items parent WHERE child.supersedes_memory_id=parent.id"""
    )
    op.execute(
        "ALTER TABLE memory.items DROP CONSTRAINT IF EXISTS items_supersedes_memory_id_fkey"
    )
    op.execute("ALTER TABLE memory.items DROP CONSTRAINT items_pkey")
    op.execute("ALTER TABLE memory.items DROP COLUMN supersedes_memory_id")
    op.execute("ALTER TABLE memory.items DROP COLUMN id")
    op.execute("ALTER TABLE memory.items RENAME COLUMN id_uuid TO id")
    op.execute(
        "ALTER TABLE memory.items RENAME COLUMN supersedes_memory_uuid TO supersedes_memory_id"
    )
    op.execute("ALTER TABLE memory.items ADD PRIMARY KEY (id)")
    op.execute(
        """ALTER TABLE memory.items ADD CONSTRAINT fk_memory_items_supersedes
        FOREIGN KEY (supersedes_memory_id) REFERENCES memory.items(id)"""
    )
    op.execute("ALTER TABLE memory.items ALTER COLUMN user_no TYPE VARCHAR(64)")
    op.execute("ALTER TABLE memory.items ALTER COLUMN namespace TYPE VARCHAR(32)")
    op.execute("ALTER TABLE memory.items ALTER COLUMN store_no TYPE VARCHAR(64)")
    op.execute("ALTER TABLE memory.items ALTER COLUMN memory_type TYPE VARCHAR(32)")
    op.execute(
        """ALTER TABLE memory.items ALTER COLUMN confidence TYPE NUMERIC(4,3)
        USING confidence::NUMERIC(4,3)"""
    )
    op.execute("ALTER TABLE memory.items ALTER COLUMN consent_no TYPE VARCHAR(64)")
    op.execute("ALTER TABLE memory.items ALTER COLUMN consent_no SET NOT NULL")
    op.execute("ALTER TABLE memory.items ALTER COLUMN expires_at SET NOT NULL")
    op.execute("ALTER TABLE memory.items DROP COLUMN safe_text")
    op.execute("ALTER TABLE memory.items DROP COLUMN embedding")
    op.execute(
        """ALTER TABLE memory.items ADD CONSTRAINT ck_memory_items_namespace_store
        CHECK ((namespace='exclusive' AND store_no IS NULL) OR
               (namespace='store' AND store_no IS NOT NULL))"""
    )
    op.execute(
        """ALTER TABLE memory.items ADD CONSTRAINT ck_memory_items_type
        CHECK (memory_type IN ('preference','stable_fact','constraint','service_history'))"""
    )
    op.execute(
        """ALTER TABLE memory.items ADD CONSTRAINT ck_memory_items_content_lifecycle
        CHECK ((memory_status IN ('candidate','active','superseded')
                AND content_ciphertext IS NOT NULL)
            OR (memory_status IN ('revoked','expired','deleted')))"""
    )
    op.execute(
        """ALTER TABLE memory.items ADD CONSTRAINT ck_memory_items_classification
        CHECK (data_classification='L2')"""
    )
    op.execute(
        """ALTER TABLE memory.items ADD CONSTRAINT ck_memory_items_risk
        CHECK (memory_risk_level IN ('low','medium','high','prohibited'))"""
    )
    op.execute(
        """ALTER TABLE memory.items ADD CONSTRAINT ck_memory_items_expiry
        CHECK (expires_at > created_at AND (valid_until IS NULL OR valid_until >= valid_from))"""
    )
    op.execute(
        """CREATE TABLE memory.item_embeddings (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        memory_id UUID NOT NULL REFERENCES memory.items(id) ON DELETE CASCADE,
        embedding_model_id VARCHAR(64) NOT NULL
          REFERENCES knowledge.embedding_models(model_code),
        embedding VECTOR(1536) NOT NULL,
        dimension INTEGER NOT NULL CHECK (dimension=1536),
        input_hash BYTEA NOT NULL,
        embedding_status VARCHAR(16) NOT NULL
          CHECK (embedding_status IN ('building','active','retired','failed')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (memory_id, embedding_model_id)
        )"""
    )
    op.execute(
        """CREATE INDEX idx_memory_item_embeddings_hnsw
        ON memory.item_embeddings USING hnsw (embedding vector_cosine_ops)
        WITH (m=16, ef_construction=128) WHERE embedding_status='active'"""
    )

    op.execute(
        """ALTER TABLE memory.events
        ADD COLUMN id_uuid UUID NOT NULL DEFAULT gen_random_uuid(),
        ADD COLUMN memory_id UUID,
        ADD COLUMN consent_no VARCHAR(64),
        ADD COLUMN source_message_no VARCHAR(64),
        ADD COLUMN ai_run_no VARCHAR(64)"""
    )
    op.execute(
        """UPDATE memory.events event SET memory_id=item.id
        FROM memory.items item WHERE event.memory_no=item.memory_no"""
    )
    op.execute("ALTER TABLE memory.events ALTER COLUMN memory_id SET NOT NULL")
    op.execute("ALTER TABLE memory.events ALTER COLUMN user_no SET NOT NULL")
    op.execute("ALTER TABLE memory.events ALTER COLUMN to_status SET NOT NULL")
    op.execute("ALTER TABLE memory.events ALTER COLUMN actor_no SET NOT NULL")
    op.execute("ALTER TABLE memory.events ALTER COLUMN reason_code SET NOT NULL")
    op.execute("DROP INDEX IF EXISTS memory.idx_memory_events_memory")
    op.execute("ALTER TABLE memory.events DROP CONSTRAINT events_pkey")
    op.execute("ALTER TABLE memory.events DROP COLUMN id")
    op.execute("ALTER TABLE memory.events DROP COLUMN memory_no")
    op.execute("ALTER TABLE memory.events RENAME COLUMN id_uuid TO id")
    op.execute("ALTER TABLE memory.events ADD PRIMARY KEY (id)")
    op.execute(
        """ALTER TABLE memory.events ADD CONSTRAINT fk_memory_events_item
        FOREIGN KEY (memory_id) REFERENCES memory.items(id)"""
    )
    op.execute(
        "CREATE INDEX idx_memory_events_memory ON memory.events(memory_id, created_at, id)"
    )
    op.execute("CREATE INDEX idx_memory_events_user ON memory.events(user_no, created_at)")

    op.execute(
        """DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM memory.summaries) THEN
          RAISE EXCEPTION 'legacy plaintext summaries must be migrated before pg_20260825_0005';
        END IF;
        END $$"""
    )
    op.execute("DROP TABLE memory.summaries")
    op.execute(
        """CREATE TABLE memory.summaries (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        summary_no VARCHAR(40) NOT NULL UNIQUE,
        conversation_no VARCHAR(64) NOT NULL,
        user_no VARCHAR(64) NOT NULL,
        store_no VARCHAR(64),
        start_message_no VARCHAR(64) NOT NULL,
        end_message_no VARCHAR(64) NOT NULL,
        message_count INTEGER NOT NULL CHECK (message_count > 0),
        source_token_count INTEGER NOT NULL CHECK (source_token_count >= 0),
        summary_token_count INTEGER NOT NULL CHECK (summary_token_count >= 0),
        summary_ciphertext BYTEA NOT NULL,
        summary_hash BYTEA NOT NULL,
        source_hash BYTEA NOT NULL,
        key_version SMALLINT NOT NULL,
        model_name VARCHAR(128) NOT NULL,
        prompt_version VARCHAR(32) NOT NULL,
        summary_status VARCHAR(16) NOT NULL
          CHECK (summary_status IN ('active','superseded','revoked','deleted')),
        supersedes_summary_id UUID REFERENCES memory.summaries(id),
        quality_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (conversation_no, end_message_no, prompt_version)
        )"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE memory.summaries")
    op.execute(
        """CREATE TABLE memory.summaries (
        id BIGSERIAL PRIMARY KEY,
        summary_no VARCHAR(40) NOT NULL UNIQUE,
        conversation_no VARCHAR(40) NOT NULL,
        user_no VARCHAR(40) NOT NULL,
        store_no VARCHAR(40),
        through_message_no VARCHAR(40) NOT NULL,
        safe_summary TEXT NOT NULL,
        summary_version BIGINT NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (conversation_no, through_message_no)
        )"""
    )
    op.execute("DROP INDEX IF EXISTS memory.idx_memory_events_user")
    op.execute("DROP INDEX IF EXISTS memory.idx_memory_events_memory")
    op.execute("ALTER TABLE memory.events DROP CONSTRAINT fk_memory_events_item")
    op.execute("ALTER TABLE memory.events ADD COLUMN memory_no VARCHAR(40)")
    op.execute(
        """UPDATE memory.events event SET memory_no=item.memory_no
        FROM memory.items item WHERE event.memory_id=item.id"""
    )
    op.execute("ALTER TABLE memory.events ALTER COLUMN memory_no SET NOT NULL")
    op.execute("ALTER TABLE memory.events DROP CONSTRAINT events_pkey")
    op.execute("ALTER TABLE memory.events DROP COLUMN id")
    op.execute("ALTER TABLE memory.events ADD COLUMN id BIGSERIAL PRIMARY KEY")
    for column in ("ai_run_no", "source_message_no", "consent_no", "memory_id"):
        op.execute(f"ALTER TABLE memory.events DROP COLUMN {column}")
    op.execute("ALTER TABLE memory.events ALTER COLUMN user_no DROP NOT NULL")
    op.execute("ALTER TABLE memory.events ALTER COLUMN to_status DROP NOT NULL")
    op.execute("ALTER TABLE memory.events ALTER COLUMN actor_no DROP NOT NULL")
    op.execute("ALTER TABLE memory.events ALTER COLUMN reason_code DROP NOT NULL")
    op.execute(
        "CREATE INDEX idx_memory_events_memory ON memory.events(memory_no, created_at, id)"
    )

    op.execute("DROP TABLE memory.item_embeddings")
    for constraint in (
        "ck_memory_items_expiry",
        "ck_memory_items_risk",
        "ck_memory_items_classification",
        "ck_memory_items_content_lifecycle",
        "ck_memory_items_type",
        "ck_memory_items_namespace_store",
    ):
        op.execute(f"ALTER TABLE memory.items DROP CONSTRAINT {constraint}")
    op.execute("ALTER TABLE memory.items ADD COLUMN safe_text TEXT")
    op.execute("ALTER TABLE memory.items ADD COLUMN embedding VECTOR(1536)")
    op.execute("ALTER TABLE memory.items ALTER COLUMN consent_no DROP NOT NULL")
    op.execute("ALTER TABLE memory.items ALTER COLUMN expires_at DROP NOT NULL")
    op.execute("ALTER TABLE memory.items DROP CONSTRAINT fk_memory_items_supersedes")
    op.execute("ALTER TABLE memory.items ADD COLUMN id_big BIGSERIAL")
    op.execute("ALTER TABLE memory.items ADD COLUMN supersedes_memory_big BIGINT")
    op.execute(
        """UPDATE memory.items child SET supersedes_memory_big=parent.id_big
        FROM memory.items parent WHERE child.supersedes_memory_id=parent.id"""
    )
    op.execute("ALTER TABLE memory.items DROP CONSTRAINT items_pkey")
    op.execute("ALTER TABLE memory.items DROP COLUMN supersedes_memory_id")
    op.execute("ALTER TABLE memory.items DROP COLUMN id")
    op.execute("ALTER TABLE memory.items RENAME COLUMN id_big TO id")
    op.execute(
        "ALTER TABLE memory.items RENAME COLUMN supersedes_memory_big TO supersedes_memory_id"
    )
    op.execute("ALTER TABLE memory.items ADD PRIMARY KEY (id)")
    op.execute(
        """ALTER TABLE memory.items ADD CONSTRAINT items_supersedes_memory_id_fkey
        FOREIGN KEY (supersedes_memory_id) REFERENCES memory.items(id)"""
    )
    op.execute(
        "CREATE INDEX idx_memory_items_embedding_hnsw ON memory.items "
        "USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128)"
    )
