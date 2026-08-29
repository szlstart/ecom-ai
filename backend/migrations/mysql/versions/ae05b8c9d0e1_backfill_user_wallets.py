"""backfill wallets for existing users

Revision ID: ae05b8c9d0e1
Revises: ad94a7b8c9d0
"""

from alembic import op

revision = "ae05b8c9d0e1"
down_revision = "ad94a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """INSERT INTO user_wallets
        (wallet_no,user_id,balance_amount,total_recharged_amount,currency,wallet_status,version)
        SELECT CONCAT('wal_migrated_', id),id,0,0,'CNY','active',0
        FROM users WHERE deleted_at IS NULL
        AND NOT EXISTS (SELECT 1 FROM user_wallets wallet WHERE wallet.user_id=users.id)
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM user_wallets WHERE wallet_no LIKE 'wal_migrated_%'")
