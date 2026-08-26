from __future__ import annotations

import base64
import json
from datetime import timedelta
from decimal import Decimal
from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PaginationMeta
from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.pagination import CursorCodec, CursorPosition
from app.core.security import utc_now
from app.modules.catalog.models import (
    Product,
    ProductContentVersion,
    ProductFaqVersion,
    ProductFavorite,
    ProductImage,
)
from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.schemas import (
    BrandView,
    CategoryView,
    HomepageSection,
    HomepageView,
    Money,
    ProductCard,
    ProductDetail,
    ProductFaqList,
    ProductFaqView,
    ProductList,
    ProductSkuList,
    ProductSkuView,
    PublicImage,
    SafeContent,
    SearchSuggestionList,
    ServiceEstimate,
    StoreSummary,
)
from app.modules.content.service import ContentService
from app.modules.files.models import FileObject
from app.modules.inventory.models import Inventory
from app.modules.stores.models import Store

ProductSort = Literal["relevance", "sales", "newest", "price_asc", "price_desc"]
StockStatus = Literal["in_stock", "low_stock", "out_of_stock", "frozen"]


class CatalogService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.repository = CatalogRepository(session)
        self.cursor = CursorCodec(settings.security_hmac_secret.get_secret_value())

    async def search(
        self,
        *,
        user_id: int | None,
        q: str | None,
        category_no: str | None,
        brand_no: str | None,
        store_no: str | None,
        group_no: str | None = None,
        price_min: int | None,
        price_max: int | None,
        sort: ProductSort,
        cursor: str | None,
        limit: int,
    ) -> tuple[ProductList, PaginationMeta]:
        q = q.strip() if q else None
        if price_min is not None and price_max is not None and price_min > price_max:
            raise ApplicationError(
                status=422,
                code="PRICE_RANGE_INVALID",
                title="Invalid price range",
                detail="最低价格不能高于最高价格。",
            )
        filter_key = _filter_key(
            q=q,
            category_no=category_no,
            brand_no=brand_no,
            store_no=store_no,
            group_no=group_no,
            price_min=price_min,
            price_max=price_max,
            sort=sort,
        )
        position = self.cursor.decode(cursor, filter_key=filter_key)
        try:
            rows, has_more = await self.repository.search_products(
                q=q,
                category_no=category_no,
                brand_no=brand_no,
                store_no=store_no,
                group_no=group_no,
                price_min=price_min,
                price_max=price_max,
                sort=sort,
                position=position,
                limit=limit,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ApplicationError(
                status=400,
                code="PAGINATION_CURSOR_INVALID",
                title="Invalid pagination cursor",
                detail="分页位置无效，请重新加载列表。",
            ) from exc
        cards = await self.product_cards(rows, user_id=user_id)
        return ProductList(items=cards), self._product_pagination(
            rows=rows,
            position=position,
            has_more=has_more,
            filter_key=filter_key,
            sort=sort,
            limit=limit,
        )

    async def product_cards(
        self,
        rows: list[tuple[Product, Store]],
        *,
        user_id: int | None,
    ) -> list[ProductCard]:
        product_ids = [product.id for product, _ in rows]
        images = await self.repository.main_images(product_ids)
        favorites = (
            await self.repository.favorite_product_ids(user_id, product_ids)
            if user_id is not None
            else set()
        )
        return [
            _product_card(
                product,
                store,
                images.get(product.id),
                is_favorited=product.id in favorites,
            )
            for product, store in rows
        ]

    async def product_detail(self, product_no: str, user_id: int | None) -> ProductDetail:
        row = await self.repository.public_product(product_no)
        if row is None:
            raise _not_found()
        product, store = row
        category = await self.repository.category_by_id(product.category_id)
        if category is None:
            raise _not_found()
        store_logo = await self.repository.public_file_by_object_key(store.logo_object_key)
        public_images, _ = await self.repository.product_images(product.id)
        content = await self.repository.published_content(product)
        attributes = await self.repository.product_attributes(product.id)
        fulfillment = await self.repository.fulfillment_profile(product.id)
        is_favorited = bool(
            user_id is not None
            and product.id in await self.repository.favorite_product_ids(user_id, [product.id])
        )
        now = utc_now()
        estimate = ServiceEstimate(
            estimate_type="dispatch",
            status="available" if fulfillment is not None else "unavailable",
            min_at=(now + timedelta(hours=fulfillment.dispatch_min_hours)) if fulfillment else None,
            max_at=(now + timedelta(hours=fulfillment.dispatch_max_hours)) if fulfillment else None,
            source="store_fulfillment_profile" if fulfillment else None,
            source_updated_at=fulfillment.updated_at if fulfillment else None,
            calculated_at=now,
            timezone="Asia/Shanghai",
            disclaimer_code="ESTIMATE_NOT_GUARANTEE" if fulfillment else None,
            unavailable_reason_code=None if fulfillment else "FULFILLMENT_PROFILE_UNAVAILABLE",
        )
        return ProductDetail(
            product_id=product.product_no,
            product_name=product.product_name,
            subtitle=product.subtitle,
            description=product.description,
            product_status=product.product_status,
            category_id=category.category_no,
            brand_id=(await self._brand_no(product.brand_id)),
            store=_store_summary(store, store_logo),
            price_range=(
                _money(product.min_price_amount, product.currency),
                _money(product.max_price_amount, product.currency),
            ),
            sales_count=product.sales_count,
            review_count=product.review_count,
            rating_score=_decimal_string(product.rating_score),
            public_images=[
                _public_image(image, file_object) for image, file_object in public_images
            ],
            default_sku_id=await self._default_sku_no(product),
            detail_content=_safe_content(content) if content else None,
            attributes=[
                {
                    "code": item.attribute_code,
                    "name": item.attribute_name,
                    "value": item.value_text,
                    "unit": item.unit,
                    "searchable": item.is_searchable,
                }
                for item in attributes
            ],
            origin_region_code=fulfillment.origin_region_code if fulfillment else None,
            dispatch_estimate=estimate,
            purchase_notice=fulfillment.purchase_notice if fulfillment else None,
            fulfillment_profile_version=fulfillment.profile_version if fulfillment else None,
            is_favorited=is_favorited,
        )

    async def product_skus(self, product_no: str) -> ProductSkuList:
        row = await self.repository.public_product(product_no)
        if row is None:
            raise _not_found()
        product, _ = row
        _, sku_images = await self.repository.product_images(product.id)
        items: list[ProductSkuView] = []
        for sku, inventory in await self.repository.public_skus(product.id):
            available = _available_quantity(inventory)
            if inventory is None or inventory.inventory_status != "active":
                stock_status: StockStatus = "frozen" if inventory else "out_of_stock"
            elif available <= 0:
                stock_status = "out_of_stock"
            elif available <= 5:
                stock_status = "low_stock"
            else:
                stock_status = "in_stock"
            images = [
                _public_image(image, file_object)
                for image, file_object in sku_images.get(sku.id, [])
            ]
            items.append(
                ProductSkuView(
                    sku_id=sku.sku_no,
                    sku_name=sku.sku_name,
                    spec_values=sku.spec_values,
                    sale_price=_money(sku.sale_price_amount, sku.currency),
                    market_price=_money(sku.market_price_amount, sku.currency),
                    sku_status=sku.sku_status,
                    stock_status=stock_status,
                    low_stock_remaining=available if stock_status == "low_stock" else None,
                    max_purchase_quantity=min(available, 99),
                    sales_count=inventory.sold_quantity if inventory else 0,
                    images=images,
                    image_fallback="none" if images else "product_public_images",
                )
            )
        return ProductSkuList(items=items)

    async def product_faqs(self, product_no: str) -> ProductFaqList:
        row = await self.repository.public_product(product_no)
        if row is None:
            raise _not_found()
        items = [
            ProductFaqView(
                faq_id=faq.faq_no,
                question=faq.question,
                answer_content=_safe_content(version),
            )
            for faq, version in await self.repository.public_faqs(row[0].id)
        ]
        return ProductFaqList(items=items)

    async def categories(self) -> list[CategoryView]:
        rows = await self.repository.categories()
        icons = await self.repository.public_files_by_object_keys(
            [row.icon_object_key for row in rows]
        )
        views: dict[int, CategoryView] = {}
        for row in rows:
            views[row.id] = CategoryView(
                category_id=row.category_no,
                parent_id=None,
                category_name=row.category_name,
                category_code=row.category_code,
                level=row.level,
                sort_order=row.sort_order,
                icon_url=_file_url(icons.get(row.icon_object_key or "")),
            )
        roots: list[CategoryView] = []
        for row in rows:
            view = views[row.id]
            parent = views.get(row.parent_id) if row.parent_id else None
            if parent is None:
                roots.append(view)
            else:
                view.parent_id = parent.category_id
                parent.children.append(view)
        return roots

    async def brands(self, q: str | None, limit: int) -> list[BrandView]:
        rows = await self.repository.brands(q.strip() if q else None, limit)
        logos = await self.repository.public_files_by_object_keys(
            [row.logo_object_key for row in rows]
        )
        items: list[BrandView] = []
        for row in rows:
            items.append(
                BrandView(
                    brand_id=row.brand_no,
                    brand_name=row.brand_name,
                    logo_url=_file_url(logos.get(row.logo_object_key or "")),
                    description=row.description,
                )
            )
        return items

    async def suggestions(self, q: str, limit: int) -> SearchSuggestionList:
        normalized = q.strip()
        if not normalized:
            return SearchSuggestionList(items=[])
        return SearchSuggestionList(items=await self.repository.suggestions(normalized, limit))

    async def homepage(self, user_id: int | None) -> HomepageView:
        content = ContentService(self.session)
        announcements = await content.published("announcement")
        banners = await content.published("banner")
        products, pagination = await self.search(
            user_id=user_id,
            q=None,
            category_no=None,
            brand_no=None,
            store_no=None,
            group_no=None,
            price_min=None,
            price_max=None,
            sort="relevance",
            cursor=None,
            limit=12,
        )
        return HomepageView(
            feed_version="catalog-v1",
            announcements=[item.model_dump(mode="json") for item in announcements.items],
            banners=[item.model_dump(mode="json") for item in banners.items],
            sections=[
                HomepageSection(
                    section="recommended",
                    title="为你推荐",
                    status="available",
                    items=products.items,
                    next_cursor=pagination.next_cursor,
                )
            ],
        )

    async def set_favorite(self, user_id: int, product_no: str, enabled: bool) -> None:
        row = await self.repository.public_product(product_no)
        if row is None:
            raise _not_found()
        product, _ = row
        favorite = await self.repository.favorite_by_user_product(
            user_id, product.id, for_update=True
        )
        now = utc_now()
        if enabled:
            if favorite is None:
                self.session.add(
                    ProductFavorite(
                        user_id=user_id,
                        product_id=product.id,
                        favorited_at=now,
                        deleted_at=None,
                    )
                )
            elif favorite.deleted_at is not None:
                favorite.deleted_at = None
                favorite.favorited_at = now
                favorite.version += 1
        elif favorite is not None and favorite.deleted_at is None:
            favorite.deleted_at = now
            favorite.version += 1
        await self.session.commit()

    async def favorite_products(self, user_id: int, limit: int) -> ProductList:
        rows = await self.repository.favorite_products(user_id, limit)
        return ProductList(items=await self.product_cards(rows, user_id=user_id))

    async def _brand_no(self, brand_id: int | None) -> str | None:
        brand = await self.repository.brand_by_id(brand_id)
        return brand.brand_no if brand else None

    async def _default_sku_no(self, product: Product) -> str | None:
        if product.default_sku_id is None:
            return None
        for sku, _ in await self.repository.public_skus(product.id):
            if sku.id == product.default_sku_id:
                return sku.sku_no
        return None

    def _product_pagination(
        self,
        *,
        rows: list[tuple[Product, Store]],
        position: CursorPosition | None,
        has_more: bool,
        filter_key: str,
        sort: ProductSort,
        limit: int,
    ) -> PaginationMeta:
        backward = position is not None and position.direction == "previous"
        has_previous = has_more if backward else position is not None
        has_next = position is not None if backward else has_more
        previous_cursor = (
            self.cursor.encode(
                filter_key=filter_key,
                values=_product_cursor_values(rows[0][0], sort),
                direction="previous",
            )
            if rows and has_previous
            else None
        )
        next_cursor = (
            self.cursor.encode(
                filter_key=filter_key,
                values=_product_cursor_values(rows[-1][0], sort),
                direction="next",
            )
            if rows and has_next
            else None
        )
        return PaginationMeta(
            previous_cursor=previous_cursor,
            next_cursor=next_cursor,
            has_previous=has_previous,
            has_next=has_next,
            limit=limit,
        )


def _product_card(
    product: Product,
    store: Store,
    image_row: tuple[ProductImage, FileObject] | None,
    *,
    is_favorited: bool,
) -> ProductCard:
    return ProductCard(
        product_id=product.product_no,
        store_id=store.store_no,
        store_name=store.store_name,
        product_name=product.product_name,
        subtitle=product.subtitle,
        price=_money(product.min_price_amount, product.currency),
        price_range=_money(product.max_price_amount, product.currency)
        if product.max_price_amount != product.min_price_amount
        else None,
        sales_count=product.sales_count,
        rating_score=_decimal_string(product.rating_score),
        main_image=_public_image(*image_row) if image_row else None,
        is_favorited=is_favorited,
    )


def _public_image(image: ProductImage, file_object: FileObject) -> PublicImage:
    return PublicImage(
        file_id=file_object.file_no,
        url=f"/api/v1/files/{file_object.file_no}",
        thumbnail_url=f"/api/v1/files/{file_object.file_no}?variant=thumbnail",
        alt_text=image.alt_text,
        width=image.width,
        height=image.height,
        sort_order=image.sort_order,
    )


def _store_summary(store: Store, logo: FileObject | None) -> StoreSummary:
    return StoreSummary(
        store_id=store.store_no,
        store_name=store.store_name,
        logo_url=_file_url(logo),
        store_status=store.store_status,
        rating_score=_decimal_string(store.rating_score),
    )


def _safe_content(content: ProductContentVersion | ProductFaqVersion) -> SafeContent:
    return SafeContent(
        content_format=cast(
            Literal["structured_v1", "safe_html_v1"], content.public_content_format
        ),
        content_version=content.content_version,
        content_hash=base64.urlsafe_b64encode(content.content_hash).rstrip(b"=").decode(),
        safe_blocks=content.safe_blocks,
        safe_html=content.safe_html,
        safe_text_fallback=content.safe_text,
    )


def _money(amount: int, currency: str) -> Money:
    return Money(minor_units=str(amount), currency=currency)


def _decimal_string(value: Decimal) -> str:
    return format(value, "f")


def _available_quantity(inventory: Inventory | None) -> int:
    if inventory is None or inventory.inventory_status != "active":
        return 0
    return max(
        inventory.on_hand_quantity - inventory.reserved_quantity - inventory.safety_stock_quantity,
        0,
    )


def _filter_key(**filters: object) -> str:
    return json.dumps(filters, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _product_cursor_values(product: Product, sort: ProductSort) -> tuple[str, str]:
    if sort == "newest":
        value = (product.published_at or product.created_at).isoformat()
    elif sort in {"relevance", "sales"}:
        value = str(product.sales_count)
    else:
        value = str(product.min_price_amount)
    return value, str(product.id)


def _file_url(file_object: FileObject | None) -> str | None:
    return f"/api/v1/files/{file_object.file_no}" if file_object else None


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="未找到该资源。",
    )
