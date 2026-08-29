"""secure store agent scope, context snapshots, and tool audits

Revision ID: n38c9d1e2f3a
Revises: m27b8c0d1e2f
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "n38c9d1e2f3a"
down_revision: str | Sequence[str] | None = "m27b8c0d1e2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_agent_definitions",
        sa.Column("scope_type", sa.String(16), server_default="platform", nullable=False),
    )
    op.add_column(
        "ai_agent_definitions",
        sa.Column("store_id", mysql.BIGINT(unsigned=True), nullable=True),
    )
    op.add_column(
        "ai_agent_definitions",
        sa.Column(
            "strategy_reuse_approved", sa.Boolean(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.create_foreign_key(
        "fk_ai_agent_definitions_store_id_stores",
        "ai_agent_definitions",
        "stores",
        ["store_id"],
        ["id"],
    )
    op.create_check_constraint(
        "agent_definition_scope",
        "ai_agent_definitions",
        "(scope_type = 'platform' AND store_id IS NULL) OR "
        "(scope_type = 'store' AND store_id IS NOT NULL)",
    )
    op.create_index(
        "idx_agent_definitions_scope",
        "ai_agent_definitions",
        ["scope_type", "store_id", "agent_status", "id"],
    )

    op.add_column(
        "ai_agent_runs",
        sa.Column("context_snapshot", sa.JSON(), nullable=True),
    )
    op.execute("UPDATE ai_agent_runs SET context_snapshot = JSON_ARRAY()")
    op.alter_column(
        "ai_agent_runs",
        "context_snapshot",
        existing_type=sa.JSON(),
        nullable=False,
    )
    op.add_column("ai_agent_runs", sa.Column("degraded_reason", sa.String(64), nullable=True))

    op.add_column(
        "ai_agent_tool_audits",
        sa.Column("arguments_hash", mysql.BINARY(32), nullable=True),
    )
    op.execute(
        "UPDATE ai_agent_tool_audits "
        "SET arguments_hash = UNHEX(SHA2(CONCAT(tool_code, CHAR(58), 'legacy'), 256))"
    )
    op.alter_column(
        "ai_agent_tool_audits",
        "arguments_hash",
        existing_type=mysql.BINARY(32),
        nullable=False,
    )
    op.add_column("ai_agent_tool_audits", sa.Column("error_code", sa.String(64), nullable=True))
    op.add_column(
        "ai_agent_tool_audits",
        sa.Column("latency_ms", mysql.INTEGER(unsigned=True), server_default="0", nullable=False),
    )
    op.execute(
        """
        INSERT INTO ai_agent_definitions (
            agent_no, agent_code, agent_type, scope_type, store_id,
            strategy_reuse_approved, display_name, agent_status,
            created_at, updated_at, version
        )
        SELECT
            'agt_01K3STOREAGENT000000000001', 'store_support', 'store_service',
            'platform', NULL, 1, '店铺客服', 'active', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), 0
        WHERE NOT EXISTS (
            SELECT 1 FROM ai_agent_definitions WHERE agent_code = 'store_support'
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
            '仅服务可信会话注入的当前店铺; 实时事实必须来自只读工具; 禁止交易写操作。',
            'deterministic-store-v1',
            JSON_ARRAY(
                'catalog.get_product', 'catalog.compare_skus',
                'catalog.compare_products', 'catalog.search_store_products',
                'catalog.get_inventory_availability', 'catalog.get_store_policy',
                'order.get_store_order_summary', 'logistics.get_store_order_shipments',
                'support.create_store_ticket', 'support.get_ticket_status'
            ),
            JSON_OBJECT(
                'transaction_write', 'none',
                'max_tool_calls', 4,
                'max_output_chars', 4000,
                'untrusted_business_content', TRUE
            ),
            UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), 0
        FROM ai_agent_definitions AS definition
        WHERE definition.agent_code = 'store_support'
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
        "WHERE definition.agent_code = 'store_support' AND version.version_no = 1 "
        "AND version.model_profile = 'deterministic-store-v1'"
    )
    op.execute(
        "DELETE FROM ai_agent_definitions WHERE agent_code = 'store_support' "
        "AND agent_no = 'agt_01K3STOREAGENT000000000001'"
    )
    op.drop_column("ai_agent_tool_audits", "latency_ms")
    op.drop_column("ai_agent_tool_audits", "error_code")
    op.drop_column("ai_agent_tool_audits", "arguments_hash")
    op.drop_column("ai_agent_runs", "degraded_reason")
    op.drop_column("ai_agent_runs", "context_snapshot")
    op.drop_index("idx_agent_definitions_scope", table_name="ai_agent_definitions")
    op.drop_constraint("agent_definition_scope", "ai_agent_definitions", type_="check")
    op.drop_constraint(
        "fk_ai_agent_definitions_store_id_stores",
        "ai_agent_definitions",
        type_="foreignkey",
    )
    op.drop_column("ai_agent_definitions", "strategy_reuse_approved")
    op.drop_column("ai_agent_definitions", "store_id")
    op.drop_column("ai_agent_definitions", "scope_type")
