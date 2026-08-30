"""Switch semantic retrieval storage to Tongyi vision flash 768 dimensions.

Revision ID: pg_20260830_0006
Revises: pg_20260825_0005
"""

from collections.abc import Sequence

from alembic import op

revision: str = "pg_20260830_0006"
down_revision: str | None = "pg_20260825_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODEL_CODE = "tongyi-embedding-vision-flash"
LEGACY_MODEL_CODE = "ecom-multilingual-v1"


def upgrade() -> None:
    # Embeddings are reproducible projections, not source records. Existing vectors cannot be
    # converted between model spaces and are intentionally invalidated before re-indexing.
    op.execute("DROP INDEX IF EXISTS knowledge.idx_document_chunks_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS memory.idx_memory_item_embeddings_hnsw")
    op.execute("UPDATE knowledge.document_chunks SET embedding=NULL")
    op.execute("DELETE FROM memory.item_embeddings")
    op.execute(
        "ALTER TABLE knowledge.embedding_models "
        "DROP CONSTRAINT IF EXISTS embedding_models_dimension_check"
    )
    op.execute(
        "ALTER TABLE knowledge.embedding_models ADD CONSTRAINT ck_embedding_models_dimension "
        "CHECK (dimension BETWEEN 1 AND 4096)"
    )
    op.execute(
        f"UPDATE knowledge.embedding_models SET model_status='retired' "
        f"WHERE model_code='{LEGACY_MODEL_CODE}'"
    )
    op.execute(
        f"""INSERT INTO knowledge.embedding_models
        (model_code, provider, dimension, model_status)
        VALUES ('{MODEL_CODE}', 'aliyun-dashscope', 768, 'active')
        ON CONFLICT (model_code) DO UPDATE SET
          provider=EXCLUDED.provider, dimension=EXCLUDED.dimension, model_status='active'"""
    )
    op.execute(
        "ALTER TABLE knowledge.document_chunks ALTER COLUMN embedding "
        "TYPE VECTOR(768) USING NULL::VECTOR(768)"
    )
    op.execute(
        f"ALTER TABLE knowledge.document_chunks ALTER COLUMN embedding_model_code "
        f"SET DEFAULT '{MODEL_CODE}'"
    )
    op.execute(
        "ALTER TABLE memory.item_embeddings DROP CONSTRAINT IF EXISTS "
        "item_embeddings_dimension_check"
    )
    op.execute(
        "ALTER TABLE memory.item_embeddings ALTER COLUMN embedding "
        "TYPE VECTOR(768) USING NULL::VECTOR(768)"
    )
    op.execute(
        "ALTER TABLE memory.item_embeddings ADD CONSTRAINT ck_memory_embedding_dimension "
        "CHECK (dimension=768)"
    )
    op.execute(
        "CREATE INDEX idx_document_chunks_embedding_hnsw ON knowledge.document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128)"
    )
    op.execute(
        "CREATE INDEX idx_memory_item_embeddings_hnsw ON memory.item_embeddings "
        "USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128) "
        "WHERE embedding_status='active'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS knowledge.idx_document_chunks_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS memory.idx_memory_item_embeddings_hnsw")
    op.execute("UPDATE knowledge.document_chunks SET embedding=NULL")
    op.execute("DELETE FROM memory.item_embeddings")
    op.execute(
        f"UPDATE knowledge.index_generations SET model_code='{LEGACY_MODEL_CODE}' "
        f"WHERE model_code='{MODEL_CODE}'"
    )
    op.execute(
        f"UPDATE knowledge.document_chunks SET embedding_model_code='{LEGACY_MODEL_CODE}' "
        f"WHERE embedding_model_code='{MODEL_CODE}'"
    )
    op.execute(f"DELETE FROM knowledge.embedding_models WHERE model_code='{MODEL_CODE}'")
    op.execute(
        f"UPDATE knowledge.embedding_models SET model_status='active' "
        f"WHERE model_code='{LEGACY_MODEL_CODE}'"
    )
    op.execute(
        "ALTER TABLE knowledge.embedding_models "
        "DROP CONSTRAINT ck_embedding_models_dimension"
    )
    op.execute(
        "ALTER TABLE knowledge.embedding_models ADD CONSTRAINT embedding_models_dimension_check "
        "CHECK (dimension=1536)"
    )
    op.execute(
        "ALTER TABLE knowledge.document_chunks ALTER COLUMN embedding "
        "TYPE VECTOR(1536) USING NULL::VECTOR(1536)"
    )
    op.execute(
        f"ALTER TABLE knowledge.document_chunks ALTER COLUMN embedding_model_code "
        f"SET DEFAULT '{LEGACY_MODEL_CODE}'"
    )
    op.execute("ALTER TABLE memory.item_embeddings DROP CONSTRAINT ck_memory_embedding_dimension")
    op.execute(
        "ALTER TABLE memory.item_embeddings ALTER COLUMN embedding "
        "TYPE VECTOR(1536) USING NULL::VECTOR(1536)"
    )
    op.execute(
        "ALTER TABLE memory.item_embeddings ADD CONSTRAINT item_embeddings_dimension_check "
        "CHECK (dimension=1536)"
    )
    op.execute(
        "CREATE INDEX idx_document_chunks_embedding_hnsw ON knowledge.document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128)"
    )
    op.execute(
        "CREATE INDEX idx_memory_item_embeddings_hnsw ON memory.item_embeddings "
        "USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128) "
        "WHERE embedding_status='active'"
    )
