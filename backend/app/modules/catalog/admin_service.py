from __future__ import annotations

import unicodedata

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import utc_now
from app.modules.catalog.admin_repository import AdminCatalogRepository
from app.modules.catalog.admin_schemas import (
    AdminBrandCreateRequest,
    AdminBrandUpdateRequest,
    AdminBrandView,
    AdminCategoryCreateRequest,
    AdminCategoryUpdateRequest,
    AdminCategoryView,
    AdminInventoryAdjustmentRequest,
    AdminInventoryAdjustmentView,
    AdminInventoryList,
    AdminInventoryView,
)
from app.modules.catalog.models import Brand, Category, Product, ProductSku
from app.modules.files.models import FileObject
from app.modules.inventory.models import Inventory, InventoryLog
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.stores.models import Store
from app.modules.system.models import OutboxEvent


class AdminCatalogService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = AdminCatalogRepository(session)
        self.idempotency = IdempotencyService(session)

    async def list_categories(self, access: AdminAccess) -> list[AdminCategoryView]:
        access.require_scope("platform", 0)
        rows = await self.repository.categories()
        assets = await self.repository.files_by_object_keys(
            [item.icon_object_key for item in rows if item.icon_object_key]
        )
        return [
            self._category_view(item, rows, assets.get(item.icon_object_key or "")) for item in rows
        ]

    async def create_category(
        self,
        access: AdminAccess,
        payload: AdminCategoryCreateRequest,
        idempotency_key: str,
    ) -> AdminCategoryView:
        access.require_scope("platform", 0)
        claim = await self.idempotency.begin(
            scope_key=f"admin:category:create:{access.context.user.user_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="category",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.category_by_no(claim.record.resource_no)
            if existing is not None:
                rows = await self.repository.categories()
                assets = await self.repository.files_by_object_keys(
                    [existing.icon_object_key] if existing.icon_object_key else []
                )
                return self._category_view(
                    existing, rows, assets.get(existing.icon_object_key or "")
                )
        parent = await self._category_parent(payload.parent_id)
        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            parent_id=parent.id if parent else None,
            category_name=payload.category_name,
            category_code=payload.category_code,
            path="/pending",
            level=(parent.level + 1) if parent else 1,
            sort_order=payload.sort_order,
            icon_object_key=await self._asset_key(payload.icon_file_id, "category_icon"),
            category_status="active",
        )
        self.session.add(category)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _conflict("CATEGORY_ALREADY_EXISTS", "分类编码已存在。") from exc
        category.path = f"{parent.path if parent else ''}/{category.id}"
        record_admin_operation(
            self.session,
            access,
            action="create_category",
            target_type="category",
            target_no=category.category_no,
            after={"category_code": category.category_code, "parent_id": payload.parent_id},
        )
        self.idempotency.complete(claim, response_status=201, resource_no=category.category_no)
        await self.session.commit()
        rows = await self.repository.categories()
        assets = await self.repository.files_by_object_keys(
            [category.icon_object_key] if category.icon_object_key else []
        )
        return self._category_view(category, rows, assets.get(category.icon_object_key or ""))

    async def update_category(
        self,
        access: AdminAccess,
        category_no: str,
        payload: AdminCategoryUpdateRequest,
        expected_version: int,
    ) -> AdminCategoryView:
        access.require_scope("platform", 0)
        category = await self.repository.category_by_no(category_no, for_update=True)
        if category is None:
            raise _not_found()
        _check_version(category.version, expected_version)
        before = _category_snapshot(category)
        fields = payload.model_fields_set
        old_path = category.path
        old_level = category.level
        if "parent_id" in fields:
            parent = await self._category_parent(payload.parent_id)
            if parent and (parent.id == category.id or parent.path.startswith(f"{category.path}/")):
                raise _conflict("CATEGORY_CYCLE", "分类不能移动到自身或其子分类下。")
            descendants = await self.repository.category_descendants(category)
            category.parent_id = parent.id if parent else None
            category.path = f"{parent.path if parent else ''}/{category.id}"
            category.level = (parent.level + 1) if parent else 1
            level_delta = category.level - old_level
            for descendant in descendants:
                descendant.path = category.path + descendant.path[len(old_path) :]
                descendant.level += level_delta
                descendant.version += 1
        if payload.category_name is not None:
            category.category_name = payload.category_name
        if payload.category_code is not None:
            category.category_code = payload.category_code
        if payload.sort_order is not None:
            category.sort_order = payload.sort_order
        if "icon_file_id" in fields:
            category.icon_object_key = await self._asset_key(payload.icon_file_id, "category_icon")
        if payload.status is not None:
            category.category_status = payload.status
        category.version += 1
        record_admin_operation(
            self.session,
            access,
            action="update_category",
            target_type="category",
            target_no=category.category_no,
            before=before,
            after=_category_snapshot(category),
        )
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _conflict("CATEGORY_ALREADY_EXISTS", "分类编码已存在。") from exc
        rows = await self.repository.categories()
        assets = await self.repository.files_by_object_keys(
            [category.icon_object_key] if category.icon_object_key else []
        )
        return self._category_view(category, rows, assets.get(category.icon_object_key or ""))

    async def list_brands(self, access: AdminAccess) -> list[AdminBrandView]:
        access.require_scope("platform", 0)
        rows = await self.repository.brands()
        assets = await self.repository.files_by_object_keys(
            [item.logo_object_key for item in rows if item.logo_object_key]
        )
        return [self._brand_view(item, assets.get(item.logo_object_key or "")) for item in rows]

    async def create_brand(
        self,
        access: AdminAccess,
        payload: AdminBrandCreateRequest,
        idempotency_key: str,
    ) -> AdminBrandView:
        access.require_scope("platform", 0)
        claim = await self.idempotency.begin(
            scope_key=f"admin:brand:create:{access.context.user.user_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="brand",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.brand_by_no(claim.record.resource_no)
            if existing is not None:
                assets = await self.repository.files_by_object_keys(
                    [existing.logo_object_key] if existing.logo_object_key else []
                )
                return self._brand_view(existing, assets.get(existing.logo_object_key or ""))
        brand = Brand(
            brand_no=new_prefixed_ulid("brd_"),
            brand_name=payload.brand_name,
            brand_name_normalized=_normalize_name(payload.brand_name),
            logo_object_key=await self._asset_key(payload.logo_file_id, "brand_logo"),
            description=payload.description,
            brand_status="active",
        )
        self.session.add(brand)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _conflict("BRAND_ALREADY_EXISTS", "品牌名称已存在。") from exc
        record_admin_operation(
            self.session,
            access,
            action="create_brand",
            target_type="brand",
            target_no=brand.brand_no,
            after={"brand_name": brand.brand_name},
        )
        self.idempotency.complete(claim, response_status=201, resource_no=brand.brand_no)
        await self.session.commit()
        assets = await self.repository.files_by_object_keys(
            [brand.logo_object_key] if brand.logo_object_key else []
        )
        return self._brand_view(brand, assets.get(brand.logo_object_key or ""))

    async def update_brand(
        self,
        access: AdminAccess,
        brand_no: str,
        payload: AdminBrandUpdateRequest,
        expected_version: int,
    ) -> AdminBrandView:
        access.require_scope("platform", 0)
        brand = await self.repository.brand_by_no(brand_no, for_update=True)
        if brand is None:
            raise _not_found()
        _check_version(brand.version, expected_version)
        before = _brand_snapshot(brand)
        if payload.brand_name is not None:
            brand.brand_name = payload.brand_name
            brand.brand_name_normalized = _normalize_name(payload.brand_name)
        if "logo_file_id" in payload.model_fields_set:
            brand.logo_object_key = await self._asset_key(payload.logo_file_id, "brand_logo")
        if "description" in payload.model_fields_set:
            brand.description = payload.description
        if payload.status is not None:
            brand.brand_status = payload.status
        brand.version += 1
        record_admin_operation(
            self.session,
            access,
            action="update_brand",
            target_type="brand",
            target_no=brand.brand_no,
            before=before,
            after=_brand_snapshot(brand),
        )
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _conflict("BRAND_ALREADY_EXISTS", "品牌名称已存在。") from exc
        assets = await self.repository.files_by_object_keys(
            [brand.logo_object_key] if brand.logo_object_key else []
        )
        return self._brand_view(brand, assets.get(brand.logo_object_key or ""))

    async def list_inventories(
        self,
        access: AdminAccess,
        *,
        store_no: str | None,
        product_no: str | None,
        q: str | None,
        limit: int,
    ) -> AdminInventoryList:
        rows = await self.repository.inventories(
            access.scopes,
            store_no=store_no,
            product_no=product_no,
            q=q.strip() if q else None,
            limit=limit,
        )
        return AdminInventoryList(items=[_inventory_view(*row) for row in rows])

    async def get_inventory(self, access: AdminAccess, sku_no: str) -> AdminInventoryView:
        row = await self.repository.inventory_by_sku_no(sku_no)
        if row is None:
            raise _not_found()
        access.require_scope("store", row[3].id)
        return _inventory_view(*row)

    async def adjust_inventory(
        self,
        access: AdminAccess,
        payload: AdminInventoryAdjustmentRequest,
        idempotency_key: str,
    ) -> AdminInventoryAdjustmentView:
        claim = await self.idempotency.begin(
            scope_key=f"admin:inventory-adjust:{access.context.user.user_no}:{payload.sku_id}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="inventory_adjustment",
        )
        row = await self.repository.inventory_by_sku_no(payload.sku_id, for_update=True)
        if row is None:
            raise _not_found()
        inventory, sku, product, store = row
        access.require_scope("store", store.id)
        if claim.replayed:
            log_rows = await self.repository.inventory_logs(
                access.scopes, sku_no=payload.sku_id, limit=20
            )
            matched = next(
                (item for item in log_rows if item[0].idempotency_key == idempotency_key),
                None,
            )
            if matched is not None:
                return _adjustment_view(
                    matched[0],
                    inventory,
                    matched[1],
                    matched[2],
                    matched[3],
                    reason_code=payload.reason_code,
                )
        _check_version(inventory.version, payload.expected_version)
        on_hand_after = inventory.on_hand_quantity + payload.on_hand_delta
        minimum_required = inventory.reserved_quantity + inventory.safety_stock_quantity
        if on_hand_after < minimum_required:
            raise ApplicationError(
                status=409,
                code="INVENTORY_ADJUSTMENT_WOULD_VIOLATE_RESERVATIONS",
                title="Inventory adjustment rejected",
                detail="调整后库存不能低于已预占数量与安全库存之和。",
            )
        before_quantity = inventory.on_hand_quantity
        next_version = inventory.version + 1
        inventory.on_hand_quantity = on_hand_after
        inventory.version = next_version
        log = InventoryLog(
            inventory_id=inventory.id,
            sku_id=sku.id,
            operation_type="adjust",
            on_hand_delta=payload.on_hand_delta,
            reserved_delta=0,
            on_hand_before=before_quantity,
            on_hand_after=on_hand_after,
            reserved_before=inventory.reserved_quantity,
            reserved_after=inventory.reserved_quantity,
            reference_type="admin_adjustment",
            reference_no=payload.reference_no,
            idempotency_key=idempotency_key,
            actor_type="admin",
            actor_id=access.context.user.id,
            reason=f"{payload.reason_code}: {payload.reason}",
            inventory_version=next_version,
        )
        self.session.add(log)
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="inventory.adjusted.v1",
                aggregate_type="inventory",
                aggregate_no=sku.sku_no,
                aggregate_version=next_version,
                payload={
                    "sku_id": sku.sku_no,
                    "product_id": product.product_no,
                    "store_id": store.store_no,
                    "on_hand_delta": payload.on_hand_delta,
                    "on_hand_after": on_hand_after,
                    "reference_no": payload.reference_no,
                },
                event_status="pending",
                available_at=utc_now(),
                trace_id=_request_id(),
            )
        )
        record_admin_operation(
            self.session,
            access,
            action="adjust_inventory",
            target_type="sku",
            target_no=sku.sku_no,
            reason=payload.reason,
            before={"on_hand_quantity": before_quantity, "version": next_version - 1},
            after={"on_hand_quantity": on_hand_after, "version": next_version},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=sku.sku_no,
            response_body={"inventory_version": next_version},
        )
        await self.session.flush()
        await self.session.refresh(log, attribute_names=["created_at"])
        result = _adjustment_view(
            log, inventory, sku, product, store, reason_code=payload.reason_code
        )
        await self.session.commit()
        return result

    async def _category_parent(self, parent_no: str | None) -> Category | None:
        if parent_no is None:
            return None
        parent = await self.repository.category_by_no(parent_no, for_update=True)
        if parent is None:
            raise _not_found()
        if parent.category_status != "active":
            raise _conflict("CATEGORY_PARENT_DISABLED", "不能使用已停用分类作为父分类。")
        return parent

    async def _asset_key(self, file_no: str | None, purpose: str) -> str | None:
        if file_no is None:
            return None
        file_object = await self.repository.active_file(file_no, purpose)
        if file_object is None:
            raise ApplicationError(
                status=422,
                code="FILE_NOT_BINDABLE",
                title="File cannot be bound",
                detail="文件不存在、未通过安全扫描或用途不匹配。",
            )
        return file_object.object_key

    @staticmethod
    def _category_view(
        category: Category, rows: list[Category], icon: FileObject | None
    ) -> AdminCategoryView:
        by_id = {item.id: item for item in rows}
        parent = by_id.get(category.parent_id) if category.parent_id else None
        path_codes: list[str] = []
        current: Category | None = category
        seen: set[int] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            path_codes.append(current.category_code)
            current = by_id.get(current.parent_id) if current.parent_id else None
        path_codes.reverse()
        return AdminCategoryView(
            category_id=category.category_no,
            parent_id=parent.category_no if parent else None,
            category_name=category.category_name,
            category_code=category.category_code,
            path="/" + "/".join(path_codes),
            level=category.level,
            sort_order=category.sort_order,
            icon_url=f"/api/v1/files/{icon.file_no}" if icon else None,
            status=category.category_status,
            version=category.version,
        )

    @staticmethod
    def _brand_view(brand: Brand, logo: FileObject | None) -> AdminBrandView:
        return AdminBrandView(
            brand_id=brand.brand_no,
            brand_name=brand.brand_name,
            logo_url=f"/api/v1/files/{logo.file_no}" if logo else None,
            description=brand.description,
            status=brand.brand_status,
            version=brand.version,
        )


