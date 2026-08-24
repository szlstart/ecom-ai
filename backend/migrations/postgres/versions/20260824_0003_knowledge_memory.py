"""Add versioned knowledge, retrieval, and memory storage.

Revision ID: pg_20260824_0003
Revises: pg_20260824_0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "pg_20260824_0003"
down_revision: str | None = "pg_20260824_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE knowledge.embedding_models (
            model_code VARCHAR(64) PRIMARY KEY,
            provider VARCHAR(64) NOT NULL,
            dimension INTEGER NOT NULL CHECK (dimension = 1536),
            model_status VARCHAR(16) NOT NULL CHECK (model_status IN ('active','retired')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """INSERT INTO knowledge.embedding_models
        (model_code, provider, dimension, model_status)
        VALUES ('ecom-multilingual-v1', 'configured-provider', 1536, 'active')"""
    )
    op.execute(
        """
        CREATE TABLE knowledge.index_generations (
            generation_no VARCHAR(40) PRIMARY KEY,
            document_no VARCHAR(40) NOT NULL,
            scope_type VARCHAR(16) NOT NULL CHECK (scope_type IN ('platform','store')),
            scope_no VARCHAR(64) NOT NULL,
            model_code VARCHAR(64) NOT NULL REFERENCES knowledge.embedding_models(model_code),
            generation_status VARCHAR(16) NOT NULL
                CHECK (generation_status IN ('building','active','retired','failed')),
            activated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (document_no, generation_no)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uk_index_generations_active_scope
        ON knowledge.index_generations(document_no)
        WHERE generation_status = 'active'
        """
    )
    op.execute(
        """
        CREATE TABLE knowledge.document_chunks (
            id BIGSERIAL PRIMARY KEY,
            chunk_no VARCHAR(40) NOT NULL UNIQUE,
            document_no VARCHAR(40) NOT NULL,
            content_version VARCHAR(40) NOT NULL,
            generation_no VARCHAR(40),
            scope_type VARCHAR(16) NOT NULL CHECK (scope_type IN ('platform','store')),
            scope_no VARCHAR(64) NOT NULL,
            safe_text TEXT NOT NULL,
            search_vector TSVECTOR GENERATED ALWAYS AS
                (to_tsvector('simple', coalesce(safe_text, ''))) STORED,
            embedding VECTOR(1536),
            embedding_model_code VARCHAR(64) NOT NULL DEFAULT 'ecom-multilingual-v1',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (generation_no) REFERENCES knowledge.index_generations(generation_no),
            FOREIGN KEY (embedding_model_code)
                REFERENCES knowledge.embedding_models(model_code),
            UNIQUE (document_no, content_version, chunk_no)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_document_chunks_acl ON knowledge.document_chunks"
        "(scope_type, scope_no, document_no, content_version)"
    )
    op.execute(
        "CREATE INDEX idx_document_chunks_fts ON knowledge.document_chunks USING GIN(search_vector)"
    )
    op.execute(
        "CREATE INDEX idx_document_chunks_embedding_hnsw ON knowledge.document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128)"
    )
    op.execute(
        """
        CREATE TABLE knowledge.indexing_jobs (
            id BIGSERIAL PRIMARY KEY,
            job_no VARCHAR(40) NOT NULL UNIQUE,
            command_job_no VARCHAR(40) NOT NULL UNIQUE,
            scope_type VARCHAR(16) NOT NULL CHECK (scope_type IN ('platform','store')),
            scope_no VARCHAR(64) NOT NULL,
            generation_no VARCHAR(40),
            job_status VARCHAR(16) NOT NULL
                CHECK (job_status IN ('queued','running','succeeded','failed','cancelled')),
            progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
            status_version BIGINT NOT NULL DEFAULT 1,
            error_code VARCHAR(64),
            error_owner VARCHAR(16) CHECK (error_owner IN ('command','execution','dependency')),
            cancel_requested_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (generation_no) REFERENCES knowledge.index_generations(generation_no)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_indexing_jobs_status ON knowledge.indexing_jobs"
        "(job_status, updated_at, id)"
    )
    op.execute(
        """
        CREATE TABLE knowledge.retrieval_logs (
            id BIGSERIAL PRIMARY KEY,
            retrieval_no VARCHAR(40) NOT NULL UNIQUE,
            trace_id VARCHAR(64) NOT NULL,
            query_hash BYTEA NOT NULL,
            scope_type VARCHAR(16) NOT NULL,
            scope_no VARCHAR(64) NOT NULL,
            embedding_model_code VARCHAR(64) NOT NULL,
            candidate_count INTEGER NOT NULL,
            returned_count INTEGER NOT NULL,
            degraded BOOLEAN NOT NULL DEFAULT false,
            latency_ms INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_retrieval_logs_scope_time ON knowledge.retrieval_logs"
        "(scope_type, scope_no, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE memory.items (
            id BIGSERIAL PRIMARY KEY,
            memory_no VARCHAR(40) NOT NULL UNIQUE,
            user_no VARCHAR(40) NOT NULL,
            agent_scope VARCHAR(16) NOT NULL CHECK (agent_scope IN ('exclusive','store')),
            store_no VARCHAR(40),
            memory_type VARCHAR(24) NOT NULL,
            safe_text TEXT NOT NULL,
            embedding VECTOR(1536),
            confidence NUMERIC(5,4) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
            memory_status VARCHAR(16) NOT NULL
                CHECK (memory_status IN ('candidate','active','superseded','expired','deleted')),
            consent_no VARCHAR(40),
            expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK ((agent_scope='exclusive' AND store_no IS NULL) OR
                   (agent_scope='store' AND store_no IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_memory_items_owner ON memory.items"
        "(user_no, agent_scope, store_no, memory_status, updated_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_memory_items_embedding_hnsw ON memory.items "
        "USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128)"
    )
    op.execute(
        """
        CREATE TABLE memory.events (
            id BIGSERIAL PRIMARY KEY,
            event_no VARCHAR(40) NOT NULL UNIQUE,
            memory_no VARCHAR(40) NOT NULL,
            event_type VARCHAR(24) NOT NULL,
            actor_type VARCHAR(16) NOT NULL,
            reason_code VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_memory_events_memory ON memory.events(memory_no, created_at, id)"
    )
    op.execute(
        """
        CREATE TABLE memory.summaries (
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
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE memory.summaries")
    op.execute("DROP TABLE memory.events")
    op.execute("DROP TABLE memory.items")
    op.execute("DROP TABLE knowledge.retrieval_logs")
    op.execute("DROP TABLE knowledge.indexing_jobs")
    op.execute("DROP TABLE knowledge.document_chunks")
    op.execute("DROP TABLE knowledge.index_generations")
    op.execute("DROP TABLE knowledge.embedding_models")
