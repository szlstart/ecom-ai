"""allow auditable manual logistics simulation events

Revision ID: ar38c1d2e3f4
Revises: aq27b0c1d2e3
"""

from alembic import op

revision = "ar38c1d2e3f4"
down_revision = "aq27b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE `logistics_sync_logs` "
        "DROP CHECK `ck_logistics_sync_logs_logistics_sync_type`"
    )
    op.execute(
        "ALTER TABLE `logistics_sync_logs` "
        "ADD CONSTRAINT `ck_logistics_sync_logs_logistics_sync_type` "
        "CHECK (sync_type IN ('poll', 'webhook', 'reconcile', 'manual'))"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM `logistics_sync_logs` WHERE `sync_type` = 'manual'"
    )
    op.execute(
        "ALTER TABLE `logistics_sync_logs` "
        "DROP CHECK `ck_logistics_sync_logs_logistics_sync_type`"
    )
    op.execute(
        "ALTER TABLE `logistics_sync_logs` "
        "ADD CONSTRAINT `ck_logistics_sync_logs_logistics_sync_type` "
        "CHECK (sync_type IN ('poll', 'webhook', 'reconcile'))"
    )
