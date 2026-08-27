from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import SecurityService, utc_now
from app.modules.catalog.schemas import Money
from app.modules.finance.models import UserWallet, WalletRecharge, WalletTransaction
from app.modules.finance.repository import FinanceRepository
from app.modules.finance.schemas import (
    MerchantRevenueView,
    WalletRechargeRequest,
    WalletRechargeResult,
    WalletRechargeView,
    WalletTransactionList,
    WalletTransactionView,
    WalletView,
)
from app.modules.identity.models import User


class FinanceService:
    def __init__(self, session: AsyncSession, security: SecurityService) -> None:
        self.session = session
        self.security = security
        self.repository = FinanceRepository(session)
        self.idempotency = IdempotencyService(session)

    async def wallet(self, user: User) -> WalletView:
        wallet = await self._wallet(user.id)
        await self.session.commit()
        return _wallet_view(wallet)

    async def recharge(
        self, user: User, payload: WalletRechargeRequest, idempotency_key: str
    ) -> WalletRechargeResult:
        claim = await self.idempotency.begin(
            scope_key=f"wallet:recharge:{user.user_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="wallet_recharge",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.recharge_by_no(user.id, claim.record.resource_no)
            if existing is None:
                raise _error("IDEMPOTENCY_RESULT_UNAVAILABLE", "原充值结果已不可用。")
            replay_wallet = await self._wallet(user.id)
            return WalletRechargeResult(
                recharge=_recharge_view(existing), wallet=_wallet_view(replay_wallet)
            )

        wallet = await self.repository.wallet(user.id, for_update=True)
        if wallet is None:
            wallet = await self._wallet(user.id)
            await self.session.flush()
        if wallet.wallet_status != "active":
            raise _error("WALLET_UNAVAILABLE", "余额账户当前不可用。")
        amount = int(payload.amount.minor_units)
        before = wallet.balance_amount
        now = utc_now()
        recharge = WalletRecharge(
            recharge_no=new_prefixed_ulid("rch_"),
            wallet_id=wallet.id,
            user_id=user.id,
            channel=payload.channel,
            amount=amount,
            currency="CNY",
            recharge_status="succeeded",
            is_simulated=True,
            provider_reference=new_prefixed_ulid("sim_"),
            idempotency_key_hash=self.security.keyed_hash(
                "wallet-recharge-idempotency", f"{user.id}:{idempotency_key}"
            ),
            completed_at=now,
        )
        self.session.add(recharge)
        await self.session.flush()
        wallet.balance_amount += amount
        wallet.total_recharged_amount += amount
        wallet.version += 1
        self.session.add(
            WalletTransaction(
                transaction_no=new_prefixed_ulid("wtx_"),
                wallet_id=wallet.id,
                transaction_type="simulated_recharge",
                direction="credit",
                amount=amount,
                balance_before=before,
                balance_after=wallet.balance_amount,
                currency="CNY",
                business_type="wallet_recharge",
                business_no=recharge.recharge_no,
                channel=payload.channel,
                description="模拟充值到账",
                occurred_at=now,
            )
        )
        self.idempotency.complete(claim, response_status=201, resource_no=recharge.recharge_no)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _error("RECHARGE_CONFLICT", "充值请求发生冲突，请刷新余额后重试。") from exc
        return WalletRechargeResult(recharge=_recharge_view(recharge), wallet=_wallet_view(wallet))

    async def transactions(self, user: User, limit: int) -> WalletTransactionList:
        rows = await self.repository.transactions(user.id, limit)
        return WalletTransactionList(
            items=[
                WalletTransactionView(
                    transaction_id=row.transaction_no,
                    transaction_type=row.transaction_type,
                    direction=row.direction,
                    amount=_money(row.amount, row.currency),
                    balance_after=_money(row.balance_after, row.currency),
                    channel=row.channel,
                    description=row.description,
                    occurred_at=row.occurred_at,
                )
                for row in rows
            ]
        )

    async def merchant_revenue(self, user: User, store_no: str) -> MerchantRevenueView:
        store = await self.repository.owned_store(user.id, store_no)
        if store is None:
            raise ApplicationError(
                status=404,
                code="STORE_NOT_FOUND",
                title="Store not found",
                detail="未找到该店铺。",
            )
        gross, refunded, count = await self.repository.revenue(store.id)
        return MerchantRevenueView(
            store_id=store.store_no,
            gross_sales=_money(gross),
            refunded_amount=_money(refunded),
            net_revenue=_money(gross - refunded),
            paid_order_count=count,
        )

    async def _wallet(self, user_id: int) -> UserWallet:
        wallet = await self.repository.wallet(user_id)
        if wallet is not None:
            return wallet
        wallet = UserWallet(
            wallet_no=new_prefixed_ulid("wal_"),
            user_id=user_id,
            balance_amount=0,
            total_recharged_amount=0,
            currency="CNY",
            wallet_status="active",
        )
        self.session.add(wallet)
        await self.session.flush()
        return wallet


def _money(amount: int, currency: str = "CNY") -> Money:
    return Money(minor_units=str(amount), currency=currency)


def _wallet_view(wallet: UserWallet) -> WalletView:
    return WalletView(
        wallet_id=wallet.wallet_no,
        balance=_money(wallet.balance_amount, wallet.currency),
        total_recharged=_money(wallet.total_recharged_amount, wallet.currency),
        wallet_status=wallet.wallet_status,
        version=wallet.version,
    )


def _recharge_view(recharge: WalletRecharge) -> WalletRechargeView:
    return WalletRechargeView(
        recharge_id=recharge.recharge_no,
        channel=recharge.channel,
        amount=_money(recharge.amount, recharge.currency),
        recharge_status="succeeded",
        is_simulated=recharge.is_simulated,
        completed_at=recharge.completed_at,
    )


def _error(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Wallet operation failed", detail=detail)
