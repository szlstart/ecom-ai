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
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import AppendOnlyMySQLModel, MutableMySQLModel, MySQLBase


class UserWallet(MutableMySQLModel, MySQLBase):
    __tablename__ = "user_wallets"
    __table_args__ = (
        UniqueConstraint("wallet_no", name="uk_user_wallets_no"),
        UniqueConstraint("user_id", "currency", name="uk_user_wallets_user_currency"),
        CheckConstraint("balance_amount >= 0", name="user_wallet_balance_nonnegative"),
        CheckConstraint("total_recharged_amount >= 0", name="user_wallet_recharged_nonnegative"),
    )

    wallet_no: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    balance_amount: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    total_recharged_amount: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    wallet_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class WalletRecharge(MutableMySQLModel, MySQLBase):
    __tablename__ = "wallet_recharges"
    __table_args__ = (
        UniqueConstraint("recharge_no", name="uk_wallet_recharges_no"),
        UniqueConstraint("idempotency_key_hash", name="uk_wallet_recharges_idempotency"),
        CheckConstraint("amount > 0", name="wallet_recharge_amount_positive"),
        CheckConstraint("channel IN ('wechat','alipay')", name="wallet_recharge_channel"),
        CheckConstraint("recharge_status = 'succeeded'", name="wallet_recharge_status"),
        Index("idx_wallet_recharges_user_time", "user_id", "created_at", "id"),
    )

    recharge_no: Mapped[str] = mapped_column(String(40), nullable=False)
    wallet_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("user_wallets.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    recharge_status: Mapped[str] = mapped_column(String(16), nullable=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    provider_reference: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class WalletTransaction(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "wallet_transactions"
    __table_args__ = (
        UniqueConstraint("transaction_no", name="uk_wallet_transactions_no"),
        UniqueConstraint("business_type", "business_no", name="uk_wallet_transactions_business"),
        CheckConstraint("amount > 0", name="wallet_transaction_amount_positive"),
        CheckConstraint("direction IN ('credit','debit')", name="wallet_transaction_direction"),
        CheckConstraint(
            "balance_after = balance_before + "
            "CASE WHEN direction = 'credit' THEN amount ELSE -amount END",
            name="wallet_transaction_balance",
        ),
        Index("idx_wallet_transactions_wallet_time", "wallet_id", "occurred_at", "id"),
    )

    transaction_no: Mapped[str] = mapped_column(String(40), nullable=False)
    wallet_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("user_wallets.id"), nullable=False
    )
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    balance_before: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    balance_after: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    business_type: Mapped[str] = mapped_column(String(32), nullable=False)
    business_no: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class AccountDeletionTask(MutableMySQLModel, MySQLBase):
    """Durable cross-system account deletion saga.

    Deliberately has no foreign key to ``users`` or ``stores``: the task is the
    audit/recovery record that must remain after the subject rows are removed.
    """

    __tablename__ = "account_deletion_tasks"
    __table_args__ = (
        UniqueConstraint("task_no", name="uk_account_deletion_tasks_no"),
        UniqueConstraint("user_no", name="uk_account_deletion_tasks_user"),
        Index(
            "idx_account_deletion_tasks_delivery",
            "task_status",
            "available_at",
            "id",
        ),
    )

    task_no: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    user_no: Mapped[str] = mapped_column(String(40), nullable=False)
    store_nos: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    task_status: Mapped[str] = mapped_column(String(24), nullable=False, default="requested")
    current_phase: Mapped[str] = mapped_column(String(32), nullable=False)
    inventory: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(nullable=False, default=12, server_default="12")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
