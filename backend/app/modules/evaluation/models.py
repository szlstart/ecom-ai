from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import MutableMySQLModel, MySQLBase


class AiEvaluationRun(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_evaluation_runs"
    __table_args__ = (
        UniqueConstraint("evaluation_run_no", name="uk_ai_evaluation_runs_no"),
        CheckConstraint(
            "run_status IN ('queued','running','completed','failed','cancelled')",
            name="ai_evaluation_run_status",
        ),
        CheckConstraint(
            "release_gate IS NULL OR release_gate IN ('pass','fail','insufficient_evidence')",
            name="ai_evaluation_release_gate",
        ),
        Index("idx_ai_evaluation_runs_status", "run_status", "created_at", "id"),
        Index("idx_ai_evaluation_runs_candidate", "candidate_type", "candidate_version", "id"),
    )

    evaluation_run_no: Mapped[str] = mapped_column(String(40), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(40), nullable=False)
    dataset_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    baseline_type: Mapped[str] = mapped_column(String(32), nullable=False)
    baseline_version: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_version: Mapped[str] = mapped_column(String(128), nullable=False)
    require_significant_gain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    run_status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    release_gate: Mapped[str | None] = mapped_column(String(32))
    report: Mapped[dict[str, object] | None] = mapped_column(JSON)
    requested_by: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    error_code: Mapped[str | None] = mapped_column(String(64))
