from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY, INTEGER, VARBINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import AppendOnlyMySQLModel, MutableMySQLModel, MySQLBase


class AgentDefinition(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_agent_definitions"
    __table_args__ = (
        UniqueConstraint("agent_no", name="uk_ai_agent_definitions_no"),
        CheckConstraint(
            "(scope_type = 'platform' AND store_id IS NULL) OR "
            "(scope_type = 'store' AND store_id IS NOT NULL)",
            name="agent_definition_scope",
        ),
        Index("idx_agent_definitions_scope", "scope_type", "store_id", "agent_status", "id"),
    )

    agent_no: Mapped[str] = mapped_column(String(40), nullable=False)
    agent_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False, default="platform")
    store_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("stores.id"))
    strategy_reuse_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class AgentVersion(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version_no", name="uk_ai_agent_versions_number"),
        Index("idx_agent_versions_status", "agent_id", "version_status", "version_no"),
    )

    agent_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_agent_definitions.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    version_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_allowlist: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    policy_config: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class AgentRun(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_agent_runs"
    __table_args__ = (
        UniqueConstraint("run_no", name="uk_ai_agent_runs_no"),
        UniqueConstraint("trigger_message_id", name="uk_ai_agent_runs_trigger_message"),
        Index("idx_agent_runs_conversation_time", "conversation_id", "created_at", "id"),
    )

    run_no: Mapped[str] = mapped_column(String(40), nullable=False)
    conversation_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("conversations.id"), nullable=False
    )
    trigger_message_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("messages.id"), nullable=False
    )
    response_message_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("messages.id")
    )
    agent_version_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_agent_versions.id"), nullable=False
    )
    run_status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    current_phase: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    public_output: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    context_no: Mapped[str | None] = mapped_column(String(40))
    context_version: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    context_snapshot: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    degraded_reason: Mapped[str | None] = mapped_column(String(64))


class AgentDelegation(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_agent_delegations"
    __table_args__ = (
        UniqueConstraint("delegation_no", name="uk_ai_agent_delegations_no"),
        UniqueConstraint("fingerprint", name="uk_ai_agent_delegations_fingerprint"),
        UniqueConstraint(
            "run_id",
            "subtask_key",
            "specialist_version",
            name="uk_ai_agent_delegations_run_subtask_version",
        ),
        CheckConstraint("depth = 1", name="agent_delegation_depth"),
        CheckConstraint(
            "delegation_status IN "
            "('queued','running','succeeded','partial','failed','denied','unknown','cancelled')",
            name="agent_delegation_status",
        ),
        Index("idx_ai_agent_delegations_run_status", "run_id", "delegation_status", "id"),
    )

    delegation_no: Mapped[str] = mapped_column(String(40), nullable=False)
    run_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_agent_runs.id"), nullable=False
    )
    subtask_key: Mapped[str] = mapped_column(String(128), nullable=False)
    specialist_code: Mapped[str] = mapped_column(String(64), nullable=False)
    specialist_version: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    depth: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    delegation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    objective_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    scope_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    resource_refs: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    dependency_nos: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    allowed_tools_snapshot: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    budget_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    result_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSON)
    tokens_used: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default="0"
    )
    tool_calls: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default="0"
    )
    model_calls: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default="0"
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    span_id: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class AgentToolAudit(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "ai_agent_tool_audits"
    __table_args__ = (UniqueConstraint("audit_no", name="uk_ai_agent_tool_audits_no"),)

    audit_no: Mapped[str] = mapped_column(String(40), nullable=False)
    run_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_agent_runs.id"), nullable=False
    )
    tool_code: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    arguments_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=0, server_default="0"
    )


class UserAgentConsent(MutableMySQLModel, MySQLBase):
    __tablename__ = "user_agent_consents"
    __table_args__ = (
        UniqueConstraint("consent_no", name="uk_user_agent_consents_no"),
        Index("idx_user_agent_consents_user", "user_id", "consent_status", "created_at", "id"),
    )

    consent_no: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    consent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_no: Mapped[str | None] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    consent_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class AgentRefundDraft(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_refund_drafts"
    __table_args__ = (
        UniqueConstraint("draft_no", name="uk_ai_refund_drafts_no"),
        UniqueConstraint("run_id", name="uk_ai_refund_drafts_run"),
        CheckConstraint(
            "draft_status IN ('active','consumed','expired','invalidated')",
            name="ai_refund_draft_status",
        ),
        Index("idx_ai_refund_drafts_expiry", "draft_status", "expires_at", "id"),
    )

    draft_no: Mapped[str] = mapped_column(String(40), nullable=False)
    run_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_agent_runs.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("orders.id"), nullable=False
    )
    draft_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    eligibility_token_ciphertext: Mapped[bytes] = mapped_column(VARBINARY(4096), nullable=False)
    arguments_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    draft_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class AgentToolApproval(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_tool_approvals"
    __table_args__ = (
        UniqueConstraint("approval_no", name="uk_ai_tool_approvals_no"),
        UniqueConstraint("run_id", "action_type", name="uk_ai_tool_approvals_run_action"),
        CheckConstraint(
            "approval_status IN ('pending','approved','rejected','expired','consumed')",
            name="ai_tool_approval_status",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approve','reject')",
            name="ai_tool_approval_decision",
        ),
        Index("idx_ai_tool_approvals_user", "user_id", "approval_status", "expires_at", "id"),
    )

    approval_no: Mapped[str] = mapped_column(String(40), nullable=False)
    run_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_agent_runs.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    conversation_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("conversations.id"), nullable=False
    )
    draft_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_refund_drafts.id"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    resource_versions: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    approval_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decision: Mapped[str | None] = mapped_column(String(16))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class AgentToolAction(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_tool_actions"
    __table_args__ = (
        UniqueConstraint("action_no", name="uk_ai_tool_actions_no"),
        UniqueConstraint("approval_id", name="uk_ai_tool_actions_approval"),
        CheckConstraint(
            "action_status IN ('pending','running','succeeded','failed','outcome_unknown')",
            name="ai_tool_action_status",
        ),
        Index("idx_ai_tool_actions_status", "action_status", "updated_at", "id"),
    )

    action_no: Mapped[str] = mapped_column(String(40), nullable=False)
    approval_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_tool_approvals.id"), nullable=False
    )
    run_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_agent_runs.id"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    action_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    resource_no: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
