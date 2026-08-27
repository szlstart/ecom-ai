from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.modules.finance.repository import FinanceRepository
from app.modules.identity.models import User
from app.modules.stores.models import Store

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_MAX_PURGE_ROWS = 100_000


class AccountDeletionService:
    """Physically removes an eligible account and all of its non-transactional data.

    Commerce records are deliberately a hard boundary: deleting an order would also alter
    another party's legally relevant history and merchant revenue. Accounts with any trade
    must therefore remain in the normal retention/anonymisation process instead.
    """

    def __init__(self, mysql: AsyncSession, postgres: AsyncSession) -> None:
        self.mysql = mysql
        self.postgres = postgres
        self.repository = FinanceRepository(mysql)

    async def delete_consumer(self, user: User) -> None:
        if await self.repository.has_consumer_trade(user.id):
            raise _blocked("账号存在历史订单，不能直接物理删除，请先联系平台客服处理留存义务。")
        if await self.mysql.scalar(select(Store.id).where(Store.owner_user_id == user.id)):
            raise _blocked("该账号绑定了店铺，请从商家中心注销店铺账号。")
        await self._purge(user)

    async def delete_merchant(self, user: User) -> None:
        stores = list(
            (await self.mysql.scalars(select(Store).where(Store.owner_user_id == user.id))).all()
        )
        if not stores:
            raise ApplicationError(
                status=404,
                code="MERCHANT_STORE_NOT_FOUND",
                title="Merchant store not found",
                detail="当前商家账号没有可注销的店铺。",
            )
        for store in stores:
            if await self.repository.has_store_trade(store.id):
                raise _blocked(
                    f"店铺“{store.store_name}”存在历史订单，不能直接物理删除，"
                    "请先联系平台客服处理留存义务。"
                )
        await self._purge(user, stores=stores)

    async def _purge(self, user: User, *, stores: list[Store] | None = None) -> None:
        selected: dict[str, set[int]] = defaultdict(set)
        selected["users"].add(user.id)
        owned_stores = stores or []
        for store in owned_stores:
            selected["stores"].add(store.id)

        await self._purge_postgres(user.user_no, [store.store_no for store in owned_stores])
        await self._expand_mysql_fk_graph(selected)
        await self._add_unconstrained_mysql_rows(selected, user, owned_stores)
        await self._delete_mysql_rows(selected, user, owned_stores)

    async def _expand_mysql_fk_graph(self, selected: dict[str, set[int]]) -> None:
        relations = (
            (
                await self.mysql.execute(
                    text(
                        """SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME,
                        REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE
                        WHERE TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_SCHEMA=DATABASE()
                        AND REFERENCED_TABLE_NAME IS NOT NULL"""
                    )
                )
            )
            .tuples()
            .all()
        )
        for names in relations:
            for name in names:
                if name is not None and not _IDENTIFIER.fullmatch(str(name)):
                    raise RuntimeError("unsafe database identifier returned by information_schema")

        changed = True
        while changed:
            changed = False
            for child, child_column, parent, parent_column in relations:
                parent_ids = selected.get(str(parent), set())
                if not parent_ids or parent_column != "id":
                    continue
                ids = ",".join(str(value) for value in sorted(parent_ids))
                rows = (
                    await self.mysql.execute(
                        text(f"SELECT id FROM `{child}` WHERE `{child_column}` IN ({ids})")
                    )
                ).scalars()
                bucket = selected[str(child)]
                before = len(bucket)
                bucket.update(int(value) for value in rows)
                changed = changed or len(bucket) != before
            if sum(len(values) for values in selected.values()) > _MAX_PURGE_ROWS:
                raise _blocked("该账号关联数据量过大，必须由平台管理员执行受控注销。")

    async def _add_unconstrained_mysql_rows(
        self, selected: dict[str, set[int]], user: User, stores: list[Store]
    ) -> None:
        if stores:
            store_nos = ",".join(f"'{store.store_no}'" for store in stores)
            rows = (
                await self.mysql.execute(
                    text(
                        "SELECT id FROM knowledge_documents WHERE scope_type='store' "
                        f"AND scope_no IN ({store_nos})"
                    )
                )
            ).scalars()
            selected["knowledge_documents"].update(int(value) for value in rows)
            await self._expand_mysql_fk_graph(selected)

    async def _delete_mysql_rows(
        self, selected: dict[str, set[int]], user: User, stores: list[Store]
    ) -> None:
        store_nos = [store.store_no for store in stores]
        try:
            await self.mysql.execute(text("SET SESSION FOREIGN_KEY_CHECKS=0"))
            for table_name in sorted(selected, key=lambda name: name == "users"):
                values = selected[table_name]
                if not values:
                    continue
                ids = ",".join(str(value) for value in sorted(values))
                await self.mysql.execute(text(f"DELETE FROM `{table_name}` WHERE id IN ({ids})"))
            await self.mysql.execute(
                text("DELETE FROM idempotency_records WHERE scope_key LIKE :needle"),
                {"needle": f"%{user.user_no}%"},
            )
            aggregate_nos = [user.user_no, *store_nos]
            for aggregate_no in aggregate_nos:
                await self.mysql.execute(
                    text(
                        "DELETE FROM outbox_events WHERE aggregate_no=:aggregate_no "
                        "OR JSON_SEARCH(payload, 'one', :aggregate_no) IS NOT NULL"
                    ),
                    {"aggregate_no": aggregate_no},
                )
            await self.mysql.commit()
        except Exception:
            await self.mysql.rollback()
            raise
        finally:
            await self.mysql.execute(text("SET SESSION FOREIGN_KEY_CHECKS=1"))

    async def _purge_postgres(self, user_no: str, store_nos: list[str]) -> None:
        run_nos = (
            (
                await self.postgres.execute(
                    text("SELECT run_no FROM agent_runtime.run_state_refs WHERE user_no=:user_no"),
                    {"user_no": user_no},
                )
            )
            .scalars()
            .all()
        )
        if run_nos:
            await self.postgres.execute(
                text("DELETE FROM agent_runtime.checkpoint_writes WHERE run_no=ANY(:run_nos)"),
                {"run_nos": list(run_nos)},
            )
            await self.postgres.execute(
                text("DELETE FROM agent_runtime.checkpoints WHERE run_no=ANY(:run_nos)"),
                {"run_nos": list(run_nos)},
            )
        await self.postgres.execute(
            text("DELETE FROM agent_runtime.run_state_refs WHERE user_no=:user_no"),
            {"user_no": user_no},
        )
        await self.postgres.execute(
            text(
                "DELETE FROM memory.events WHERE user_no=:user_no OR memory_id IN "
                "(SELECT id FROM memory.items WHERE user_no=:user_no)"
            ),
            {"user_no": user_no},
        )
        await self.postgres.execute(
            text(
                "DELETE FROM memory.item_embeddings WHERE memory_id IN "
                "(SELECT id FROM memory.items WHERE user_no=:user_no)"
            ),
            {"user_no": user_no},
        )
        await self.postgres.execute(
            text("DELETE FROM memory.items WHERE user_no=:user_no"), {"user_no": user_no}
        )
        await self.postgres.execute(
            text("DELETE FROM memory.summaries WHERE user_no=:user_no"),
            {"user_no": user_no},
        )
        if store_nos:
            await self.postgres.execute(
                text("DELETE FROM knowledge.retrieval_logs WHERE scope_no=ANY(:store_nos)"),
                {"store_nos": store_nos},
            )
        await self.postgres.commit()


def _blocked(detail: str) -> ApplicationError:
    return ApplicationError(
        status=409,
        code="ACCOUNT_DELETION_BLOCKED",
        title="Account deletion blocked",
        detail=detail,
    )
