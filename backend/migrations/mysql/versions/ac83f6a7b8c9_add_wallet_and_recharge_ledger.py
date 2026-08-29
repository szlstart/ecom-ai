"""add user wallet and simulated recharge ledger

Revision ID: ac83f6a7b8c9
Revises: ab72e5f6a7b8
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "ac83f6a7b8c9"
down_revision = "ab72e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_wallets",
        sa.Column("wallet_no", sa.String(length=40), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column(
            "balance_amount", mysql.BIGINT(unsigned=True), server_default="0", nullable=False
        ),
        sa.Column(
            "total_recharged_amount",
            mysql.BIGINT(unsigned=True),
            server_default="0",
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("wallet_status", sa.String(length=16), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.CheckConstraint(
            "balance_amount >= 0", name="ck_user_wallets_user_wallet_balance_nonnegative"
        ),
        sa.CheckConstraint(
            "total_recharged_amount >= 0", name="ck_user_wallets_user_wallet_recharged_nonnegative"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_wallets_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_user_wallets"),
        sa.UniqueConstraint("wallet_no", name="uk_user_wallets_no"),
        sa.UniqueConstraint("user_id", "currency", name="uk_user_wallets_user_currency"),
    )
    op.create_table(
        "wallet_recharges",
        sa.Column("recharge_no", sa.String(length=40), nullable=False),
        sa.Column("wallet_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("recharge_status", sa.String(length=16), nullable=False),
        sa.Column("is_simulated", sa.Boolean(), nullable=False),
        sa.Column("provider_reference", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key_hash", mysql.BINARY(length=32), nullable=False),
        sa.Column("completed_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default="0", nullable=False),
        sa.CheckConstraint(
            "amount > 0", name="ck_wallet_recharges_wallet_recharge_amount_positive"
        ),
        sa.CheckConstraint(
            "channel IN ('wechat','alipay')", name="ck_wallet_recharges_wallet_recharge_channel"
        ),
        sa.CheckConstraint(
            "recharge_status = 'succeeded'", name="ck_wallet_recharges_wallet_recharge_status"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_wallet_recharges_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"], ["user_wallets.id"], name="fk_wallet_recharges_wallet_id_user_wallets"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_wallet_recharges"),
        sa.UniqueConstraint("idempotency_key_hash", name="uk_wallet_recharges_idempotency"),
        sa.UniqueConstraint("recharge_no", name="uk_wallet_recharges_no"),
    )
    op.create_index(
        "idx_wallet_recharges_user_time", "wallet_recharges", ["user_id", "created_at", "id"]
    )
    op.create_table(
        "wallet_transactions",
        sa.Column("transaction_no", sa.String(length=40), nullable=False),
        sa.Column("wallet_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("transaction_type", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("amount", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("balance_before", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("balance_after", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("business_type", sa.String(length=32), nullable=False),
        sa.Column("business_no", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("occurred_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("utc_timestamp(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount > 0", name="ck_wallet_transactions_wallet_transaction_amount_positive"
        ),
        sa.CheckConstraint(
            "direction IN ('credit','debit')",
            name="ck_wallet_transactions_wallet_transaction_direction",
        ),
        sa.CheckConstraint(
            "balance_after = balance_before + "
            "CASE WHEN direction = 'credit' THEN amount ELSE -amount END",
            name="ck_wallet_transactions_wallet_transaction_balance",
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"], ["user_wallets.id"], name="fk_wallet_transactions_wallet_id_user_wallets"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_wallet_transactions"),
        sa.UniqueConstraint("business_type", "business_no", name="uk_wallet_transactions_business"),
        sa.UniqueConstraint("transaction_no", name="uk_wallet_transactions_no"),
    )
    op.create_index(
        "idx_wallet_transactions_wallet_time",
        "wallet_transactions",
        ["wallet_id", "occurred_at", "id"],
    )


def downgrade() -> None:
    # The composite indexes are also valid supporting indexes for their foreign keys.
    # Let DROP TABLE remove constraints and indexes together; dropping them first fails
    # on MySQL with error 1553.
    op.drop_table("wallet_transactions")
    op.drop_table("wallet_recharges")
    op.drop_table("user_wallets")
