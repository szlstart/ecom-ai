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
from sqlalchemy.dialects.mysql import BIGINT, INTEGER
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import MutableMySQLModel, MySQLBase


class SkillDefinition(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_skill_definitions"
    __table_args__ = (
        UniqueConstraint("skill_no", name="uk_ai_skill_definitions_no"),
        UniqueConstraint("skill_code", name="uq_ai_skill_definitions_code"),
    )

    skill_no: Mapped[str] = mapped_column(String(40), nullable=False)
    skill_code: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class SkillVersion(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_skill_versions"
    __table_args__ = (
        UniqueConstraint("skill_id", "version_no", name="uk_ai_skill_versions_number"),
    )

    skill_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_skill_definitions.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    version_status: Mapped[str] = mapped_column(String(16), nullable=False)
    input_schema: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    output_schema: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    evaluation_report: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class ToolDefinition(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_tool_definitions"
    __table_args__ = (UniqueConstraint("tool_code", name="uk_ai_tool_definitions_code"),)

    tool_code: Mapped[str] = mapped_column(String(128), nullable=False)
    server_code: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    tool_status: Mapped[str] = mapped_column(String(16), nullable=False)


class ToolVersion(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_tool_versions"
    __table_args__ = (
        UniqueConstraint("tool_id", "version_no", name="uk_ai_tool_versions_number"),
        Index("idx_ai_tool_versions_status", "tool_id", "version_status", "version_no"),
    )

    tool_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_tool_definitions.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    version_status: Mapped[str] = mapped_column(String(16), nullable=False)
    input_schema: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    output_schema: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    evaluation_report: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class KnowledgeDocument(MutableMySQLModel, MySQLBase):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("document_no", name="uk_knowledge_documents_no"),
        Index("idx_knowledge_documents_scope", "scope_type", "scope_no", "document_status"),
    )

    document_no: Mapped[str] = mapped_column(String(40), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_no: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    safe_text: Mapped[str] = mapped_column(Text, nullable=False)
    document_status: Mapped[str] = mapped_column(String(16), nullable=False)
    content_version: Mapped[str] = mapped_column(String(40), nullable=False)


class AgentSkillBinding(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_agent_skill_bindings"
    __table_args__ = (
        UniqueConstraint("agent_version_id", "skill_version_id", name="uk_ai_agent_skill_binding"),
    )

    agent_version_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_agent_versions.id"), nullable=False
    )
    skill_version_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_skill_versions.id"), nullable=False
    )
    binding_status: Mapped[str] = mapped_column(String(16), nullable=False)


class SkillToolBinding(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_skill_tool_bindings"
    __table_args__ = (
        UniqueConstraint("skill_version_id", "tool_version_id", name="uk_ai_skill_tool_binding"),
        CheckConstraint("permission_effect IN ('allow','deny')", name="skill_tool_effect"),
    )

    skill_version_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_skill_versions.id"), nullable=False
    )
    tool_version_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("ai_tool_versions.id"), nullable=False
    )
    permission_effect: Mapped[str] = mapped_column(String(16), nullable=False)
    confirmation_policy: Mapped[str] = mapped_column(String(24), nullable=False)
    call_budget: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    timeout_ms: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)


class RuntimeKillSwitch(MutableMySQLModel, MySQLBase):
    __tablename__ = "ai_runtime_kill_switches"
    __table_args__ = (
        UniqueConstraint("switch_no", name="uk_ai_runtime_kill_switch_no"),
        UniqueConstraint("target_type", "target_code", name="uk_ai_runtime_kill_target"),
        CheckConstraint(
            "target_type IN ('agent','skill','tool','mcp_server')",
            name="ai_kill_target_type",
        ),
    )

    switch_no: Mapped[str] = mapped_column(String(40), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_code: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    changed_by: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
