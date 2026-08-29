from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.modules.files.models import FileObject, FileUploadSession
from app.modules.finance.models import AccountDeletionTask
from app.modules.finance.repository import FinanceRepository
from app.modules.identity.models import AuthSession, User
from app.modules.stores.models import Store
from app.modules.system.models import OutboxEvent

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_MAX_PURGE_ROWS = 100_000


class AccountDeletionService:
    """Requests a durable deletion saga for an eligible non-trading account.

    Commerce records are deliberately a hard boundary: deleting an order would also alter
    another party's legally relevant history and merchant revenue. Accounts with any trade
    must therefore remain in the normal retention/anonymisation process instead.
    """

    def __init__(self, mysql: AsyncSession) -> None:
        self.mysql = mysql
        self.repository = FinanceRepository(mysql)

    async def delete_consumer(self, user: User) -> AccountDeletionTask:
        if await self.repository.has_consumer_trade(user.id):
            raise _blocked("账号存在历史订单，不能直接物理删除，请先联系平台客服处理留存义务。")
        if await self.mysql.scalar(select(Store.id).where(Store.owner_user_id == user.id)):
            raise _blocked("该账号绑定了店铺，请从商家中心注销店铺账号。")
        return await self._request(user, subject_type="consumer")

    async def delete_merchant(self, user: User) -> AccountDeletionTask:
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
        return await self._request(user, subject_type="merchant", stores=stores)

    async def _request(
        self,
        user: User,
        *,
        subject_type: str,
        stores: list[Store] | None = None,
    ) -> AccountDeletionTask:
        existing = await self.mysql.scalar(
            select(AccountDeletionTask).where(AccountDeletionTask.user_no == user.user_no)
        )
        if existing is not None:
            return existing
        selected: dict[str, set[int]] = defaultdict(set)
        selected["users"].add(user.id)
        owned_stores = stores or []
        for store in owned_stores:
            selected["stores"].add(store.id)

        await self._expand_mysql_fk_graph(selected)
        await self._add_unconstrained_mysql_rows(selected, user, owned_stores)
        await self._validate_global_grantor_replacement(user.id)
        inventory = await self._inventory(selected)
        now = utc_now()
        task_no = new_prefixed_ulid("adt_")
        trace_id = request_id_context.get() or task_no
        task = AccountDeletionTask(
            task_no=task_no,
            subject_type=subject_type,
            user_no=user.user_no,
            store_nos=[store.store_no for store in owned_stores],
            task_status="requested",
            current_phase="access_revoked",
            inventory=inventory,
            attempt_count=0,
            max_attempts=12,
            available_at=now,
            trace_id=trace_id,
        )
        self.mysql.add(task)
        user.user_status = "deletion_pending"
        user.status_reason_code = "account_deletion_requested"
        user.permission_version += 1
        await self.mysql.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now, revoke_reason="account_deletion_requested")
        )
        self.mysql.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="account.deletion.requested.v1",
                aggregate_type="account_deletion_task",
                aggregate_no=task_no,
                aggregate_version=1,
                payload={
                    "task_id": task_no,
                    "subject_type": subject_type,
                    "user_id": user.user_no,
                    "store_ids": [store.store_no for store in owned_stores],
                },
                event_status="pending",
                available_at=now,
                trace_id=trace_id,
            )
        )
        await self.mysql.commit()
        await self.mysql.refresh(task)
        return task

    async def _inventory(self, selected: dict[str, set[int]]) -> dict[str, object]:
        file_ids = sorted(selected.get("file_objects", set()))
        upload_ids = sorted(selected.get("file_upload_sessions", set()))
        file_objects: list[dict[str, str]] = []
        upload_objects: list[dict[str, str]] = []
        if file_ids:
            rows = (
                await self.mysql.execute(
                    select(FileObject.bucket, FileObject.object_key).where(
                        FileObject.id.in_(file_ids)
                    )
                )
            ).all()
            file_objects = [{"bucket": row.bucket, "object_key": row.object_key} for row in rows]
        if upload_ids:
            rows = (
                await self.mysql.execute(
                    select(
                        FileUploadSession.reserved_bucket,
                        FileUploadSession.reserved_object_key,
                    ).where(FileUploadSession.id.in_(upload_ids))
                )
            ).all()
            upload_objects = [
                {"bucket": row.reserved_bucket, "object_key": row.reserved_object_key}
                for row in rows
            ]
        objects = {
            (item["bucket"], item["object_key"]): item for item in [*file_objects, *upload_objects]
        }
        return {
            "mysql_ids": {
                table_name: sorted(values) for table_name, values in selected.items() if values
            },
            "storage_objects": list(objects.values()),
        }

    async def _validate_global_grantor_replacement(self, user_id: int) -> None:
        count = int(
            await self.mysql.scalar(
                text("SELECT COUNT(*) FROM role_permissions WHERE granted_by=:user_id"),
                {"user_id": user_id},
            )
            or 0
        )
        if not count:
            return
        replacement = await self.mysql.scalar(
            text(
                """SELECT ur.user_id FROM user_roles ur
                JOIN roles r ON r.id=ur.role_id JOIN users u ON u.id=ur.user_id
                WHERE r.role_code='platform_super_admin' AND r.scope_type='platform'
                AND r.role_status='active' AND ur.scope_type='platform' AND ur.scope_id=0
                AND ur.grant_status='active' AND u.user_status='active'
                AND ur.user_id<>:user_id ORDER BY ur.id LIMIT 1"""
            ),
            {"user_id": user_id},
        )
        if replacement is None:
            raise _blocked("该账号仍是全局权限配置的审计授予人，必须先由其他平台管理员接管。")

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
        # RolePermission is global security configuration. Its grantor is audit
        # metadata, not account-owned content, so deleting an account must never
        # cascade into the platform's role-to-permission bindings.
        relations = tuple(
            relation
            for relation in relations
            if not (str(relation[0]) == "role_permissions" and str(relation[1]) == "granted_by")
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
                        text(
                            # Identifiers are validated information_schema values; ids are ints.
                            f"SELECT id FROM `{child}` WHERE `{child_column}` IN ({ids})"  # nosec B608
                        )
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
                        # Store numbers are server-generated ULIDs loaded from Store rows.
                        f"AND scope_no IN ({store_nos})"  # nosec B608
                    )
                )
            ).scalars()
            selected["knowledge_documents"].update(int(value) for value in rows)
            await self._expand_mysql_fk_graph(selected)

    async def delete_mysql_inventory(
        self,
        selected: dict[str, set[int]],
        *,
        user_id: int,
        user_no: str,
        store_nos: list[str],
    ) -> None:
        try:
            role_permission_count = int(
                await self.mysql.scalar(
                    text("SELECT COUNT(*) FROM role_permissions WHERE granted_by=:user_id"),
                    {"user_id": user_id},
                )
                or 0
            )
            if role_permission_count:
                replacement_grantor = await self.mysql.scalar(
                    text(
                        """SELECT ur.user_id FROM user_roles ur
                        JOIN roles r ON r.id=ur.role_id
                        JOIN users u ON u.id=ur.user_id
                        WHERE r.role_code='platform_super_admin'
                        AND r.scope_type='platform' AND r.role_status='active'
                        AND ur.scope_type='platform' AND ur.scope_id=0
                        AND ur.grant_status='active' AND u.user_status='active'
                        AND ur.user_id<>:user_id ORDER BY ur.id LIMIT 1"""
                    ),
                    {"user_id": user_id},
                )
                if replacement_grantor is None:
                    raise _blocked(
                        "该账号仍是全局权限配置的审计授予人，必须先由其他平台管理员接管。"
                    )
                await self.mysql.execute(
                    text(
                        "UPDATE role_permissions SET granted_by=:replacement "
                        "WHERE granted_by=:user_id"
                    ),
                    {"replacement": int(replacement_grantor), "user_id": user_id},
                )
            await self.mysql.execute(text("SET SESSION FOREIGN_KEY_CHECKS=0"))
            for table_name in sorted(selected, key=lambda name: name == "users"):
                values = selected[table_name]
                if not values:
                    continue
                ids = ",".join(str(value) for value in sorted(values))
                await self.mysql.execute(
                    text(
                        # table_name is validated by selected_ids; ids are parsed integers.
                        f"DELETE FROM `{table_name}` WHERE id IN ({ids})"  # nosec B608
                    )
                )
            await self.mysql.execute(
                text("DELETE FROM idempotency_records WHERE scope_key LIKE :needle"),
                {"needle": f"%{user_no}%"},
            )
            aggregate_nos = [user_no, *store_nos]
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

    async def purge_postgres(
        self, postgres: AsyncSession, user_no: str, store_nos: list[str]
    ) -> None:
        run_nos = (
            (
                await postgres.execute(
                    text("SELECT run_no FROM agent_runtime.run_state_refs WHERE user_no=:user_no"),
                    {"user_no": user_no},
                )
            )
            .scalars()
            .all()
        )
        if run_nos:
            await postgres.execute(
                text("DELETE FROM agent_runtime.checkpoint_writes WHERE run_no=ANY(:run_nos)"),
                {"run_nos": list(run_nos)},
            )
            await postgres.execute(
                text("DELETE FROM agent_runtime.checkpoints WHERE run_no=ANY(:run_nos)"),
                {"run_nos": list(run_nos)},
            )
        await postgres.execute(
            text("DELETE FROM agent_runtime.run_state_refs WHERE user_no=:user_no"),
            {"user_no": user_no},
        )
        await postgres.execute(
            text(
                "DELETE FROM memory.events WHERE user_no=:user_no OR memory_id IN "
                "(SELECT id FROM memory.items WHERE user_no=:user_no)"
            ),
            {"user_no": user_no},
        )
        await postgres.execute(
            text(
                "DELETE FROM memory.item_embeddings WHERE memory_id IN "
                "(SELECT id FROM memory.items WHERE user_no=:user_no)"
            ),
            {"user_no": user_no},
        )
        await postgres.execute(
            text("DELETE FROM memory.items WHERE user_no=:user_no"), {"user_no": user_no}
        )
        await postgres.execute(
            text("DELETE FROM memory.summaries WHERE user_no=:user_no"),
            {"user_no": user_no},
        )
        if store_nos:
            await postgres.execute(
                text("DELETE FROM knowledge.retrieval_logs WHERE scope_no=ANY(:store_nos)"),
                {"store_nos": store_nos},
            )
        await postgres.commit()


