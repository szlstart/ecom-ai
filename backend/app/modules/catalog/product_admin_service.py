from __future__ import annotations

import hashlib
import json
from typing import Literal, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyClaim, IdempotencyService
from app.core.pagination import CursorCodec
from app.core.security import utc_now
from app.modules.catalog.content_sanitizer import SanitizedContent, sanitize_content
from app.modules.catalog.models import (
    Brand,
    Category,
    Product,
    ProductAttribute,
    ProductContentVersion,
    ProductFaq,
    ProductFaqVersion,
    ProductFulfillmentProfile,
    ProductImage,
    ProductSku,
    ProductStatusLog,
)
from app.modules.catalog.product_admin_repository import ProductAdminRepository, ProductRow
from app.modules.catalog.product_admin_schemas import (
    AdminContentVersionCreateRequest,
    AdminContentVersionView,
    AdminFaqCreateRequest,
    AdminFaqPublicationRequest,
    AdminFaqVersionCreateRequest,
    AdminFaqView,
    AdminProductAttributeInput,
    AdminProductAttributeSetRequest,
    AdminProductCommandRequest,
    AdminProductCompleteness,
    AdminProductCreateRequest,
    AdminProductDetail,
    AdminProductFulfillmentRequest,
    AdminProductFulfillmentView,
    AdminProductImageSetRequest,
    AdminProductImageView,
    AdminProductList,
    AdminProductModerationRequest,
    AdminProductSummary,
    AdminProductUpdateRequest,
    AdminSkuCreateRequest,
    AdminSkuSpecValue,
    AdminSkuStatusRequest,
    AdminSkuUpdateRequest,
    AdminSkuView,
)
from app.modules.files.models import FileObject
from app.modules.inventory.models import Inventory
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.stores.models import Store
from app.modules.system.models import OutboxEvent

EDITABLE_STATUSES = {"draft", "rejected", "off_shelf"}


