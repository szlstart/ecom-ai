from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import cast

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import canonical_request_hash, utc_now
from app.integrations.object_storage import ObjectStorage
from app.modules.batch_jobs.parser import (
    ProductImportParseResult,
    RowProblem,
    parse_product_import,
)
from app.modules.batch_jobs.repository import BatchJobRepository
from app.modules.batch_jobs.schemas import ParsedProductImportRow
from app.modules.catalog.models import Brand, Category, Product, ProductSku, ProductStatusLog
from app.modules.files.models import FileObject
from app.modules.inventory.models import Inventory, InventoryLog
from app.modules.stores.models import Store
from app.modules.system.models import AdminBatchJob, AdminBatchJobItem, OutboxEvent

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ValidatedImport:
    rows: tuple[ParsedProductImportRow, ...]
    problems: tuple[RowProblem, ...]
    digest: str


class BatchJobProcessor:
    def __init__(self, session: AsyncSession, storage: ObjectStorage) -> None:
        self.session = session
        self.storage = storage
        self.repository = BatchJobRepository(session)

    async def process_one(self) -> bool:
        job = await self.repository.next_job(("created", "queued"))
        if job is None:
            await self.session.rollback()
            return False
        job_no = job.job_no
        action = "validate" if job.job_status == "created" else "execute"
        job.job_status = "validating" if action == "validate" else "running"
        if action == "execute":
            job.started_at = utc_now()
        job.version += 1
        await self.session.commit()
        try:
            if action == "validate":
                await self._validate(job_no)
            else:
                await self._execute(job_no)
        except ApplicationError as exc:
            await self.session.rollback()
            await self._fail(job_no, exc.code, exc.detail)
        except Exception:
            logger.exception("batch_job_processing_failed", job_id=job_no, action=action)
            await self.session.rollback()
            await self._fail(
                job_no, "BATCH_JOB_INTERNAL_ERROR", "批处理执行失败，请联系管理员并提供任务编号。"
            )
        return True

    async def _validate(self, job_no: str) -> None:
        job, source, store = await self._job_source_store(job_no)
        validated = await self._read_and_validate(job, source, store)
        error_file = await self._write_error_report(job, store, validated.problems)
        for row in validated.rows:
            self.session.add(
                AdminBatchJobItem(
                    job_id=job.id,
                    item_key=str(row.row_number),
                    item_status="valid",
                    input_hash=_row_hash(row),
                )
            )
        for problem in validated.problems:
            self.session.add(
                AdminBatchJobItem(
                    job_id=job.id,
                    item_key=str(problem.row_number),
                    item_status="invalid",
                    error_code=problem.code,
                    error_message=problem.message,
                    input_hash=problem.input_hash,
                )
            )
        config = dict(job.request_config)
        config["validation_digest"] = validated.digest
        config["validated_at"] = utc_now().isoformat()
        config["valid_product_count"] = len({row.product_key for row in validated.rows})
        job.request_config = config
        job.request_hash = canonical_request_hash(config)
        job.total_count = len(validated.rows) + len(validated.problems)
        job.success_count = len(validated.rows)
        job.failure_count = len(validated.problems)
        job.error_file_id = error_file.id if error_file else None
        if validated.rows:
            job.job_status = "awaiting_confirmation"
            job.error_code = None
            job.error_summary = None
        else:
            job.job_status = "failed"
            job.error_code = "BATCH_JOB_NO_VALID_ITEMS"
            job.error_summary = "文件中没有可执行的有效商品行。"
            job.finished_at = utc_now()
        job.version += 1
        _event(self.session, job, "admin_batch_job.validation_completed.v1")
        await self.session.commit()

    async def _execute(self, job_no: str) -> None:
        job, source, store = await self._job_source_store(job_no)
        if not await self.repository.requester_is_authorized(job):
            raise _forbidden("BATCH_JOB_PERMISSION_REVOKED", "任务发起人的权限或数据范围已经失效。")
        validated = await self._read_and_validate(job, source, store)
        config = job.request_config
        if validated.digest != config.get("validation_digest"):
            raise _conflict(
                "BATCH_JOB_PREVIEW_STALE", "文件内容或业务校验结果已经变化，请重新预检。"
            )
        groups: dict[str, list[ParsedProductImportRow]] = defaultdict(list)
        for row in validated.rows:
            groups[row.product_key].append(row)
        for rows in groups.values():
            current = await self.repository.job_by_no(job_no, for_update=True)
            if current is None:
                raise _not_found()
            if current.cancel_requested_at is not None:
                current.job_status = "cancelled"
                current.finished_at = utc_now()
                current.version += 1
                _event(self.session, current, "admin_batch_job.cancelled.v1")
                await self.session.commit()
                return
            try:
                product_no = await self._create_product_group(current, store, rows)
                for row in rows:
                    item = await self.repository.item_by_key(current.id, str(row.row_number))
                    if item is not None and item.item_status == "valid":
                        item.item_status = "succeeded"
                        item.resource_type = "product"
                        item.resource_no = product_no
                await self.session.commit()
            except (ApplicationError, IntegrityError) as exc:
                await self.session.rollback()
                locked = await self.repository.job_by_no(job_no, for_update=True)
                if locked is None:
                    raise _not_found() from exc
                code = (
                    exc.code if isinstance(exc, ApplicationError) else "IMPORT_CONCURRENT_CONFLICT"
                )
                for row in rows:
                    item = await self.repository.item_by_key(locked.id, str(row.row_number))
                    if item is not None and item.item_status == "valid":
                        item.item_status = "failed"
                        item.error_code = code
                        item.error_message = "执行时资源状态发生变化，本商品组未导入。"
                await self.session.commit()
        final = await self.repository.job_by_no(job_no, for_update=True)
        if final is None:
            raise _not_found()
        statuses = await self.repository.items(
            final.id, item_status=None, cursor_id=None, limit=100_000
        )
        succeeded = sum(item.item_status == "succeeded" for item in statuses)
        failed = sum(item.item_status in {"invalid", "failed"} for item in statuses)
        final.success_count = succeeded
        final.failure_count = failed
        final.total_count = len(statuses)
        final.job_status = "succeeded" if failed == 0 else ("partial" if succeeded else "failed")
        final.finished_at = utc_now()
        final.version += 1
        _event(self.session, final, "admin_batch_job.execution_completed.v1")
        await self.session.commit()

    async def _create_product_group(
        self,
        job: AdminBatchJob,
        store: Store,
        rows: list[ParsedProductImportRow],
    ) -> str:
        first = rows[0]
        category = cast(
            Category | None,
            await self.session.scalar(
                select(Category).where(
                    Category.category_no == first.category_id,
                    Category.category_status == "active",
                )
            ),
        )
        brand = None
        if first.brand_id:
            brand = cast(
                Brand | None,
                await self.session.scalar(
                    select(Brand).where(
                        Brand.brand_no == first.brand_id,
                        Brand.brand_status == "active",
                    )
                ),
            )
        if category is None or (first.brand_id and brand is None):
            raise _conflict("IMPORT_REFERENCE_CHANGED", "类目或品牌状态已经变化。")
        product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=store.id,
            category_id=category.id,
            brand_id=brand.id if brand else None,
            product_name=first.product_name,
            subtitle=first.subtitle,
            product_status="draft",
            min_price_amount=min(row.sale_price_amount for row in rows),
            max_price_amount=max(row.sale_price_amount for row in rows),
            currency="CNY",
        )
        self.session.add(product)
        await self.session.flush()
        for index, row in enumerate(rows):
            sku = ProductSku(
                sku_no=new_prefixed_ulid("sku_"),
                product_id=product.id,
                store_id=store.id,
                merchant_sku_code=row.merchant_sku_code,
                sku_name=row.sku_name,
                spec_values=row.spec_values,
                spec_signature=_spec_signature(row.spec_values),
                sale_price_amount=row.sale_price_amount,
                market_price_amount=row.market_price_amount,
                currency=row.currency,
                weight_grams=row.weight_grams,
                barcode=row.barcode,
                sku_status="active",
            )
            self.session.add(sku)
            await self.session.flush()
            inventory = Inventory(
                sku_id=sku.id,
                on_hand_quantity=row.initial_stock,
                inventory_status="active",
            )
            self.session.add(inventory)
            await self.session.flush()
            if row.initial_stock:
                self.session.add(
                    InventoryLog(
                        inventory_id=inventory.id,
                        sku_id=sku.id,
                        operation_type="import_initial_stock",
                        on_hand_delta=row.initial_stock,
                        reserved_delta=0,
                        on_hand_before=0,
                        on_hand_after=row.initial_stock,
                        reserved_before=0,
                        reserved_after=0,
                        reference_type="admin_batch_job",
                        reference_no=job.job_no,
                        idempotency_key=f"{job.job_no}:{row.row_number}:stock",
                        actor_type="admin",
                        actor_id=job.requested_by,
                        reason="商品批量导入初始库存",
                        inventory_version=inventory.version,
                    )
                )
            if index == 0:
                product.default_sku_id = sku.id
        self.session.add(
            ProductStatusLog(
                product_id=product.id,
                from_status=None,
                to_status="draft",
                event_type="imported",
                actor_type="admin",
                actor_id=job.requested_by,
                reason_code="batch_import",
                reason="商品批量导入",
                product_version=product.version,
                request_id=job.trace_id,
                trace_id=job.trace_id,
            )
        )
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="product.imported.v1",
                aggregate_type="product",
                aggregate_no=product.product_no,
                aggregate_version=product.version,
                payload={"product_id": product.product_no, "job_id": job.job_no},
                event_status="pending",
                available_at=utc_now(),
                trace_id=job.trace_id,
            )
        )
        await self.session.flush()
        return product.product_no

    async def _read_and_validate(
        self, job: AdminBatchJob, source: FileObject, store: Store
    ) -> ValidatedImport:
        content = await self.storage.read(source.bucket, source.object_key, source.size_bytes)
        if hashlib.sha256(content).digest() != source.sha256:
            raise _conflict("IMPORT_FILE_HASH_CHANGED", "导入文件校验值发生变化。")
        parsed = parse_product_import(content, source.detected_mime_type)
        return await self._validate_references(parsed, store)

    async def _validate_references(
        self, parsed: ProductImportParseResult, store: Store
    ) -> ValidatedImport:
        category_nos = {row.category_id for row in parsed.rows}
        brand_nos = {row.brand_id for row in parsed.rows if row.brand_id}
        categories = set(
            (
                await self.session.scalars(
                    select(Category.category_no).where(
                        Category.category_no.in_(category_nos), Category.category_status == "active"
                    )
                )
            ).all()
        )
        brands = (
            set(
                (
                    await self.session.scalars(
                        select(Brand.brand_no).where(
                            Brand.brand_no.in_(brand_nos), Brand.brand_status == "active"
                        )
                    )
                ).all()
            )
            if brand_nos
            else set()
        )
        sku_codes = {row.merchant_sku_code for row in parsed.rows}
        existing_codes = set(
            (
                await self.session.scalars(
                    select(ProductSku.merchant_sku_code).where(
                        ProductSku.store_id == store.id,
                        ProductSku.active_merchant_sku_code.in_(sku_codes),
                    )
                )
            ).all()
        )
        problems = list(parsed.problems)
        invalid_products: set[str] = set()
        for row in parsed.rows:
            code = None
            message = None
            if row.category_id not in categories:
                code, message = "IMPORT_CATEGORY_NOT_ACTIVE", "类目不存在或已停用。"
            elif row.brand_id and row.brand_id not in brands:
                code, message = "IMPORT_BRAND_NOT_ACTIVE", "品牌不存在或已停用。"
            elif row.merchant_sku_code in existing_codes:
                code, message = "IMPORT_SKU_CODE_EXISTS", "店铺中已存在相同 SKU 编码。"
            if code and message:
                invalid_products.add(row.product_key)
                problems.append(RowProblem(row.row_number, code, message, _row_hash(row)))
        problem_rows = {problem.row_number for problem in problems}
        valid_rows: list[ParsedProductImportRow] = []
        for row in parsed.rows:
            if row.product_key in invalid_products:
                if row.row_number not in problem_rows:
                    problems.append(
                        RowProblem(
                            row.row_number,
                            "IMPORT_PRODUCT_GROUP_INVALID",
                            "同一商品组存在错误，本行不会执行。",
                            _row_hash(row),
                        )
                    )
            else:
                valid_rows.append(row)
        problems.sort(key=lambda item: item.row_number)
        digest_payload = {
            "rows": [row.model_dump(mode="json") for row in valid_rows],
            "problems": [
                {"row": problem.row_number, "code": problem.code, "hash": problem.input_hash.hex()}
                for problem in problems
            ],
        }
        digest = hashlib.sha256(
            json.dumps(
                digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        return ValidatedImport(tuple(valid_rows), tuple(problems), digest)

    async def _write_error_report(
        self, job: AdminBatchJob, store: Store, problems: tuple[RowProblem, ...]
    ) -> FileObject | None:
        if not problems:
            return None
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(("row_number", "error_code", "error_message"))
        for problem in problems:
            writer.writerow((problem.row_number, problem.code, problem.message))
        content = ("\ufeff" + output.getvalue()).encode("utf-8")
        object_key = f"v1/admin_import/{utc_now():%Y/%m}/{job.job_no}/validation-errors.csv"
        metadata = await self.storage.put("admin-job-artifacts", object_key, content, "text/csv")
        file = FileObject(
            file_no=new_prefixed_ulid("file_"),
            bucket="admin-job-artifacts",
            object_key=object_key,
            purpose="admin_import",
            owner_type="store",
            owner_no=store.store_no,
            variant="error_report",
            declared_mime_type="text/csv",
            detected_mime_type="text/csv",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).digest(),
            provider_checksum=metadata.etag,
            visibility="private",
            sensitivity_level="L1",
            scan_status="safe",
            file_status="active",
            storage_version_id=metadata.version_id,
            reference_count=1,
            activated_at=utc_now(),
        )
        self.session.add(file)
        await self.session.flush()
        return file

    async def _job_source_store(self, job_no: str) -> tuple[AdminBatchJob, FileObject, Store]:
        job = await self.repository.job_by_no(job_no, for_update=True)
        if job is None or job.job_type != "product_import":
            raise _not_found()
        if not await self.repository.requester_is_authorized(job):
            raise _forbidden(
                "BATCH_JOB_PERMISSION_REVOKED",
                "任务发起人的权限或数据范围已经失效。",
            )
        source = await self.repository.file_by_id(job.input_file_id)
        store = await self.repository.store_by_id(job.scope_id)
        if source is None or store is None:
            raise _not_found()
        if (
            source.purpose != "admin_import"
            or source.owner_type != "store"
            or source.owner_no != store.store_no
            or source.file_status != "active"
            or source.scan_status != "safe"
        ):
            raise _conflict("IMPORT_FILE_NOT_READY", "导入文件不可用或不属于当前店铺。")
        return job, source, store

    async def _fail(self, job_no: str, code: str, summary: str) -> None:
        job = await self.repository.job_by_no(job_no, for_update=True)
        if job is None or job.job_status in {"succeeded", "partial", "cancelled", "expired"}:
            await self.session.rollback()
            return
        job.job_status = "failed"
        job.error_code = code[:64]
        job.error_summary = summary[:1000]
        job.finished_at = utc_now()
        job.version += 1
        _event(self.session, job, "admin_batch_job.failed.v1")
        await self.session.commit()


def _spec_signature(values: list[dict[str, str]]) -> bytes:
    normalized = sorted(
        (item["name"].strip().casefold(), item["value"].strip().casefold()) for item in values
    )
    return hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
    ).digest()


def _row_hash(row: ParsedProductImportRow) -> bytes:
    return hashlib.sha256(
        json.dumps(
            row.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).digest()


def _event(session: AsyncSession, job: AdminBatchJob, event_type: str) -> None:
    session.add(
        OutboxEvent(
            event_no=new_prefixed_ulid("evt_"),
            event_type=event_type,
            aggregate_type="admin_batch_job",
            aggregate_no=job.job_no,
            aggregate_version=job.version,
            payload={"job_id": job.job_no, "job_type": job.job_type, "status": job.job_status},
            event_status="pending",
            available_at=utc_now(),
            trace_id=job.trace_id,
        )
    )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404, code="RESOURCE_NOT_FOUND", title="Resource not found", detail="未找到该资源。"
    )


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Batch job conflict", detail=detail)


def _forbidden(code: str, detail: str) -> ApplicationError:
    return ApplicationError(
        status=403, code=code, title="Batch job permission denied", detail=detail
    )