def selected_ids(inventory: dict[str, object]) -> dict[str, set[int]]:
    raw = inventory.get("mysql_ids", {})
    if not isinstance(raw, dict):
        raise ValueError("account deletion inventory has no mysql_ids")
    result: dict[str, set[int]] = {}
    for table_name, values in raw.items():
        if not isinstance(table_name, str) or not _IDENTIFIER.fullmatch(table_name):
            raise ValueError("account deletion inventory has an unsafe table name")
        if not isinstance(values, list) or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in values
        ):
            raise ValueError("account deletion inventory has invalid row identifiers")
        result[table_name] = set(values)
    return result


def storage_objects(inventory: dict[str, object]) -> list[tuple[str, str]]:
    raw = inventory.get("storage_objects", [])
    if not isinstance(raw, list):
        raise ValueError("account deletion inventory has invalid storage objects")
    result: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("account deletion inventory has invalid storage object")
        bucket, object_key = item.get("bucket"), item.get("object_key")
        if not isinstance(bucket, str) or not isinstance(object_key, str):
            raise ValueError("account deletion inventory has invalid storage location")
        result.append((bucket, object_key))
    return result


def _blocked(detail: str) -> ApplicationError:
    return ApplicationError(
        status=409,
        code="ACCOUNT_DELETION_BLOCKED",
        title="Account deletion blocked",
        detail=detail,
    )