class ProductAdminService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.repository = ProductAdminRepository(session)
        self.idempotency = IdempotencyService(session)
        self.cursor = CursorCodec(settings.security_hmac_secret.get_secret_value())

    async def list_products(
        self,
        access: AdminAccess,
        *,
        store_no: str | None,
        product_status: str | None,
        q: str | None,
        cursor: str | None,
        limit: int,
    ) -> AdminProductList:
        q = q.strip() if q else None
        filter_key = json.dumps(
            {"kind": "admin-products", "store": store_no, "status": product_status, "q": q},
            sort_keys=True,
            separators=(",", ":"),
        )
        position = self.cursor.decode(cursor, filter_key=filter_key)
        if position is not None and (position.direction != "next" or len(position.values) != 1):
            raise _bad_cursor()
        rows = await self.repository.products(
            access.scopes,
            store_no=store_no,
            product_status=product_status,
            q=q,
            cursor_no=position.values[0] if position else None,
            limit=limit,
        )
        visible = rows[:limit]
        return AdminProductList(
            items=[_summary(row) for row in visible],
            next_cursor=(
                self.cursor.encode(filter_key=filter_key, values=(visible[-1][0].product_no,))
                if len(rows) > limit and visible
                else None
            ),
        )

    async def create_product(
        self, access: AdminAccess, payload: AdminProductCreateRequest, key: str
    ) -> AdminProductDetail:
        store = await self.repository.store_by_no(payload.store_id)
        if store is None:
            raise _not_found()
        access.require_scope("store", store.id)
        if store.store_status != "active":
            raise _conflict("STORE_NOT_ACTIVE", "只有启用中的店铺可以创建商品。")
        category, brand = await self._references(payload.category_id, payload.brand_id)
        claim = await self.idempotency.begin(
            scope_key=f"admin:product-create:{store.store_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="product",
        )
        if claim.replayed and claim.record.resource_no:
            return await self.get_product(access, claim.record.resource_no)
        product = Product(
            product_no=new_prefixed_ulid("prd_"),
            store_id=store.id,
            category_id=category.id,
            brand_id=brand.id if brand else None,
            product_name=payload.product_name,
            subtitle=payload.subtitle,
            description=payload.description,
            product_status="draft",
            currency="CNY",
        )
        self.session.add(product)
        await self.session.flush()
        _status_log(self.session, product, access, None, "draft", "created", None, None)
        record_admin_operation(
            self.session,
            access,
            action="create_product",
            target_type="product",
            target_no=product.product_no,
            after={"store_id": store.store_no, "status": "draft"},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(claim, response_status=201, resource_no=product.product_no)
        await self.session.commit()
        row = await self.repository.product_by_no(product.product_no)
        assert row is not None
        return await self._detail(row)

    async def get_product(self, access: AdminAccess, product_no: str) -> AdminProductDetail:
        row = await self._product(access, product_no)
        return await self._detail(row)

    async def update_product(
        self,
        access: AdminAccess,
        product_no: str,
        payload: AdminProductUpdateRequest,
        expected_version: int,
    ) -> AdminProductDetail:
        row = await self._product(access, product_no, for_update=True)
        product, store, category, brand = row
        _editable(product)
        _version(product.version, expected_version)
        before = _product_snapshot(product)
        if payload.category_id is not None:
            category, _ = await self._references(payload.category_id, None)
            product.category_id = category.id
        if "brand_id" in payload.model_fields_set:
            if payload.brand_id is None:
                brand = None
                product.brand_id = None
            else:
                _, brand = await self._references(category.category_no, payload.brand_id)
                product.brand_id = brand.id if brand else None
        if payload.product_name is not None:
            product.product_name = payload.product_name
        if "subtitle" in payload.model_fields_set:
            product.subtitle = payload.subtitle
        if "description" in payload.model_fields_set:
            product.description = payload.description
        product.version += 1
        record_admin_operation(
            self.session,
            access,
            action="update_product",
            target_type="product",
            target_no=product.product_no,
            before=before,
            after=_product_snapshot(product),
            scope_type="store",
            scope_id=store.id,
        )
        await self.session.commit()
        return await self._fresh_detail(access, product.product_no)

    async def skus(self, access: AdminAccess, product_no: str) -> list[AdminSkuView]:
        row = await self._product(access, product_no)
        return [
            _sku_view(item, row[0].product_no) for item in await self.repository.skus(row[0].id)
        ]

    async def create_sku(
        self, access: AdminAccess, product_no: str, payload: AdminSkuCreateRequest, key: str
    ) -> AdminSkuView:
        row = await self._product(access, product_no, for_update=True)
        product, store, _, _ = row
        _editable(product)
        claim = await self.idempotency.begin(
            scope_key=f"admin:sku-create:{product_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="product_sku",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.sku_by_no(product.id, claim.record.resource_no)
            if existing:
                return _sku_view(existing, product.product_no)
        sku = ProductSku(
            sku_no=new_prefixed_ulid("sku_"),
            product_id=product.id,
            store_id=store.id,
            merchant_sku_code=payload.merchant_sku_code,
            sku_name=payload.sku_name,
            spec_values=[item.model_dump() for item in payload.spec_values],
            spec_signature=_spec_signature(payload.spec_values),
            sale_price_amount=payload.sale_price_amount,
            market_price_amount=payload.market_price_amount,
            currency=payload.currency,
            weight_grams=payload.weight_grams,
            barcode=payload.barcode,
            sku_status="active",
        )
        self.session.add(sku)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _conflict("SKU_ALREADY_EXISTS", "规格组合或店铺 SKU 编码已存在。") from exc
        self.session.add(Inventory(sku_id=sku.id, inventory_status="active"))
        if product.default_sku_id is None:
            product.default_sku_id = sku.id
        product.version += 1
        await self.repository.recalculate_price_bounds(product)
        _change_event(self.session, product, store, "product.sku_changed.v1")
        record_admin_operation(
            self.session,
            access,
            action="create_product_sku",
            target_type="sku",
            target_no=sku.sku_no,
            after={"product_id": product.product_no},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(claim, response_status=201, resource_no=sku.sku_no)
        await self.session.commit()
        return _sku_view(sku, product.product_no)

    async def update_sku(
        self,
        access: AdminAccess,
        product_no: str,
        sku_no: str,
        payload: AdminSkuUpdateRequest,
        expected_version: int,
    ) -> AdminSkuView:
        row = await self._product(access, product_no, for_update=True)
        product, store, _, _ = row
        _editable(product)
        sku = await self.repository.sku_by_no(product.id, sku_no, for_update=True)
        if sku is None:
            raise _not_found()
        _version(sku.version, expected_version)
        if "merchant_sku_code" in payload.model_fields_set:
            sku.merchant_sku_code = payload.merchant_sku_code
        if payload.sku_name is not None:
            sku.sku_name = payload.sku_name
        if payload.spec_values is not None:
            sku.spec_values = [item.model_dump() for item in payload.spec_values]
            sku.spec_signature = _spec_signature(payload.spec_values)
        if payload.sale_price_amount is not None:
            sku.sale_price_amount = payload.sale_price_amount
        if payload.market_price_amount is not None:
            sku.market_price_amount = payload.market_price_amount
        if "weight_grams" in payload.model_fields_set:
            sku.weight_grams = payload.weight_grams
        if "barcode" in payload.model_fields_set:
            sku.barcode = payload.barcode
        if sku.market_price_amount < sku.sale_price_amount:
            raise _invalid("SKU_MARKET_PRICE_INVALID", "市场价不能低于销售价。")
        sku.version += 1
        product.version += 1
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _conflict("SKU_ALREADY_EXISTS", "规格组合或店铺 SKU 编码已存在。") from exc
        await self.repository.recalculate_price_bounds(product)
        _change_event(self.session, product, store, "product.sku_changed.v1")
        await self.session.commit()
        return _sku_view(sku, product.product_no)

    async def change_sku_status(
        self,
        access: AdminAccess,
        product_no: str,
        sku_no: str,
        payload: AdminSkuStatusRequest,
        expected_version: int,
        key: str,
    ) -> AdminSkuView:
        row = await self._product(access, product_no, for_update=True)
        product, store, _, _ = row
        sku = await self.repository.sku_by_no(product.id, sku_no, for_update=True)
        if sku is None:
            raise _not_found()
        claim = await self.idempotency.begin(
            scope_key=f"admin:sku-status:{sku_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="product_sku",
        )
        if claim.replayed:
            return _sku_view(sku, product.product_no)
        _version(sku.version, expected_version)
        target = "active" if payload.action == "enable" else "disabled"
        if sku.sku_status == target:
            raise _conflict("ILLEGAL_STATE_TRANSITION", "SKU 已处于目标状态。")
        if target == "disabled" and product.product_status == "on_sale":
            active = [
                item
                for item in await self.repository.skus(product.id)
                if item.sku_status == "active"
            ]
            if len(active) <= 1:
                raise _conflict("LAST_ACTIVE_SKU_REQUIRED", "在售商品必须保留至少一个启用 SKU。")
        sku.sku_status = target
        sku.version += 1
        if target == "disabled" and product.default_sku_id == sku.id:
            product.default_sku_id = next(
                (
                    item.id
                    for item in await self.repository.skus(product.id)
                    if item.id != sku.id and item.sku_status == "active"
                ),
                None,
            )
        elif target == "active" and product.default_sku_id is None:
            product.default_sku_id = sku.id
        product.version += 1
        await self.repository.recalculate_price_bounds(product)
        _change_event(self.session, product, store, "product.sku_status_changed.v1")
        record_admin_operation(
            self.session,
            access,
            action=f"{payload.action}_sku",
            target_type="sku",
            target_no=sku.sku_no,
            reason=payload.reason,
            after={"status": target},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(claim, response_status=200, resource_no=sku.sku_no)
        await self.session.commit()
        return _sku_view(sku, product.product_no)

    async def images(self, access: AdminAccess, product_no: str) -> list[AdminProductImageView]:
        product = (await self._product(access, product_no))[0]
        rows = await self.repository.images(product.id)
        sku_nos = {item.id: item.sku_no for item in await self.repository.skus(product.id)}
        return [
            _image_view(
                image,
                file,
                sku_nos.get(image.sku_id) if image.sku_id is not None else None,
            )
            for image, file in rows
        ]

    async def replace_images(
        self,
        access: AdminAccess,
        product_no: str,
        payload: AdminProductImageSetRequest,
        expected_version: int,
    ) -> list[AdminProductImageView]:
        product, store, _, _ = await self._product(access, product_no, for_update=True)
        _editable(product)
        _version(product.version, expected_version)
        if len({item.file_id for item in payload.items}) != len(payload.items):
            raise _invalid("PRODUCT_IMAGE_DUPLICATE", "同一文件不能重复绑定。")
        spu_main = [
            item for item in payload.items if item.sku_id is None and item.image_type == "main"
        ]
        if len(spu_main) != 1:
            raise _invalid("PRODUCT_MAIN_IMAGE_REQUIRED", "必须且只能设置一张 SPU 主图。")
        if any(item.image_type == "main" and item.sku_id is not None for item in payload.items):
            raise _invalid("PRODUCT_IMAGE_SCOPE_INVALID", "SKU 图片不能声明为 SPU 主图。")
        scopes = [(item.sku_id, item.sort_order) for item in payload.items]
        if len(scopes) != len(set(scopes)):
            raise _invalid("PRODUCT_IMAGE_ORDER_DUPLICATE", "同一图片作用域的排序号不能重复。")
        skus = await self.repository.skus(product.id)
        sku_by_no = {item.sku_no: item for item in skus}
        if any(item.sku_id is not None and item.sku_id not in sku_by_no for item in payload.items):
            raise _invalid("PRODUCT_IMAGE_SKU_INVALID", "图片引用了不属于该商品的 SKU。")
        files = await self.repository.files_by_nos([item.file_id for item in payload.items])
        file_by_no = {item.file_no: item for item in files}
        if len(files) != len(payload.items) or any(
            not _bindable_image(file_by_no.get(item.file_id), store) for item in payload.items
        ):
            raise _invalid(
                "PRODUCT_IMAGE_FILE_NOT_BINDABLE", "图片必须属于当前店铺并已通过安全扫描。"
            )
        for existing, _ in await self.repository.images(product.id):
            await self.session.delete(existing)
        await self.session.flush()
        result: list[AdminProductImageView] = []
        for item in payload.items:
            file = file_by_no[item.file_id]
            image = ProductImage(
                product_id=product.id,
                sku_id=sku_by_no[item.sku_id].id if item.sku_id else None,
                file_id=file.id,
                object_key=file.object_key,
                image_type=item.image_type,
                alt_text=item.alt_text,
                width=file.width or 0,
                height=file.height or 0,
                sort_order=item.sort_order,
                image_status="active",
            )
            self.session.add(image)
            result.append(_image_view(image, file, item.sku_id))
        product.version += 1
        _change_event(self.session, product, store, "product.images_replaced.v1")
        await self.session.commit()
        return result

    async def attributes(
        self, access: AdminAccess, product_no: str
    ) -> list[AdminProductAttributeInput]:
        product = (await self._product(access, product_no))[0]
        return [_attribute_view(item) for item in await self.repository.attributes(product.id)]

    async def replace_attributes(
        self,
        access: AdminAccess,
        product_no: str,
        payload: AdminProductAttributeSetRequest,
        expected_version: int,
    ) -> list[AdminProductAttributeInput]:
        product, store, _, _ = await self._product(access, product_no, for_update=True)
        _editable(product)
        _version(product.version, expected_version)
        codes = [item.attribute_code for item in payload.items]
        if len(codes) != len(set(codes)):
            raise _invalid("PRODUCT_ATTRIBUTE_DUPLICATE", "属性编码不能重复。")
        for existing in await self.repository.attributes(product.id):
            await self.session.delete(existing)
        await self.session.flush()
        for item in payload.items:
            self.session.add(ProductAttribute(product_id=product.id, **item.model_dump()))
        product.version += 1
        _change_event(self.session, product, store, "product.attributes_replaced.v1")
        await self.session.commit()
        return payload.items

    async def set_fulfillment(
        self,
        access: AdminAccess,
        product_no: str,
        payload: AdminProductFulfillmentRequest,
        expected_version: int,
    ) -> AdminProductFulfillmentView:
        product, store, _, _ = await self._product(access, product_no, for_update=True)
        _editable(product)
        _version(product.version, expected_version)
        template = await self.repository.shipping_template(store.id, payload.shipping_template_id)
        if template is None or template.template_status != "effective":
            raise _invalid("SHIPPING_TEMPLATE_NOT_EFFECTIVE", "必须使用本店当前生效的配送模板。")
        profile = await self.repository.fulfillment(product.id)
        if profile is None:
            profile = ProductFulfillmentProfile(
                product_id=product.id,
                shipping_template_id=template.id,
                origin_region_code=payload.origin_region_code,
                dispatch_min_hours=payload.dispatch_min_hours,
                dispatch_max_hours=payload.dispatch_max_hours,
                purchase_notice=payload.purchase_notice,
            )
            self.session.add(profile)
        else:
            profile.shipping_template_id = template.id
            profile.origin_region_code = payload.origin_region_code
            profile.dispatch_min_hours = payload.dispatch_min_hours
            profile.dispatch_max_hours = payload.dispatch_max_hours
            profile.purchase_notice = payload.purchase_notice
            profile.profile_version += 1
            profile.version += 1
        product.version += 1
        _change_event(self.session, product, store, "product.fulfillment_changed.v1")
        await self.session.flush()
        result = AdminProductFulfillmentView(
            **payload.model_dump(), profile_version=profile.profile_version, version=profile.version
        )
        await self.session.commit()
        return result

    async def create_content_version(
        self,
        access: AdminAccess,
        product_no: str,
        payload: AdminContentVersionCreateRequest,
        key: str,
    ) -> AdminContentVersionView:
        product, store, _, _ = await self._product(access, product_no, for_update=True)
        _editable(product)
        claim = await self.idempotency.begin(
            scope_key=f"admin:product-content-create:{product_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="product_content_version",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.content_version_by_no(
                product.id, claim.record.resource_no
            )
            if existing:
                return _content_view(existing)
        sanitized = sanitize_content(payload.source_format, payload.source_content)
        await self._validate_content_files(store, sanitized)
        version = ProductContentVersion(
            content_version_no=new_prefixed_ulid("pcv_"),
            product_id=product.id,
            content_version=await self.repository.next_content_version(product.id),
            source_format=payload.source_format,
            source_content=payload.source_content,
            source_hash=hashlib.sha256(payload.source_content.encode()).digest(),
            public_content_format=sanitized.public_content_format,
            safe_blocks=sanitized.safe_blocks,
            safe_html=sanitized.safe_html,
            safe_text=sanitized.safe_text,
            content_hash=hashlib.sha256(sanitized.safe_text.encode()).digest(),
            sanitizer_policy_version=1,
            content_schema_version=1,
            security_scan_status="passed",
            version_status="draft",
            created_by=access.context.user.id,
        )
        self.session.add(version)
        await self.session.flush()
        product.current_detail_content_version_id = version.id
        product.version += 1
        _change_event(self.session, product, store, "product.content_version_created.v1")
        self.idempotency.complete(
            claim, response_status=201, resource_no=version.content_version_no
        )
        await self.session.refresh(version, attribute_names=["created_at"])
        result = _content_view(version)
        await self.session.commit()
        return result

    async def content_version(
        self, access: AdminAccess, product_no: str, version_no: str
    ) -> AdminContentVersionView:
        product = (await self._product(access, product_no))[0]
        version = await self.repository.content_version_by_no(product.id, version_no)
        if version is None:
            raise _not_found()
        return _content_view(version)

    async def faqs(self, access: AdminAccess, product_no: str) -> list[AdminFaqView]:
        product = (await self._product(access, product_no))[0]
        return [
            await self._faq_view(item, product.product_no)
            for item in await self.repository.faqs(product.id)
        ]

    async def create_faq(
        self,
        access: AdminAccess,
        product_no: str,
        payload: AdminFaqCreateRequest,
        key: str,
    ) -> AdminFaqView:
        product, store, _, _ = await self._product(access, product_no, for_update=True)
        if product.product_status == "archived":
            raise _conflict("PRODUCT_NOT_EDITABLE", "归档商品不能新增 FAQ。")
        claim = await self.idempotency.begin(
            scope_key=f"admin:product-faq-create:{product_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="product_faq",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.faq_by_no(product.id, claim.record.resource_no)
            if existing:
                return await self._faq_view(existing, product.product_no)
        sanitized = sanitize_content(payload.source_format, payload.source_content)
        await self._validate_content_files(store, sanitized)
        faq = ProductFaq(
            faq_no=new_prefixed_ulid("faq_"),
            product_id=product.id,
            question=" ".join(payload.question.split()),
            faq_status="draft",
            sort_order=payload.sort_order,
        )
        self.session.add(faq)
        await self.session.flush()
        version = await self._new_faq_version(faq, payload, sanitized, access)
        self.session.add(version)
        await self.session.flush()
        faq.current_content_version_id = version.id
        product.version += 1
        _change_event(self.session, product, store, "product.faq_version_created.v1")
        self.idempotency.complete(claim, response_status=201, resource_no=faq.faq_no)
        await self.session.commit()
        return await self._faq_view(faq, product.product_no)

    async def create_faq_version(
        self,
        access: AdminAccess,
        product_no: str,
        faq_no: str,
        payload: AdminFaqVersionCreateRequest,
        key: str,
    ) -> AdminContentVersionView:
        product, store, _, _ = await self._product(access, product_no, for_update=True)
        if product.product_status == "archived":
            raise _conflict("PRODUCT_NOT_EDITABLE", "归档商品不能新增 FAQ 版本。")
        faq = await self.repository.faq_by_no(product.id, faq_no, for_update=True)
        if faq is None:
            raise _not_found()
        claim = await self.idempotency.begin(
            scope_key=f"admin:product-faq-version:{faq_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="product_faq_version",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.faq_version_by_no(faq.id, claim.record.resource_no)
            if existing:
                return _faq_content_view(existing)
        sanitized = sanitize_content(payload.source_format, payload.source_content)
        await self._validate_content_files(store, sanitized)
        version = await self._new_faq_version(faq, payload, sanitized, access)
        self.session.add(version)
        await self.session.flush()
        faq.current_content_version_id = version.id
        faq.version += 1
        product.version += 1
        _change_event(self.session, product, store, "product.faq_version_created.v1")
        self.idempotency.complete(claim, response_status=201, resource_no=version.faq_version_no)
        await self.session.refresh(version, attribute_names=["created_at"])
        result = _faq_content_view(version)
        await self.session.commit()
        return result

    async def publish_faq(
        self,
        access: AdminAccess,
        product_no: str,
        faq_no: str,
        payload: AdminFaqPublicationRequest,
        key: str,
    ) -> AdminFaqView:
        product, store, _, _ = await self._product(access, product_no, for_update=True)
        faq = await self.repository.faq_by_no(product.id, faq_no, for_update=True)
        if faq is None:
            raise _not_found()
        claim = await self.idempotency.begin(
            scope_key=f"admin:product-faq-publish:{faq_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="product_faq",
        )
        if claim.replayed:
            return await self._faq_view(faq, product.product_no)
        version = await self.repository.faq_version_by_no(faq.id, payload.version_id)
        if version is None or version.security_scan_status != "passed":
            raise _invalid("FAQ_VERSION_NOT_PUBLISHABLE", "FAQ 版本不存在或未通过安全扫描。")
        previous = (
            await self.repository.faq_version_by_id(faq.id, faq.published_content_version_id)
            if faq.published_content_version_id
            else None
        )
        if previous and previous.id != version.id:
            previous.version_status = "superseded"
            previous.version += 1
        if version.version_status != "published":
            version.version_status = "published"
            version.published_at = utc_now()
            version.approved_by = access.context.user.id
            version.approved_at = utc_now()
            version.version += 1
        faq.current_content_version_id = version.id
        faq.published_content_version_id = version.id
        faq.faq_status = "published"
        faq.published_at = utc_now()
        faq.version += 1
        product.version += 1
        _change_event(self.session, product, store, "product.faq_published.v1")
        record_admin_operation(
            self.session,
            access,
            action="publish_product_faq",
            target_type="product_faq",
            target_no=faq.faq_no,
            reason=payload.reason,
            after={"version_id": version.faq_version_no},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(claim, response_status=200, resource_no=faq.faq_no)
        await self.session.commit()
        return await self._faq_view(faq, product.product_no)

    async def _faq_view(self, faq: ProductFaq, product_no: str) -> AdminFaqView:
        current = (
            await self.repository.faq_version_by_id(faq.id, faq.current_content_version_id)
            if faq.current_content_version_id
            else None
        )
        published = (
            await self.repository.faq_version_by_id(faq.id, faq.published_content_version_id)
            if faq.published_content_version_id
            else None
        )
        return AdminFaqView(
            faq_id=faq.faq_no,
            product_id=product_no,
            question=faq.question,
            status=faq.faq_status,
            sort_order=faq.sort_order,
            current_version_id=current.faq_version_no if current else None,
            published_version_id=published.faq_version_no if published else None,
            published_at=faq.published_at,
            version=faq.version,
        )

    async def _new_faq_version(
        self,
        faq: ProductFaq,
        payload: AdminFaqCreateRequest | AdminFaqVersionCreateRequest,
        sanitized: SanitizedContent,
        access: AdminAccess,
    ) -> ProductFaqVersion:
        return ProductFaqVersion(
            faq_version_no=new_prefixed_ulid("fqv_"),
            product_faq_id=faq.id,
            content_version=await self.repository.next_faq_version(faq.id),
            source_format=payload.source_format,
            source_content=payload.source_content,
            source_hash=hashlib.sha256(payload.source_content.encode()).digest(),
            public_content_format=sanitized.public_content_format,
            safe_blocks=sanitized.safe_blocks,
            safe_html=sanitized.safe_html,
            safe_text=sanitized.safe_text,
            content_hash=hashlib.sha256(sanitized.safe_text.encode()).digest(),
            sanitizer_policy_version=1,
            content_schema_version=1,
            security_scan_status="passed",
            version_status="draft",
            created_by=access.context.user.id,
        )

    async def submit_review(
        self,
        access: AdminAccess,
        product_no: str,
        payload: AdminProductCommandRequest,
        expected_version: int,
        key: str,
    ) -> AdminProductDetail:
        row = await self._product(access, product_no, for_update=True)
        product, store, _, _ = row
        claim = await self._command_claim(product_no, "review-submit", payload, key)
        if claim.replayed:
            return await self._detail(row)
        _editable(product)
        _version(product.version, expected_version)
        completeness = await self._completeness(product)
        if completeness.missing_requirements:
            raise _conflict(
                "PRODUCT_INCOMPLETE",
                f"商品资料不完整: {', '.join(completeness.missing_requirements)}。",
            )
        previous = product.product_status
        product.product_status = "pending_review"
        product.version += 1
        current = await self.repository.content_version_by_id(
            product.id, product.current_detail_content_version_id
        )
        assert current is not None
        if current.version_status in {"draft", "rejected"}:
            current.version_status = "reviewing"
            current.version += 1
        _status_log(
            self.session,
            product,
            access,
            previous,
            "pending_review",
            "submitted",
            payload.reason_code,
            payload.reason,
        )
        _change_event(self.session, product, store, "product.review_submitted.v1")
        self.idempotency.complete(claim, response_status=200, resource_no=product.product_no)
        await self.session.commit()
        return await self._fresh_detail(access, product.product_no)

    async def moderate(
        self,
        access: AdminAccess,
        product_no: str,
        payload: AdminProductModerationRequest,
        expected_version: int,
        key: str,
    ) -> AdminProductDetail:
        row = await self._product(access, product_no, for_update=True)
        product, store, _, _ = row
        claim = await self._command_claim(product_no, "moderation", payload, key)
        if claim.replayed:
            return await self._detail(row)
        _version(product.version, expected_version)
        if product.product_status != "pending_review":
            raise _conflict("ILLEGAL_STATE_TRANSITION", "只有待审核商品可以执行审核决定。")
        latest = await self.repository.latest_status_log(product.id)
        if latest and latest.event_type == "approved":
            raise _conflict("PRODUCT_ALREADY_APPROVED", "该版本已经审核通过，请执行发布。")
        previous = product.product_status
        event = "approved"
        target = "pending_review"
        if payload.decision in {"reject", "request_changes"}:
            target = "rejected"
            event = "rejected" if payload.decision == "reject" else "changes_requested"
            product.product_status = target
        product.version += 1
        current = await self.repository.content_version_by_id(
            product.id, product.current_detail_content_version_id
        )
        if current:
            if current.version_status not in {"published", "approved"}:
                current.version_status = "approved" if payload.decision == "approve" else "rejected"
                current.approved_by = (
                    access.context.user.id if payload.decision == "approve" else None
                )
                current.approved_at = utc_now() if payload.decision == "approve" else None
                current.version += 1
        _status_log(
            self.session,
            product,
            access,
            previous,
            target,
            event,
            payload.reason_code,
            payload.reason,
        )
        _change_event(self.session, product, store, f"product.{event}.v1")
        self.idempotency.complete(claim, response_status=200, resource_no=product.product_no)
        await self.session.commit()
        return await self._fresh_detail(access, product.product_no)

    async def publish(
        self,
        access: AdminAccess,
        product_no: str,
        payload: AdminProductCommandRequest,
        expected_version: int,
        key: str,
    ) -> AdminProductDetail:
        row = await self._product(access, product_no, for_update=True)
        product, store, category, _ = row
        claim = await self._command_claim(product_no, "publish", payload, key)
        if claim.replayed:
            return await self._detail(row)
        _version(product.version, expected_version)
        latest = await self.repository.latest_status_log(product.id)
        if (
            product.product_status != "pending_review"
            or latest is None
            or latest.event_type != "approved"
            or latest.product_version != product.version
        ):
            raise _conflict("PRODUCT_NOT_APPROVED", "商品当前版本尚未审核通过。")
        if store.store_status != "active" or category.category_status != "active":
            raise _conflict("PRODUCT_PUBLICATION_SCOPE_INVALID", "店铺与平台分类必须处于启用状态。")
        completeness = await self._completeness(product)
        if completeness.missing_requirements:
            raise _conflict("PRODUCT_INCOMPLETE", "商品发布条件已变化，请重新提交审核。")
        current = await self.repository.content_version_by_id(
            product.id, product.current_detail_content_version_id
        )
        assert current is not None
        if current.version_status != "published":
            current.version_status = "published"
            current.published_at = utc_now()
            current.version += 1
        product.published_detail_content_version_id = current.id
        previous = product.product_status
        product.product_status = "on_sale"
        product.published_at = utc_now()
        product.off_shelf_at = None
        product.version += 1
        _status_log(
            self.session,
            product,
            access,
            previous,
            "on_sale",
            "published",
            payload.reason_code,
            payload.reason,
        )
        _change_event(self.session, product, store, "product.published.v1")
        self.idempotency.complete(claim, response_status=200, resource_no=product.product_no)
        await self.session.commit()
        return await self._fresh_detail(access, product.product_no)

    async def off_shelf(
        self,
        access: AdminAccess,
        product_no: str,
        payload: AdminProductCommandRequest,
        expected_version: int,
        key: str,
    ) -> AdminProductDetail:
        row = await self._product(access, product_no, for_update=True)
        product, store, _, _ = row
        claim = await self._command_claim(product_no, "off-shelf", payload, key)
        if claim.replayed:
            return await self._detail(row)
        _version(product.version, expected_version)
        if product.product_status != "on_sale":
            raise _conflict("ILLEGAL_STATE_TRANSITION", "只有在售商品可以下架。")
        product.product_status = "off_shelf"
        product.off_shelf_at = utc_now()
        product.version += 1
        _status_log(
            self.session,
            product,
            access,
            "on_sale",
            "off_shelf",
            "off_shelf",
            payload.reason_code,
            payload.reason,
        )
        _change_event(self.session, product, store, "product.off_shelf.v1")
        self.idempotency.complete(claim, response_status=200, resource_no=product.product_no)
        await self.session.commit()
        return await self._fresh_detail(access, product.product_no)

    async def _product(
        self, access: AdminAccess, product_no: str, *, for_update: bool = False
    ) -> ProductRow:
        row = await self.repository.product_by_no(product_no, for_update=for_update)
        if row is None:
            raise _not_found()
        access.require_scope("store", row[1].id)
        return row

    async def _fresh_detail(
        self, access: AdminAccess, product_no: str
    ) -> AdminProductDetail:
        return await self._detail(await self._product(access, product_no))

    async def _references(
        self, category_no: str, brand_no: str | None
    ) -> tuple[Category, Brand | None]:
        category = await self.repository.category_by_no(category_no)
        if category is None or category.category_status != "active":
            raise _invalid("CATEGORY_NOT_ACTIVE", "必须选择启用中的平台分类。")
        brand = None
        if brand_no:
            brand = await self.repository.brand_by_no(brand_no)
            if brand is None or brand.brand_status != "active":
                raise _invalid("BRAND_NOT_ACTIVE", "必须选择启用中的品牌。")
        return category, brand

    async def _detail(self, row: ProductRow) -> AdminProductDetail:
        product, _, _, _ = row
        completeness = await self._completeness(product)
        current = await self.repository.content_version_by_id(
            product.id, product.current_detail_content_version_id
        )
        published = await self.repository.content_version_by_id(
            product.id, product.published_detail_content_version_id
        )
        latest = await self.repository.latest_status_log(product.id)
        actions: list[str] = []
        if product.product_status in EDITABLE_STATUSES:
            actions.extend(["update", "submit_review"])
        if product.product_status == "pending_review" and not (
            latest and latest.event_type == "approved"
        ):
            actions.append("moderate")
        if (
            product.product_status == "pending_review"
            and latest
            and latest.event_type == "approved"
        ):
            actions.append("publish")
        if product.product_status == "on_sale":
            actions.append("off_shelf")
        return AdminProductDetail(
            **_summary(row).model_dump(),
            description=product.description,
            default_sku_id=await self._sku_no(product.id, product.default_sku_id),
            current_detail_content_version_id=current.content_version_no if current else None,
            published_detail_content_version_id=published.content_version_no if published else None,
            completeness=completeness,
            available_actions=actions,
            published_at=product.published_at,
            off_shelf_at=product.off_shelf_at,
        )

    async def _completeness(self, product: Product) -> AdminProductCompleteness:
        skus = await self.repository.skus(product.id)
        images = await self.repository.images(product.id)
        attributes = await self.repository.attributes(product.id)
        fulfillment = await self.repository.fulfillment(product.id)
        content = await self.repository.content_version_by_id(
            product.id, product.current_detail_content_version_id
        )
        flags = {
            "basic": bool(product.product_name and product.category_id),
            "sku": any(item.sku_status == "active" for item in skus),
            "main_image": any(
                image.sku_id is None
                and image.image_type == "main"
                and image.image_status == "active"
                for image, _ in images
            ),
            "attributes": bool(attributes),
            "fulfillment": fulfillment is not None,
            "detail_content": bool(content and content.security_scan_status == "passed"),
        }
        required = {"basic", "sku", "main_image", "fulfillment", "detail_content"}
        return AdminProductCompleteness(
            **flags,
            missing_requirements=[name for name in required if not flags[name]],
        )

    async def _sku_no(self, product_id: int, sku_id: int | None) -> str | None:
        if sku_id is None:
            return None
        return next(
            (item.sku_no for item in await self.repository.skus(product_id) if item.id == sku_id),
            None,
        )

    async def _validate_content_files(self, store: Store, content: SanitizedContent) -> None:
        files = await self.repository.files_by_nos(list(content.referenced_file_ids))
        if len(files) != len(content.referenced_file_ids) or any(
            file.owner_type != "store"
            or file.owner_no != store.store_no
            or file.purpose not in {"product", "product_detail"}
            or file.file_status != "active"
            or file.scan_status != "safe"
            or file.visibility != "public"
            for file in files
        ):
            raise _invalid(
                "PRODUCT_CONTENT_FILE_NOT_BINDABLE", "详情图片必须属于本店并通过安全扫描。"
            )

    async def _command_claim(
        self,
        product_no: str,
        command: str,
        payload: AdminProductCommandRequest,
        key: str,
    ) -> IdempotencyClaim:
        return await self.idempotency.begin(
            scope_key=f"admin:product-{command}:{product_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="product",
        )


def _summary(row: ProductRow) -> AdminProductSummary:
    product, store, category, brand = row
    return AdminProductSummary(
        product_id=product.product_no,
        store_id=store.store_no,
        store_name=store.store_name,
        category_id=category.category_no,
        category_name=category.category_name,
        brand_id=brand.brand_no if brand else None,
        brand_name=brand.brand_name if brand else None,
        product_name=product.product_name,
        subtitle=product.subtitle,
        status=product.product_status,
        min_price=_money(product.min_price_amount),
        max_price=_money(product.max_price_amount),
        currency=product.currency,
        updated_at=product.updated_at,
        version=product.version,
    )


def _sku_view(sku: ProductSku, product_no: str) -> AdminSkuView:
    return AdminSkuView(
        sku_id=sku.sku_no,
        product_id=product_no,
        merchant_sku_code=sku.merchant_sku_code,
        sku_name=sku.sku_name,
        spec_values=[AdminSkuSpecValue.model_validate(item) for item in sku.spec_values],
        sale_price=_money(sku.sale_price_amount),
        market_price=_money(sku.market_price_amount),
        currency=sku.currency,
        weight_grams=sku.weight_grams,
        barcode=sku.barcode,
        status=sku.sku_status,
        version=sku.version,
    )


def _image_view(image: ProductImage, file: FileObject, sku_no: str | None) -> AdminProductImageView:
    return AdminProductImageView(
        file_id=file.file_no,
        sku_id=sku_no,
        image_type=cast(Literal["main", "gallery", "detail", "spec"], image.image_type),
        alt_text=image.alt_text,
        sort_order=image.sort_order,
        image_url=f"/api/v1/files/{file.file_no}",
        width=image.width,
        height=image.height,
        status=image.image_status,
    )


def _attribute_view(item: ProductAttribute) -> AdminProductAttributeInput:
    return AdminProductAttributeInput(
        attribute_code=item.attribute_code,
        attribute_name=item.attribute_name,
        value_text=item.value_text,
        value_normalized=item.value_normalized,
        unit=item.unit,
        is_searchable=item.is_searchable,
        sort_order=item.sort_order,
    )


def _content_view(version: ProductContentVersion) -> AdminContentVersionView:
    return AdminContentVersionView(
        version_id=version.content_version_no,
        content_version=version.content_version,
        source_format=version.source_format,
        source_content=version.source_content,
        public_content_format=version.public_content_format,
        safe_blocks=version.safe_blocks,
        safe_html=version.safe_html,
        safe_text=version.safe_text,
        security_scan_status=version.security_scan_status,
        status=version.version_status,
        created_at=version.created_at,
    )


def _faq_content_view(version: ProductFaqVersion) -> AdminContentVersionView:
    return AdminContentVersionView(
        version_id=version.faq_version_no,
        content_version=version.content_version,
        source_format=version.source_format,
        source_content=version.source_content,
        public_content_format=version.public_content_format,
        safe_blocks=version.safe_blocks,
        safe_html=version.safe_html,
        safe_text=version.safe_text,
        security_scan_status=version.security_scan_status,
        status=version.version_status,
        created_at=version.created_at,
    )


def _spec_signature(values: list[AdminSkuSpecValue]) -> bytes:
    normalized = [
        {"name": item.name.strip().casefold(), "value": item.value.strip().casefold()}
        for item in sorted(values, key=lambda item: item.name.casefold())
    ]
    return hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
    ).digest()


def _bindable_image(file: FileObject | None, store: Store) -> bool:
    return bool(
        file
        and file.owner_type == "store"
        and file.owner_no == store.store_no
        and file.purpose == "product"
        and file.file_status == "active"
        and file.scan_status == "safe"
        and file.visibility == "public"
        and file.detected_mime_type in {"image/jpeg", "image/png", "image/webp", "image/avif"}
        and file.width
        and file.height
    )


def _status_log(
    session: AsyncSession,
    product: Product,
    access: AdminAccess,
    previous: str | None,
    target: str,
    event: str,
    reason_code: str | None,
    reason: str | None,
) -> None:
    session.add(
        ProductStatusLog(
            product_id=product.id,
            from_status=previous,
            to_status=target,
            event_type=event,
            actor_type="admin",
            actor_id=access.context.user.id,
            reason_code=reason_code,
            reason=reason,
            product_version=product.version,
            request_id=_request_id(),
            trace_id=_request_id(),
        )
    )


def _change_event(session: AsyncSession, product: Product, store: Store, event_type: str) -> None:
    session.add(
        OutboxEvent(
            event_no=new_prefixed_ulid("evt_"),
            event_type=event_type,
            aggregate_type="product",
            aggregate_no=product.product_no,
            aggregate_version=product.version,
            payload={
                "product_id": product.product_no,
                "store_id": store.store_no,
                "status": product.product_status,
            },
            event_status="pending",
            available_at=utc_now(),
            trace_id=_request_id(),
        )
    )


def _product_snapshot(product: Product) -> dict[str, object]:
    return {
        "category_id": product.category_id,
        "brand_id": product.brand_id,
        "product_name": product.product_name,
        "subtitle": product.subtitle,
        "description": product.description,
        "status": product.product_status,
        "version": product.version,
    }


def _editable(product: Product) -> None:
    if product.product_status not in EDITABLE_STATUSES:
        raise _conflict("PRODUCT_NOT_EDITABLE", "当前状态的商品不能修改。")


def _version(actual: int, expected: int) -> None:
    if actual != expected:
        raise ApplicationError(
            status=412,
            code="RESOURCE_VERSION_CONFLICT",
            title="Resource version conflict",
            detail="资源已被其他操作更新，请刷新后重试。",
        )


def _money(amount: int) -> str:
    return f"{amount // 100}.{amount % 100:02d}"


def _request_id() -> str:
    return request_id_context.get() or new_prefixed_ulid("req_")


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="未找到该资源。",
    )


def _invalid(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=422, code=code, title="Invalid product data", detail=detail)


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Product conflict", detail=detail)


def _bad_cursor() -> ApplicationError:
    return ApplicationError(
        status=400,
        code="PAGINATION_CURSOR_INVALID",
        title="Invalid pagination cursor",
        detail="分页位置无效，请重新加载列表。",
    )
