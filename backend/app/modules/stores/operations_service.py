from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_context
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import utc_now
from app.modules.catalog.models import Product
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess
from app.modules.stores.models import (
    ShippingTemplate,
    ShippingTemplateRule,
    Store,
    StoreAnnouncement,
    StoreProductGroup,
)
from app.modules.stores.operations_repository import StoreOperationsRepository
from app.modules.stores.operations_schemas import (
    AdminFeaturedProductSetRequest,
    AdminFeaturedProductView,
    AdminShippingRuleInput,
    AdminShippingTemplateCreateRequest,
    AdminShippingTemplatePublicationRequest,
    AdminShippingTemplateUpdateRequest,
    AdminShippingTemplateView,
    AdminStoreAnnouncementCreateRequest,
    AdminStoreAnnouncementUpdateRequest,
    AdminStoreAnnouncementView,
    AdminStoreProductGroupCreateRequest,
    AdminStoreProductGroupProductsRequest,
    AdminStoreProductGroupUpdateRequest,
    AdminStoreProductGroupView,
)
from app.modules.system.models import OutboxEvent


class StoreOperationsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = StoreOperationsRepository(session)
        self.idempotency = IdempotencyService(session)

    async def groups(self, access: AdminAccess, store_no: str) -> list[AdminStoreProductGroupView]:
        store = await self._store(access, store_no)
        groups = await self.repository.groups(store.id)
        items = await self.repository.group_items([item.id for item in groups])
        return [
            _group_view(group, store.store_no, groups, items.get(group.id, [])) for group in groups
        ]

    async def store_version(self, access: AdminAccess, store_no: str) -> int:
        return (await self._store(access, store_no)).version

    async def create_group(
        self,
        access: AdminAccess,
        store_no: str,
        payload: AdminStoreProductGroupCreateRequest,
        key: str,
    ) -> AdminStoreProductGroupView:
        store = await self._store(access, store_no)
        claim = await self.idempotency.begin(
            scope_key=f"admin:store-group-create:{store.store_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="store_product_group",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.group(store.id, claim.record.resource_no)
            if existing is not None:
                return await self._group_view(store, existing)
        parent = await self._parent(store, payload.parent_group_id)
        group = StoreProductGroup(
            group_no=new_prefixed_ulid("grp_"),
            store_id=store.id,
            parent_id=parent.id if parent else None,
            group_name=payload.group_name.strip(),
            group_name_normalized=_normalized(payload.group_name),
            group_status="active",
            sort_order=payload.sort_order,
        )
        self.session.add(group)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _conflict("STORE_GROUP_ALREADY_EXISTS", "同一层级已存在同名商品分组。") from exc
        record_admin_operation(
            self.session,
            access,
            action="create_store_product_group",
            target_type="store_product_group",
            target_no=group.group_no,
            after={"name": group.group_name, "parent_id": payload.parent_group_id},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(claim, response_status=201, resource_no=group.group_no)
        await self.session.commit()
        return await self._group_view(store, group)

    async def update_group(
        self,
        access: AdminAccess,
        store_no: str,
        group_no: str,
        payload: AdminStoreProductGroupUpdateRequest,
        expected_version: int,
    ) -> AdminStoreProductGroupView:
        store = await self._store(access, store_no)
        group = await self.repository.group(store.id, group_no, for_update=True)
        if group is None:
            raise _not_found()
        _version(group.version, expected_version)
        before = _group_snapshot(group)
        if "parent_group_id" in payload.model_fields_set:
            parent = await self._parent(store, payload.parent_group_id)
            if parent is not None and (
                parent.id == group.id or await self._is_descendant(group, parent)
            ):
                raise _conflict("STORE_GROUP_CYCLE", "商品分组不能形成循环层级。")
            group.parent_id = parent.id if parent else None
        if payload.group_name is not None:
            group.group_name = payload.group_name.strip()
            group.group_name_normalized = _normalized(payload.group_name)
        if payload.status is not None:
            group.group_status = payload.status
        if payload.sort_order is not None:
            group.sort_order = payload.sort_order
        group.version += 1
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _conflict("STORE_GROUP_ALREADY_EXISTS", "同一层级已存在同名商品分组。") from exc
        record_admin_operation(
            self.session,
            access,
            action="update_store_product_group",
            target_type="store_product_group",
            target_no=group.group_no,
            before=before,
            after=_group_snapshot(group),
            scope_type="store",
            scope_id=store.id,
        )
        await self.session.commit()
        return await self._group_view(store, group)

    async def replace_group_products(
        self,
        access: AdminAccess,
        store_no: str,
        group_no: str,
        payload: AdminStoreProductGroupProductsRequest,
        expected_version: int,
    ) -> AdminStoreProductGroupView:
        store = await self._store(access, store_no)
        group = await self.repository.group(store.id, group_no, for_update=True)
        if group is None:
            raise _not_found()
        _version(group.version, expected_version)
        products = await self._ordered_products(store, payload.product_ids, allowed_statuses=None)
        await self.repository.replace_group_items(group, products)
        group.version += 1
        record_admin_operation(
            self.session,
            access,
            action="replace_store_group_products",
            target_type="store_product_group",
            target_no=group.group_no,
            after={"product_ids": payload.product_ids, "version": group.version},
            scope_type="store",
            scope_id=store.id,
        )
        _outbox(
            self.session,
            "store.product_group_changed.v1",
            "store_product_group",
            group.group_no,
            group.version,
            {"store_id": store.store_no, "product_ids": payload.product_ids},
        )
        await self.session.commit()
        return await self._group_view(store, group)

    async def shipping_templates(
        self, access: AdminAccess, store_no: str
    ) -> list[AdminShippingTemplateView]:
        store = await self._store(access, store_no)
        templates = await self.repository.shipping_templates(store.id)
        rules = await self.repository.shipping_rules([item.id for item in templates])
        return [_shipping_view(item, store.store_no, rules.get(item.id, [])) for item in templates]

    async def create_shipping_template(
        self,
        access: AdminAccess,
        store_no: str,
        payload: AdminShippingTemplateCreateRequest,
        key: str,
    ) -> AdminShippingTemplateView:
        store = await self._store(access, store_no, for_update=True)
        claim = await self.idempotency.begin(
            scope_key=f"admin:shipping-template-create:{store.store_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="shipping_template",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.shipping_template(store.id, claim.record.resource_no)
            if existing is not None:
                return await self._shipping_view(store, existing)
        family_no = payload.template_family_id or new_prefixed_ulid("sht_")
        if payload.template_family_id is not None:
            family_rows = [
                item
                for item in await self.repository.shipping_templates(store.id)
                if item.template_family_no == family_no
            ]
            if not family_rows:
                raise _not_found()
            if any(item.template_status == "draft" for item in family_rows):
                raise _conflict("SHIPPING_TEMPLATE_DRAFT_EXISTS", "该模板系列已有草稿版本。")
        template = ShippingTemplate(
            template_no=new_prefixed_ulid("sht_"),
            template_family_no=family_no,
            store_id=store.id,
            template_name=payload.template_name,
            delivery_type=payload.delivery_type,
            charge_mode=payload.charge_mode,
            currency=payload.currency,
            template_status="draft",
            dispatch_min_hours=payload.dispatch_min_hours,
            dispatch_max_hours=payload.dispatch_max_hours,
            policy_version=await self.repository.next_shipping_version(store.id, family_no),
        )
        self.session.add(template)
        await self.session.flush()
        await self.repository.replace_shipping_rules(template, payload.rules)
        record_admin_operation(
            self.session,
            access,
            action="create_shipping_template",
            target_type="shipping_template",
            target_no=template.template_no,
            after={"family_id": family_no, "policy_version": template.policy_version},
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(claim, response_status=201, resource_no=template.template_no)
        await self.session.commit()
        return await self._shipping_view(store, template)

    async def update_shipping_template(
        self,
        access: AdminAccess,
        store_no: str,
        template_no: str,
        payload: AdminShippingTemplateUpdateRequest,
        expected_version: int,
    ) -> AdminShippingTemplateView:
        store = await self._store(access, store_no)
        template = await self.repository.shipping_template(store.id, template_no, for_update=True)
        if template is None:
            raise _not_found()
        _version(template.version, expected_version)
        if template.template_status != "draft":
            raise _conflict("SHIPPING_TEMPLATE_IMMUTABLE", "已发布或停用的运费模板版本不可修改。")
        before = _shipping_snapshot(template)
        for field in ("template_name", "delivery_type", "charge_mode"):
            value = getattr(payload, field)
            if value is not None:
                setattr(template, field, value)
        minimum = (
            payload.dispatch_min_hours
            if payload.dispatch_min_hours is not None
            else template.dispatch_min_hours
        )
        maximum = (
            payload.dispatch_max_hours
            if payload.dispatch_max_hours is not None
            else template.dispatch_max_hours
        )
        if minimum > maximum:
            raise _unprocessable(
                "SHIPPING_DISPATCH_RANGE_INVALID", "最短发货时间不能超过最长发货时间。"
            )
        template.dispatch_min_hours = minimum
        template.dispatch_max_hours = maximum
        if payload.rules is not None:
            await self.repository.replace_shipping_rules(template, payload.rules)
        template.version += 1
        record_admin_operation(
            self.session,
            access,
            action="update_shipping_template",
            target_type="shipping_template",
            target_no=template.template_no,
            before=before,
            after=_shipping_snapshot(template),
            scope_type="store",
            scope_id=store.id,
        )
        await self.session.commit()
        return await self._shipping_view(store, template)

    async def publish_shipping_template(
        self,
        access: AdminAccess,
        store_no: str,
        template_no: str,
        payload: AdminShippingTemplatePublicationRequest,
        expected_version: int,
        key: str,
    ) -> AdminShippingTemplateView:
        store = await self._store(access, store_no)
        claim = await self.idempotency.begin(
            scope_key=f"admin:shipping-template-publish:{template_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="shipping_template",
        )
        template = await self.repository.shipping_template(store.id, template_no, for_update=True)
        if template is None:
            raise _not_found()
        if claim.replayed:
            return await self._shipping_view(store, template)
        _version(template.version, expected_version)
        if template.template_status != "draft":
            raise _conflict("ILLEGAL_STATE_TRANSITION", "只有草稿运费模板可以发布。")
        rules = await self.repository.shipping_rules([template.id])
        if not rules.get(template.id):
            raise _conflict("SHIPPING_TEMPLATE_RULE_REQUIRED", "运费模板至少需要一条有效规则。")
        current = await self.repository.effective_shipping_template(
            store.id, template.template_family_no, for_update=True
        )
        if current is not None:
            current.template_status = "disabled"
            current.version += 1
        template.template_status = "effective"
        template.version += 1
        record_admin_operation(
            self.session,
            access,
            action="publish_shipping_template",
            target_type="shipping_template",
            target_no=template.template_no,
            reason=payload.reason,
            before={"status": "draft"},
            after={"status": "effective", "policy_version": template.policy_version},
            scope_type="store",
            scope_id=store.id,
        )
        _outbox(
            self.session,
            "store.shipping_template_published.v1",
            "shipping_template",
            template.template_no,
            template.version,
            {
                "store_id": store.store_no,
                "template_family_id": template.template_family_no,
                "policy_version": template.policy_version,
                "invalidate_checkout_cache": True,
            },
        )
        self.idempotency.complete(claim, response_status=200, resource_no=template.template_no)
        await self.session.commit()
        return await self._shipping_view(store, template)

    async def announcements(
        self, access: AdminAccess, store_no: str
    ) -> list[AdminStoreAnnouncementView]:
        store = await self._store(access, store_no)
        return [
            _announcement_view(item, store.store_no)
            for item in await self.repository.announcements(store.id)
        ]

    async def create_announcement(
        self,
        access: AdminAccess,
        store_no: str,
        payload: AdminStoreAnnouncementCreateRequest,
        key: str,
    ) -> AdminStoreAnnouncementView:
        store = await self._store(access, store_no)
        claim = await self.idempotency.begin(
            scope_key=f"admin:store-announcement-create:{store.store_no}",
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
            resource_type="store_announcement",
        )
        if claim.replayed and claim.record.resource_no:
            existing = await self.repository.announcement(store.id, claim.record.resource_no)
            if existing is not None:
                return _announcement_view(existing, store.store_no)
        item = StoreAnnouncement(
            announcement_no=new_prefixed_ulid("ann_"),
            store_id=store.id,
            title=payload.title,
            content=payload.content,
            announcement_status=payload.status,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            sort_order=payload.sort_order,
        )
        self.session.add(item)
        await self.session.flush()
        record_admin_operation(
            self.session,
            access,
            action="create_store_announcement",
            target_type="store_announcement",
            target_no=item.announcement_no,
            after=_announcement_snapshot(item),
            scope_type="store",
            scope_id=store.id,
        )
        self.idempotency.complete(claim, response_status=201, resource_no=item.announcement_no)
        await self.session.commit()
        return _announcement_view(item, store.store_no)

    async def update_announcement(
        self,
        access: AdminAccess,
        store_no: str,
        announcement_no: str,
        payload: AdminStoreAnnouncementUpdateRequest,
        expected_version: int,
    ) -> AdminStoreAnnouncementView:
        store = await self._store(access, store_no)
        item = await self.repository.announcement(store.id, announcement_no, for_update=True)
        if item is None:
            raise _not_found()
        _version(item.version, expected_version)
        before = _announcement_snapshot(item)
        for field in ("title", "content", "status", "sort_order"):
            value = getattr(payload, field)
            if value is not None:
                setattr(item, "announcement_status" if field == "status" else field, value)
        if "starts_at" in payload.model_fields_set:
            item.starts_at = payload.starts_at
        if "ends_at" in payload.model_fields_set:
            item.ends_at = payload.ends_at
        if (
            item.starts_at is not None
            and item.ends_at is not None
            and item.ends_at <= item.starts_at
        ):
            raise _unprocessable("ANNOUNCEMENT_WINDOW_INVALID", "公告结束时间必须晚于开始时间。")
        item.version += 1
        record_admin_operation(
            self.session,
            access,
            action="update_store_announcement",
            target_type="store_announcement",
            target_no=item.announcement_no,
            before=before,
            after=_announcement_snapshot(item),
            scope_type="store",
            scope_id=store.id,
        )
        _outbox(
            self.session,
            "store.announcement_changed.v1",
            "store_announcement",
            item.announcement_no,
            item.version,
            {"store_id": store.store_no, "status": item.announcement_status},
        )
        await self.session.commit()
        return _announcement_view(item, store.store_no)

    async def featured(
        self, access: AdminAccess, store_no: str, slot_type: str
    ) -> list[AdminFeaturedProductView]:
        store = await self._store(access, store_no)
        if slot_type not in {"recommended", "hot"}:
            raise _unprocessable("FEATURED_SLOT_INVALID", "未知的精选商品展示位。")
        return [
            AdminFeaturedProductView(
                product_id=product.product_no,
                slot_type=featured.slot_type,
                sort_order=featured.sort_order,
                starts_at=featured.starts_at,
                ends_at=featured.ends_at,
            )
            for featured, product in await self.repository.featured(store.id, slot_type)
        ]

    async def replace_featured(
        self,
        access: AdminAccess,
        store_no: str,
        payload: AdminFeaturedProductSetRequest,
        expected_version: int,
    ) -> list[AdminFeaturedProductView]:
        store = await self._store(access, store_no, for_update=True)
        _version(store.version, expected_version)
        ids = [item.product_id for item in payload.items]
        products = await self._ordered_products(store, ids, allowed_statuses={"on_sale"})
        await self.repository.replace_featured(
            store.id, payload.slot_type, list(zip(products, payload.items, strict=True))
        )
        store.version += 1
        record_admin_operation(
            self.session,
            access,
            action="replace_store_featured_products",
            target_type="store",
            target_no=store.store_no,
            after={"slot_type": payload.slot_type, "product_ids": ids, "version": store.version},
            scope_type="store",
            scope_id=store.id,
        )
        _outbox(
            self.session,
            "store.featured_products_changed.v1",
            "store",
            store.store_no,
            store.version,
            {"slot_type": payload.slot_type, "product_ids": ids},
        )
        await self.session.commit()
        return await self.featured(access, store.store_no, payload.slot_type)

    async def _store(
        self, access: AdminAccess, store_no: str, *, for_update: bool = False
    ) -> Store:
        store = await self.repository.store(store_no, for_update=for_update)
        if store is None:
            raise _not_found()
        access.require_scope("store", store.id)
        return store

    async def _parent(self, store: Store, parent_no: str | None) -> StoreProductGroup | None:
        if parent_no is None:
            return None
        parent = await self.repository.group(store.id, parent_no)
        if parent is None:
            raise _not_found()
        return parent

    async def _is_descendant(self, group: StoreProductGroup, candidate: StoreProductGroup) -> bool:
        groups = {item.id: item for item in await self.repository.groups(group.store_id)}
        current: StoreProductGroup | None = candidate
        while current is not None:
            if current.id == group.id:
                return True
            current = groups.get(current.parent_id) if current.parent_id else None
        return False

    async def _ordered_products(
        self, store: Store, product_nos: list[str], allowed_statuses: set[str] | None
    ) -> list[Product]:
        if len(product_nos) != len(set(product_nos)):
            raise _unprocessable("PRODUCT_LIST_DUPLICATED", "商品列表中不能包含重复商品。")
        products = await self.repository.products(store.id, product_nos)
        by_no = {item.product_no: item for item in products}
        if set(by_no) != set(product_nos):
            raise _unprocessable("STORE_PRODUCT_SCOPE_INVALID", "商品必须全部属于当前店铺。")
        ordered = [by_no[item] for item in product_nos]
        if allowed_statuses is not None and any(
            item.product_status not in allowed_statuses for item in ordered
        ):
            raise _conflict("FEATURED_PRODUCT_NOT_ON_SALE", "精选商品必须处于在售状态。")
        return ordered

    async def _group_view(
        self, store: Store, group: StoreProductGroup
    ) -> AdminStoreProductGroupView:
        groups = await self.repository.groups(store.id)
        items = await self.repository.group_items([group.id])
        return _group_view(group, store.store_no, groups, items.get(group.id, []))

    async def _shipping_view(
        self, store: Store, template: ShippingTemplate
    ) -> AdminShippingTemplateView:
        rules = await self.repository.shipping_rules([template.id])
        return _shipping_view(template, store.store_no, rules.get(template.id, []))


def _group_view(
    group: StoreProductGroup,
    store_no: str,
    groups: list[StoreProductGroup],
    products: list[Product],
) -> AdminStoreProductGroupView:
    by_id = {item.id: item.group_no for item in groups}
    return AdminStoreProductGroupView(
        group_id=group.group_no,
        store_id=store_no,
        parent_group_id=by_id.get(group.parent_id) if group.parent_id else None,
        group_name=group.group_name,
        status=group.group_status,
        sort_order=group.sort_order,
        product_ids=[item.product_no for item in products],
        version=group.version,
    )


def _shipping_view(
    template: ShippingTemplate,
    store_no: str,
    rules: list[ShippingTemplateRule],
) -> AdminShippingTemplateView:
    return AdminShippingTemplateView(
        template_id=template.template_no,
        template_family_id=template.template_family_no,
        store_id=store_no,
        template_name=template.template_name,
        delivery_type=template.delivery_type,
        charge_mode=template.charge_mode,
        currency=template.currency,
        status=template.template_status,
        dispatch_min_hours=template.dispatch_min_hours,
        dispatch_max_hours=template.dispatch_max_hours,
        policy_version=template.policy_version,
        rules=[
            AdminShippingRuleInput(
                region_scope=item.region_scope,
                first_unit=item.first_unit,
                additional_unit=item.additional_unit,
                first_fee_amount=item.first_fee_amount,
                additional_fee_amount=item.additional_fee_amount,
                estimated_min_days=item.estimated_min_days,
                estimated_max_days=item.estimated_max_days,
            )
            for item in rules
        ],
        version=template.version,
    )


def _announcement_view(item: StoreAnnouncement, store_no: str) -> AdminStoreAnnouncementView:
    return AdminStoreAnnouncementView(
        announcement_id=item.announcement_no,
        store_id=store_no,
        title=item.title,
        content=item.content,
        status=item.announcement_status,
        starts_at=item.starts_at,
        ends_at=item.ends_at,
        sort_order=item.sort_order,
        version=item.version,
    )


def _group_snapshot(group: StoreProductGroup) -> dict[str, object]:
    return {
        "name": group.group_name,
        "parent_id": group.parent_id,
        "status": group.group_status,
        "sort_order": group.sort_order,
        "version": group.version,
    }


def _shipping_snapshot(template: ShippingTemplate) -> dict[str, object]:
    return {
        "name": template.template_name,
        "delivery_type": template.delivery_type,
        "charge_mode": template.charge_mode,
        "dispatch_min_hours": template.dispatch_min_hours,
        "dispatch_max_hours": template.dispatch_max_hours,
        "status": template.template_status,
        "version": template.version,
    }


def _announcement_snapshot(item: StoreAnnouncement) -> dict[str, object]:
    return {
        "title": item.title,
        "status": item.announcement_status,
        "starts_at": item.starts_at.isoformat() if item.starts_at else None,
        "ends_at": item.ends_at.isoformat() if item.ends_at else None,
        "sort_order": item.sort_order,
        "version": item.version,
    }


def _outbox(
    session: AsyncSession,
    event_type: str,
    aggregate_type: str,
    aggregate_no: str,
    aggregate_version: int,
    payload: dict[str, object],
) -> None:
    request_id = request_id_context.get() or new_prefixed_ulid("req_")
    session.add(
        OutboxEvent(
            event_no=new_prefixed_ulid("evt_"),
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_no=aggregate_no,
            aggregate_version=aggregate_version,
            payload=payload,
            event_status="pending",
            available_at=utc_now(),
            trace_id=request_id,
        )
    )


def _normalized(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _version(actual: int, expected: int) -> None:
    if actual != expected:
        raise ApplicationError(
            status=412,
            code="RESOURCE_VERSION_CONFLICT",
            title="Resource version conflict",
            detail="资源已被其他操作更新，请刷新后重试。",
        )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="未找到该资源。",
    )


def _conflict(code: str, detail: str) -> ApplicationError:
    return ApplicationError(status=409, code=code, title="Resource conflict", detail=detail)


def _unprocessable(code: str, detail: str) -> ApplicationError:
    return ApplicationError(
        status=422, code=code, title="Request cannot be processed", detail=detail
    )
