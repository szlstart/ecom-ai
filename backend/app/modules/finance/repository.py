from __future__ import annotations

from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.finance.models import UserWallet, WalletRecharge, WalletTransaction
from app.modules.orders.models import Order, TradeOrder
from app.modules.stores.models import Store


class FinanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def wallet(self, user_id: int, *, for_update: bool = False) -> UserWallet | None:
        statement = select(UserWallet).where(
            UserWallet.user_id == user_id, UserWallet.currency == "CNY"
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(UserWallet | None, await self.session.scalar(statement))

    async def recharge_by_no(self, user_id: int, recharge_no: str) -> WalletRecharge | None:
        return cast(
            WalletRecharge | None,
            await self.session.scalar(
                select(WalletRecharge).where(
                    WalletRecharge.user_id == user_id,
                    WalletRecharge.recharge_no == recharge_no,
                )
            ),
        )

    async def transactions(self, user_id: int, limit: int) -> list[WalletTransaction]:
        return list(
            (
                await self.session.scalars(
                    select(WalletTransaction)
                    .join(UserWallet, UserWallet.id == WalletTransaction.wallet_id)
                    .where(UserWallet.user_id == user_id)
                    .order_by(WalletTransaction.occurred_at.desc(), WalletTransaction.id.desc())
                    .limit(limit)
                )
            ).all()
        )

    async def owned_store(self, user_id: int, store_no: str) -> Store | None:
        return cast(
            Store | None,
            await self.session.scalar(
                select(Store).where(Store.owner_user_id == user_id, Store.store_no == store_no)
            ),
        )

    async def revenue(self, store_id: int) -> tuple[int, int, int]:
        row = (
            await self.session.execute(
                select(
                    func.coalesce(func.sum(Order.paid_amount), 0),
                    func.coalesce(func.sum(Order.refunded_amount), 0),
                    func.count(Order.id),
                ).where(Order.store_id == store_id, Order.paid_amount > 0)
            )
        ).one()
        return int(row[0]), int(row[1]), int(row[2])

    async def has_consumer_trade(self, user_id: int) -> bool:
        return bool(
            await self.session.scalar(
                select(func.count(TradeOrder.id)).where(TradeOrder.user_id == user_id)
            )
        )

    async def has_store_trade(self, store_id: int) -> bool:
        return bool(
            await self.session.scalar(
                select(func.count(Order.id)).where(Order.store_id == store_id)
            )
        )
