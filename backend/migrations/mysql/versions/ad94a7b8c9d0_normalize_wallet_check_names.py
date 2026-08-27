"""normalize wallet check constraint names

Revision ID: ad94a7b8c9d0
Revises: ac83f6a7b8c9
"""

from alembic import op

revision = "ad94a7b8c9d0"
down_revision = "ac83f6a7b8c9"
branch_labels = None
depends_on = None

_CHECKS = (
    (
        "user_wallets",
        "ck_user_wallets_ck_user_wallets_user_wallet_balance_nonnegative",
        "ck_user_wallets_user_wallet_balance_nonnegative",
        "balance_amount >= 0",
    ),
    (
        "user_wallets",
        "ck_user_wallets_ck_user_wallets_user_wallet_recharged_no_a91c",
        "ck_user_wallets_user_wallet_recharged_nonnegative",
        "total_recharged_amount >= 0",
    ),
    (
        "wallet_recharges",
        "ck_wallet_recharges_ck_wallet_recharges_wallet_recharge__c6f0",
        "ck_wallet_recharges_wallet_recharge_amount_positive",
        "amount > 0",
    ),
    (
        "wallet_recharges",
        "ck_wallet_recharges_ck_wallet_recharges_wallet_recharge_channel",
        "ck_wallet_recharges_wallet_recharge_channel",
        "channel IN ('wechat','alipay')",
    ),
    (
        "wallet_recharges",
        "ck_wallet_recharges_ck_wallet_recharges_wallet_recharge_status",
        "ck_wallet_recharges_wallet_recharge_status",
        "recharge_status = 'succeeded'",
    ),
    (
        "wallet_transactions",
        "ck_wallet_transactions_ck_wallet_transactions_wallet_tra_856a",
        "ck_wallet_transactions_wallet_transaction_amount_positive",
        "amount > 0",
    ),
    (
        "wallet_transactions",
        "ck_wallet_transactions_ck_wallet_transactions_wallet_tra_8f1f",
        "ck_wallet_transactions_wallet_transaction_balance",
        "balance_after = balance_before + "
        "CASE WHEN direction = 'credit' THEN amount ELSE -amount END",
    ),
    (
        "wallet_transactions",
        "ck_wallet_transactions_ck_wallet_transactions_wallet_tra_d123",
        "ck_wallet_transactions_wallet_transaction_direction",
        "direction IN ('credit','debit')",
    ),
)


def upgrade() -> None:
    for table, old_name, new_name, condition in _CHECKS:
        op.execute(f"ALTER TABLE `{table}` DROP CHECK `{old_name}`")
        op.execute(f"ALTER TABLE `{table}` ADD CONSTRAINT `{new_name}` CHECK ({condition})")


def downgrade() -> None:
    for table, old_name, new_name, condition in reversed(_CHECKS):
        op.execute(f"ALTER TABLE `{table}` DROP CHECK `{new_name}`")
        op.execute(f"ALTER TABLE `{table}` ADD CONSTRAINT `{old_name}` CHECK ({condition})")
