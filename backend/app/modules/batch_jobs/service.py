from __future__ import annotations

import csv
import io
import json
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.pagination import CursorCodec
from app.core.security import canonical_request_hash, utc_now
from app.modules.batch_jobs.parser import HEADERS, MAX_ROWS, SCHEMA_VERSION
from app.modules.batch_jobs.repository import BatchJobRepository
from app.modules.batch_jobs.schemas import (
    BatchJobCancellationRequest,
    BatchJobConfirmationRequest,
    BatchJobItemList,
    BatchJobItemView,
    BatchJobList,
    BatchJobView,
    ProductImportJobCreateRequest,
    ProductImportTemplateColumn,
    ProductImportTemplateView,
)
from app.modules.files.models import FileObject
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.stores.models import Store
from app.modules.system.models import AdminBatchJob, OutboxEvent

JOB_TERMINAL_STATUSES = {"succeeded", "partial", "failed", "cancelled", "expired"}


class BatchJobService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repository = BatchJobRepository(session)
        self.idempotency = IdempotencyService(session)
        self.cursor = CursorCodec(settings.security_hmac_secret.get_secret_value())

    def product_import_template(self) -> ProductImportTemplateView:
        descriptions = {
            "product_key": (True, "文件内商品分组键，同一商品的多个 SKU 使用相同值", "P001"),
            "product_name": (True, "商品名称，同一 product_key 必须一致", "轻量通勤双肩包"),
            "subtitle": (False, "商品副标题", "防泼水，可容纳 14 英寸电脑"),
            "category_id": (True, "平台公开类目 ID (cat_...)", "cat_01..."),
            "brand_id": (False, "平台公开品牌 ID (brd_...)", "brd_01..."),
            "merchant_sku_code": (True, "店铺内唯一 SKU 编码", "BAG-BLACK"),
            "sku_name": (True, "SKU 展示名称", "黑色标准版"),
            "spec_values_json": (
                True,
                "规格 JSON 数组，name/value 均必填",
                '[{"name":"颜色","value":"黑色"}]',
            ),
            "sale_price_amount": (True, "销售价，单位为分", "19900"),
            "market_price_amount": (True, "市场价，单位为分且不得低于销售价", "22900"),
            "currency": (True, "首版固定 CNY", "CNY"),
            "weight_grams": (False, "重量，单位克", "650"),
            "barcode": (False, "条码", "6900000000000"),
            "initial_stock": (True, "初始现货库存，非负整数", "100"),
        }
        return ProductImportTemplateView(
            schema_version=SCHEMA_VERSION,
            supported_file_types=["csv", "xlsx"],
            maximum_rows=MAX_ROWS,
            columns=[
                ProductImportTemplateColumn(
                    name=name,
                    required=descriptions[name][0],
                    description=descriptions[name][1],
                    example=descriptions[name][2],
                )
                for name in HEADERS
            ],
        )

    def product_import_template_csv(self) -> bytes:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(HEADERS)
        writer.writerow([column.example for column in self.product_import_template().columns])
        return ("\ufeff" + output.getvalue()).encode("utf-8")

    async def create_product_import(
        self,
        access: AdminAccess,
        payload: ProductImportJobCreateRequest,
        key: str,
    ) -> BatchJobView:
        store = await self.repository.store_by_no(payload.store_id)
        if store is None:
            raise _not_found()
        access.require_scope("store", store.id)
        if store.store_status != "active":
            raise _conflict("STORE_NOT_ACTIVE", "只有启用中的店铺可以导入商品。")
        source = await self.repository.file_by_no(payload.input_file_id)
        if (
            source is None
            or source.purpose != "admin_import"
            or source.owner_type != "store"
            or source.owner_no != store.store_no
        ):
            raise _not_found()
        if source.file_status != "active" or source.scan_status != "safe":
            raise _conflict("IMPORT_FILE_NOT_READY", "导入文件尚未通过安全扫描。")
        if source.expires_at is not None and source.expires_at <= utc_now():
            raise ApplicationError(
                status=410,
                code="IMPORT_FILE_EXPIRED",
                title="Import file expired",
                detail="导入文件已经过期，请重新上传。",
            )
        claim = await self.idempotency.begin(
            scope_key=f"admin:product-import-create:{store.store_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="admin_batch_job",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.job_by_no(claim.record.resource_no)
            if existing is not None:
                return await self._view(existing)
        request_config: dict[str, object] = {
            "schema_version": payload.schema_version,
            "store_id": store.store_no,
            "input_sha256": source.sha256.hex(),
        }
        job = AdminBatchJob(
            job_no=new_prefixed_ulid("job_"),
            job_type="product_import",
            requested_by=access.context.user.id,
            scope_type="store",
            scope_id=store.id,
            permission_code="products:create",
            input_file_id=source.id,
            request_config=request_config,
            request_hash=canonical_request_hash(request_config),
            job_status="created",
            expires_at=utc_now() + timedelta(days=7),
            trace_id=request_id_context.get() or new_prefixed_ulid("req_"),
        )
        source.reference_count += 1
        source.version += 1
        self.session.add(job)
        await self.session.flush()
        _event(self.session, job, "admin_batch_job.validation_requested.v1")
        record_admin_operation(
            self.session,
            access,
            action="create_product_import_job",
            target_type="admin_batch_job",
            target_no=job.job_no,
            after={"store_id": store.store_no, "schema_version": payload.schema_version},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(claim, response_status=201, resource_no=job.job_no)
        await self.session.commit()
        return await self._fresh_view(job.job_no)

    async def list_jobs(
        self,
        access: AdminAccess,
        *,
        job_type: str | None,
        job_status: str | None,
        cursor: str | None,
        limit: int,
    ) -> BatchJobList:
        filter_key = json.dumps(
            {"kind": "admin-batch-jobs", "type": job_type, "status": job_status},
            sort_keys=True,
            separators=(",", ":"),
        )
        position = self.cursor.decode(cursor, filter_key=filter_key)
        if position is not None and (position.direction != "next" or len(position.values) != 1):
            raise _bad_cursor()
        rows = await self.repository.jobs(
            access.scopes,
            job_type=job_type,
            job_status=job_status,
            cursor_no=position.values[0] if position else None,
            limit=limit,
        )
        visible = rows[:limit]
        knowledge_states: dict[str, tuple[AdminBatchJob, AdminBatchJob | None]] = {}
        if job_type == "knowledge_index":
            history = await self.repository.knowledge_job_history(access.scopes)
            failed_by_document: dict[str, list[AdminBatchJob]] = {}
            for history_job in history:
                document_no = history_job.request_config.get("document_no")
                if not isinstance(document_no, str) or not document_no:
                    continue
                if document_no not in knowledge_states:
                    knowledge_states[document_no] = (history_job, None)
                if history_job.job_status == "failed":
                    failed_by_document.setdefault(document_no, []).append(history_job)
            for document_no, (latest, _recovery) in list(knowledge_states.items()):
                recovered_failure = None
                if latest.job_status == "succeeded" and failed_by_document.get(document_no):
                    recovered_failure = latest
                knowledge_states[document_no] = (latest, recovered_failure)
        stores = await self.repository.stores_by_ids({item.scope_id for item in visible})
        file_ids = {
            file_id
            for item in visible
            for file_id in (item.input_file_id, item.result_file_id, item.error_file_id)
            if file_id is not None
        }
        files = await self.repository.files_by_ids(file_ids)
        return BatchJobList(
            items=[
                _job_view(
                    item,
                    stores.get(item.scope_id),
                    files.get(item.input_file_id) if item.input_file_id is not None else None,
                    files.get(item.result_file_id) if item.result_file_id is not None else None,
                    files.get(item.error_file_id) if item.error_file_id is not None else None,
                    knowledge_states=knowledge_states,
                )
                for item in visible
            ],
            next_cursor=(
                self.cursor.encode(filter_key=filter_key, values=(visible[-1].job_no,))
                if len(rows) > limit and visible
                else None
            ),
        )

    async def get_job(self, access: AdminAccess, job_no: str) -> BatchJobView:
        job = await self._visible_job(access, job_no)
        return await self._view(job)

    async def list_items(
        self,
        access: AdminAccess,
        job_no: str,
        *,
        item_status: str | None,
        cursor: str | None,
        limit: int,
    ) -> BatchJobItemList:
        job = await self._visible_job(access, job_no)
        filter_key = json.dumps(
            {"kind": "admin-batch-job-items", "job": job.job_no, "status": item_status},
            sort_keys=True,
            separators=(",", ":"),
        )
        position = self.cursor.decode(cursor, filter_key=filter_key)
        cursor_id = None
        if position is not None:
            if position.direction != "next" or len(position.values) != 1:
                raise _bad_cursor()
            try:
                cursor_id = int(position.values[0])
            except ValueError as exc:
                raise _bad_cursor() from exc
        rows = await self.repository.items(
            job.id, item_status=item_status, cursor_id=cursor_id, limit=limit
        )
        visible = rows[:limit]
        return BatchJobItemList(
            items=[
                BatchJobItemView(
                    item_key=item.item_key,
                    item_status=item.item_status,
                    resource_type=item.resource_type,
                    resource_id=item.resource_no,
                    error_code=item.error_code,
                    error_message=item.error_message,
                )
                for item in visible
            ],
            next_cursor=(
                self.cursor.encode(filter_key=filter_key, values=(str(visible[-1].id),))
                if len(rows) > limit and visible
                else None
            ),
        )

    async def confirm(
        self,
        access: AdminAccess,
        job_no: str,
        payload: BatchJobConfirmationRequest,
        expected_version: int,
        key: str,
    ) -> BatchJobView:
        job = await self.repository.job_by_no(job_no, for_update=True)
        if job is None or job.job_type != "product_import":
            raise _not_found()
        access.require_scope(job.scope_type, job.scope_id)
        claim = await self.idempotency.begin(
            scope_key=f"admin:batch-job-confirm:{job.job_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="admin_batch_job",
        )
        if claim.replayed:
            return await self._view(job)
        _version(job.version, expected_version)
        if job.job_status != "awaiting_confirmation":
            raise _conflict("BATCH_JOB_NOT_CONFIRMABLE", "当前任务状态不能确认执行。")
        if job.expires_at is not None and job.expires_at <= utc_now():
            job.job_status = "expired"
            job.finished_at = utc_now()
            job.version += 1
            await self.session.commit()
            raise ApplicationError(
                status=410,
                code="BATCH_JOB_EXPIRED",
                title="Batch job expired",
                detail="预检结果已过期，请重新创建任务。",
            )
        if payload.preview_hash != job.request_hash.hex():
            raise _conflict("BATCH_JOB_PREVIEW_CHANGED", "预检摘要已经变化，请刷新后重新确认。")
        if job.success_count <= 0:
            raise _conflict("BATCH_JOB_NO_VALID_ITEMS", "任务没有可以执行的有效数据。")
        job.job_status = "queued"
        job.version += 1
        _event(self.session, job, "admin_batch_job.execution_requested.v1")
        record_admin_operation(
            self.session,
            access,
            action="confirm_product_import_job",
            target_type="admin_batch_job",
            target_no=job.job_no,
            after={"preview_hash": payload.preview_hash, "valid_count": job.success_count},
            scope_type=job.scope_type,
            scope_id=job.scope_id,
        )
        self.idempotency.complete(claim, response_status=200, resource_no=job.job_no)
        await self.session.commit()
        return await self._fresh_view(job.job_no)

    async def cancel(
        self,
        access: AdminAccess,
        job_no: str,
        payload: BatchJobCancellationRequest,
        expected_version: int,
        key: str,
    ) -> BatchJobView:
        job = await self._visible_job(access, job_no, for_update=True)
        if not await self.repository.actor_has_job_permission(access.context.user.id, job):
            raise ApplicationError(
                status=403,
                code="BATCH_JOB_PERMISSION_REVOKED",
                title="Batch job permission denied",
                detail="当前管理员已不具备任务原始操作权限。",
            )
        claim = await self.idempotency.begin(
            scope_key=f"admin:batch-job-cancel:{job.job_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="admin_batch_job",
        )
        if claim.replayed:
            return await self._view(job)
        _version(job.version, expected_version)
        if job.job_status in JOB_TERMINAL_STATUSES:
            raise _conflict("BATCH_JOB_NOT_CANCELLABLE", "任务已经结束，不能取消。")
        job.cancel_requested_at = utc_now()
        job.cancel_requested_by = access.context.user.id
        job.cancel_reason = payload.reason
        if job.job_status != "running":
            job.job_status = "cancelled"
            job.finished_at = utc_now()
        job.version += 1
        _event(self.session, job, "admin_batch_job.cancellation_requested.v1")
        record_admin_operation(
            self.session,
            access,
            action="cancel_batch_job",
            target_type="admin_batch_job",
            target_no=job.job_no,
            reason=payload.reason,
            scope_type=job.scope_type,
            scope_id=job.scope_id,
        )
        self.idempotency.complete(claim, response_status=200, resource_no=job.job_no)
        await self.session.commit()
        return await self._fresh_view(job.job_no)

    async def _visible_job(
        self, access: AdminAccess, job_no: str, *, for_update: bool = False
    ) -> AdminBatchJob:
        job = await self.repository.job_by_no(job_no, for_update=for_update)
        if job is None:
            raise _not_found()
        if access.permission.permission_code != "jobs:read" and (
            job.permission_code != access.permission.permission_code
        ):
            raise _not_found()
        access.require_scope(job.scope_type, job.scope_id)
        return job

    async def _view(self, job: AdminBatchJob) -> BatchJobView:
        store = await self.repository.store_by_id(job.scope_id)
        input_file = await self.repository.file_by_id(job.input_file_id)
        result_file = await self.repository.file_by_id(job.result_file_id)
        error_file = await self.repository.file_by_id(job.error_file_id)
        return _job_view(job, store, input_file, result_file, error_file)

    async def _fresh_view(self, job_no: str) -> BatchJobView:
        job = await self.repository.job_by_no(job_no)
        if job is None:
            raise _not_found()
        return await self._view(job)


def _job_view(
    job: AdminBatchJob,
    store: Store | None,
    input_file: FileObject | None,
    result_file: FileObject | None,
    error_file: FileObject | None,
    *,
    knowledge_states: dict[str, tuple[AdminBatchJob, AdminBatchJob | None]] | None = None,
) -> BatchJobView:
    config = job.request_config
    actions: list[str] = []
    if job.job_status == "awaiting_confirmation" and job.success_count > 0:
        actions.append("confirm")
    if job.job_status not in JOB_TERMINAL_STATUSES:
        actions.append("cancel")
    document_no = config.get("document_no")
    document_no = document_no if isinstance(document_no, str) and document_no else None
    content_version = config.get("content_version")
    content_version = (
        content_version if isinstance(content_version, str) and content_version else None
    )
    latest_for_resource: bool | None = None
    effective_status: str | None = None
    recovered_by_job_id: str | None = None
    if document_no is not None and knowledge_states is not None:
        state = knowledge_states.get(document_no)
        if state is not None:
            latest, recovery = state
            latest_for_resource = latest.id == job.id
            effective_status = latest.job_status
            if job.job_status == "failed" and recovery is not None and recovery.id > job.id:
                recovered_by_job_id = recovery.job_no
    return BatchJobView(
        job_id=job.job_no,
        job_type=job.job_type,
        store_id=store.store_no if store else "",
        schema_version=str(config.get("schema_version", "")),
        status=job.job_status,
        total_count=job.total_count,
        success_count=job.success_count,
        failure_count=job.failure_count,
        preview_hash=job.request_hash.hex() if job.job_status != "created" else None,
        input_file_id=input_file.file_no if input_file else None,
        result_file_id=result_file.file_no if result_file else None,
        error_file_id=error_file.file_no if error_file else None,
        error_code=job.error_code,
        error_summary=job.error_summary,
        requested_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        expires_at=job.expires_at,
        available_actions=actions,
        version=job.version,
        resource_id=document_no,
        content_version=content_version,
        latest_for_resource=latest_for_resource,
        effective_status=effective_status,
        recovered_by_job_id=recovered_by_job_id,
    )


def _event(session: AsyncSession, job: AdminBatchJob, event_type: str) -> None:
    session.add(
        OutboxEvent(
            event_no=new_prefixed_ulid("evt_"),
            event_type=event_type,
            aggregate_type="admin_batch_job",
            aggregate_no=job.job_no,
            aggregate_version=job.version,
            payload={"job_id": job.job_no, "job_type": job.job_type},
            event_status="pending",
            available_at=utc_now(),
            trace_id=request_id_context.get() or job.trace_id,
        )
    )


def _version(current: int, expected: int) -> None:
    if current != expected:
        raise ApplicationError(
            status=412,
            code="RESOURCE_VERSION_CONFLICT",
            title="Version conflict",
            detail="任务状态已经变化，请刷新后重试。",
        )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404, code="RESOURCE_NOT_FOUND", title="Resource not found", detail="未找到该资源。"
    )


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Batch job conflict", detail=detail)


def _bad_cursor() -> ApplicationError:
    return ApplicationError(
        status=400,
        code="PAGINATION_CURSOR_INVALID",
        title="Invalid pagination cursor",
        detail="分页位置无效，请重新加载列表。",
    )
