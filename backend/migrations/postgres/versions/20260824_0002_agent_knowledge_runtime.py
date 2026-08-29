"""Create agent runtime state and checkpoint tables.

Revision ID: pg_20260824_0002
Revises: pg_20260822_0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "pg_20260824_0002"
down_revision: str | None = "pg_20260822_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_runtime.run_state_refs (
            id BIGSERIAL PRIMARY KEY,
            run_no VARCHAR(40) NOT NULL UNIQUE,
            thread_id VARCHAR(128) NOT NULL,
            conversation_no VARCHAR(40) NOT NULL,
            trigger_message_no VARCHAR(40) NOT NULL,
            user_no VARCHAR(40) NOT NULL,
            store_no VARCHAR(40),
            agent_version_no VARCHAR(64) NOT NULL,
            graph_version VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL,
            current_phase VARCHAR(32) NOT NULL,
            last_checkpoint_id VARCHAR(128),
            lease_owner VARCHAR(128),
            lease_expires_at TIMESTAMPTZ,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            trace_id VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_run_state_refs_status "
        "ON agent_runtime.run_state_refs(status, lease_expires_at, id)"
    )
    op.execute(
        """
        CREATE TABLE agent_runtime.checkpoints (
            id BIGSERIAL PRIMARY KEY,
            checkpoint_id VARCHAR(128) NOT NULL UNIQUE,
            run_no VARCHAR(40) NOT NULL,
            checkpoint_seq BIGINT NOT NULL CHECK(checkpoint_seq > 0),
            phase VARCHAR(32) NOT NULL,
            state_json JSONB NOT NULL,
            state_hash BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(run_no, checkpoint_seq)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_checkpoints_run "
        "ON agent_runtime.checkpoints(run_no, checkpoint_seq DESC)"
    )
    op.execute(
        """
        CREATE TABLE agent_runtime.checkpoint_writes (
            id BIGSERIAL PRIMARY KEY,
            write_no VARCHAR(40) NOT NULL UNIQUE,
            checkpoint_id VARCHAR(128) NOT NULL,
            run_no VARCHAR(40) NOT NULL,
            write_type VARCHAR(32) NOT NULL,
            write_status VARCHAR(16) NOT NULL,
            payload_hash BYTEA NOT NULL,
            error_code VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_checkpoint_writes_run "
        "ON agent_runtime.checkpoint_writes(run_no, created_at, id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE agent_runtime.checkpoint_writes")
    op.execute("DROP TABLE agent_runtime.checkpoints")
    op.execute("DROP TABLE agent_runtime.run_state_refs")
