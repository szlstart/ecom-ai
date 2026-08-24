"""add immutable AI evaluation run records

Revision ID: r72a5b6c7d8e
Revises: q61f4a5b6c7d
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "r72a5b6c7d8e"
down_revision = "q61f4a5b6c7d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_evaluation_runs",
        sa.Column("evaluation_run_no", sa.String(40), nullable=False),
        sa.Column("dataset_id", sa.String(128), nullable=False),
        sa.Column("dataset_version", sa.String(40), nullable=False),
        sa.Column("dataset_hash", mysql.BINARY(32), nullable=False),
        sa.Column("baseline_type", sa.String(32), nullable=False),
        sa.Column("baseline_version", sa.String(128), nullable=False),
        sa.Column("candidate_type", sa.String(32), nullable=False),
        sa.Column("candidate_version", sa.String(128), nullable=False),
        sa.Column("require_significant_gain", sa.Boolean(), nullable=False),
        sa.Column("run_status", sa.String(32), nullable=False),
        sa.Column("release_gate", sa.String(32)),
        sa.Column("report", sa.JSON()),
        sa.Column("requested_by", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("utc_timestamp(6)"), nullable=False
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.UniqueConstraint("evaluation_run_no", name="uk_ai_evaluation_runs_no"),
        sa.CheckConstraint(
            "run_status IN ('queued','running','completed','failed','cancelled')",
            name="ai_evaluation_run_status",
        ),
        sa.CheckConstraint(
            "release_gate IS NULL OR release_gate IN ('pass','fail','insufficient_evidence')",
            name="ai_evaluation_release_gate",
        ),
    )
    op.create_index(
        "idx_ai_evaluation_runs_status", "ai_evaluation_runs", ["run_status", "created_at", "id"]
    )
    op.create_index(
        "idx_ai_evaluation_runs_candidate",
        "ai_evaluation_runs",
        ["candidate_type", "candidate_version", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_ai_evaluation_runs_candidate", table_name="ai_evaluation_runs")
    op.drop_index("idx_ai_evaluation_runs_status", table_name="ai_evaluation_runs")
    op.drop_table("ai_evaluation_runs")