def _inventory_view(
    inventory: Inventory, sku: ProductSku, product: Product, store: Store
) -> AdminInventoryView:
    available = max(
        inventory.on_hand_quantity - inventory.reserved_quantity - inventory.safety_stock_quantity,
        0,
    )
    return AdminInventoryView(
        sku_id=sku.sku_no,
        sku_name=sku.sku_name,
        product_id=product.product_no,
        product_name=product.product_name,
        store_id=store.store_no,
        store_name=store.store_name,
        on_hand_quantity=inventory.on_hand_quantity,
        reserved_quantity=inventory.reserved_quantity,
        safety_stock_quantity=inventory.safety_stock_quantity,
        available_quantity=available,
        sold_quantity=inventory.sold_quantity,
        status=inventory.inventory_status,
        last_reconciled_at=inventory.last_reconciled_at,
        version=inventory.version,
    )


def _adjustment_view(
    log: InventoryLog,
    inventory: Inventory,
    sku: ProductSku,
    product: Product,
    store: Store,
    *,
    reason_code: str,
) -> AdminInventoryAdjustmentView:
    available = max(
        log.on_hand_after - log.reserved_after - inventory.safety_stock_quantity,
        0,
    )
    reason = log.reason or ""
    if reason.startswith(f"{reason_code}: "):
        reason = reason[len(reason_code) + 2 :]
    return AdminInventoryAdjustmentView(
        adjustment_id=f"{log.reference_type}:{log.reference_no}:{log.inventory_version}",
        inventory=AdminInventoryView(
            sku_id=sku.sku_no,
            sku_name=sku.sku_name,
            product_id=product.product_no,
            product_name=product.product_name,
            store_id=store.store_no,
            store_name=store.store_name,
            on_hand_quantity=log.on_hand_after,
            reserved_quantity=log.reserved_after,
            safety_stock_quantity=inventory.safety_stock_quantity,
            available_quantity=available,
            sold_quantity=inventory.sold_quantity,
            status=inventory.inventory_status,
            last_reconciled_at=inventory.last_reconciled_at,
            version=log.inventory_version,
        ),
        on_hand_delta=log.on_hand_delta,
        reason_code=reason_code,
        reason=reason,
        reference_no=log.reference_no,
        adjusted_at=log.created_at,
    )


def _category_snapshot(category: Category) -> dict[str, object]:
    return {
        "category_name": category.category_name,
        "category_code": category.category_code,
        "parent_id": category.parent_id,
        "status": category.category_status,
        "version": category.version,
    }


def _brand_snapshot(brand: Brand) -> dict[str, object]:
    return {
        "brand_name": brand.brand_name,
        "status": brand.brand_status,
        "version": brand.version,
    }


def _normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def _check_version(actual: int, expected: int) -> None:
    if actual != expected:
        raise ApplicationError(
            status=412,
            code="RESOURCE_VERSION_CONFLICT",
            title="Resource version conflict",
            detail="资源已被其他操作更新，请刷新后重试。",
        )


def _request_id() -> str:
    return request_id_context.get() or new_prefixed_ulid("req_")


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="未找到该资源。",
    )


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Resource conflict", detail=detail)
