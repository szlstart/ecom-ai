"""add exclusive agent refund drafts and action approvals

Revision ID: o49d2e3f4a5b
Revises: n38c9d1e2f3a
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "o49d2e3f4a5b"
down_revision: str | Sequence[str] | None = "n38c9d1e2f3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _mutable_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
    ]


def upgrade() -> None:
    op.alter_column(
        "ai_agent_definitions",
        "agent_type",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
    )
    op.create_table(
        "ai_refund_drafts",
        sa.Column("draft_no", sa.String(40), nullable=False),
        sa.Column("run_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("order_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("draft_payload", sa.JSON(), nullable=False),
        sa.Column("eligibility_token_ciphertext", mysql.VARBINARY(4096), nullable=False),
        sa.Column("arguments_hash", mysql.BINARY(32), nullable=False),
        sa.Column("draft_status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        *_mutable_columns(),
        sa.CheckConstraint(
            "draft_status IN ('active','consumed','expired','invalidated')",
            name="ai_refund_draft_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["ai_agent_runs.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_ai_refund_drafts"),
        sa.UniqueConstraint("draft_no", name="uk_ai_refund_drafts_no"),
        sa.UniqueConstraint("run_id", name="uk_ai_refund_drafts_run"),
    )
    op.create_index(
        "idx_ai_refund_drafts_expiry",
        "ai_refund_drafts",
        ["draft_status", "expires_at", "id"],
    )
    op.create_table(
        "ai_tool_approvals",
        sa.Column("approval_no", sa.String(40), nullable=False),
        sa.Column("run_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("conversation_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("draft_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("arguments_hash", mysql.BINARY(32), nullable=False),
        sa.Column("resource_versions", sa.JSON(), nullable=False),
        sa.Column("approval_status", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        *_mutable_columns(),
        sa.CheckConstraint(
            "approval_status IN ('pending','approved','rejected','expired','consumed')",
            name="ai_tool_approval_status",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('approve','reject')",
            name="ai_tool_approval_decision",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["ai_agent_runs.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["draft_id"], ["ai_refund_drafts.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_ai_tool_approvals"),
        sa.UniqueConstraint("approval_no", name="uk_ai_tool_approvals_no"),
        sa.UniqueConstraint("run_id", "action_type", name="uk_ai_tool_approvals_run_action"),
    )
    op.create_index(
        "idx_ai_tool_approvals_user",
        "ai_tool_approvals",
        ["user_id", "approval_status", "expires_at", "id"],
    )
    op.create_table(
        "ai_tool_actions",
        sa.Column("action_no", sa.String(40), nullable=False),
        sa.Column("approval_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("run_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("arguments_hash", mysql.BINARY(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("action_status", sa.String(24), nullable=False),
        sa.Column("resource_no", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        *_mutable_columns(),
        sa.CheckConstraint(
            "action_status IN ('pending','running','succeeded','failed','outcome_unknown')",
            name="ai_tool_action_status",
        ),
        sa.ForeignKeyConstraint(["approval_id"], ["ai_tool_approvals.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["ai_agent_runs.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_ai_tool_actions"),
        sa.UniqueConstraint("action_no", name="uk_ai_tool_actions_no"),
        sa.UniqueConstraint("approval_id", name="uk_ai_tool_actions_approval"),
    )
    op.create_index(
        "idx_ai_tool_actions_status",
        "ai_tool_actions",
        ["action_status", "updated_at", "id"],
    )
    op.execute(
        """
        INSERT INTO ai_agent_definitions (
            agent_no, agent_code, agent_type, scope_type, store_id,
            strategy_reuse_approved, display_name, agent_status,
            created_at, updated_at, version
        )
        SELECT
            'agt_01K3EXCLUSIVEAGENT00000001', 'exclusive_support', 'exclusive_service',
            'platform', NULL, 0, '专属客服', 'active', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), 0
        WHERE NOT EXISTS (
            SELECT 1 FROM ai_agent_definitions WHERE agent_code = 'exclusive_support'
        )
        """
    )
    op.execute(
        """
        INSERT INTO ai_agent_versions (
            agent_id, version_no, version_status, system_prompt, model_profile,
            tool_allowlist, policy_config, published_at,
            created_at, updated_at, version
        )
        SELECT
            definition.id, 1, 'published',
            '仅服务可信注入的当前用户; 交易写必须消费结构化用户确认; 禁止读取其他用户数据。',
            'deterministic-exclusive-v1',
            JSON_ARRAY(
                'catalog.search_products', 'catalog.compare_products',
                'order.list_user_orders', 'order.get_user_order_detail',
                'logistics.get_user_order_shipments',
                'after_sale.check_refund_eligibility', 'after_sale.build_refund_draft',
                'after_sale.submit_refund_application', 'after_sale.list_user_refunds',
                'after_sale.get_user_refund_detail', 'support.create_platform_ticket',
                'support.get_ticket_status'
            ),
            JSON_OBJECT(
                'transaction_write', 'approval_required',
                'max_tool_calls', 6,
                'max_output_chars', 4000,
                'approval_ttl_minutes', 10
            ),
            UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), 0
        FROM ai_agent_definitions AS definition
        WHERE definition.agent_code = 'exclusive_support'
          AND NOT EXISTS (
              SELECT 1 FROM ai_agent_versions AS version
              WHERE version.agent_id = definition.id AND version.version_no = 1
          )
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE version FROM ai_agent_versions AS version "
        "JOIN ai_agent_definitions AS definition ON definition.id = version.agent_id "
        "WHERE definition.agent_code = 'exclusive_support' AND version.version_no = 1 "
        "AND version.model_profile = 'deterministic-exclusive-v1'"
    )
    op.execute(
        "DELETE FROM ai_agent_definitions WHERE agent_code = 'exclusive_support' "
        "AND agent_no = 'agt_01K3EXCLUSIVEAGENT00000001'"
    )
    op.drop_table("ai_tool_actions")
    op.drop_table("ai_tool_approvals")
    op.drop_table("ai_refund_drafts")
    op.alter_column(
        "ai_agent_definitions",
        "agent_type",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
    )
