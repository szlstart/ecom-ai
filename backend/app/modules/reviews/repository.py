from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Select, exists, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import CursorPosition
from app.modules.catalog.models import Product, ProductSku
from app.modules.files.models import FileObject
from app.modules.identity.models import User
from app.modules.orders.models import Order, OrderItem
from app.modules.reviews.models import (
    Review,
    ReviewAppendImage,
    ReviewAppendRecord,
    ReviewGovernanceRecord,
    ReviewImage,
    ReviewReply,
)
from app.modules.stores.models import Store


class ReviewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def public_product(self, product_no: str) -> Product | None:
        return cast(
            Product | None,
            await self.session.scalar(
                select(Product)
                .join(Store, Store.id == Product.store_id)
                .where(
                    Product.product_no == product_no,
                    Product.product_status == "on_sale",
                    Store.store_status == "active",
                )
            ),
        )

    async def user_order_item(
        self,
        user_id: int,
        order_item_no: str,
        *,
        for_update: bool = False,
    ) -> tuple[OrderItem, Order] | None:
        statement = (
            select(OrderItem, Order)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                OrderItem.order_item_no == order_item_no,
                Order.user_id == user_id,
            )
        )
        if for_update:
            statement = statement.with_for_update(of=OrderItem)
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1]) if row else None

    async def review_for_order_item(self, order_item_id: int) -> Review | None:
        return cast(
            Review | None,
            await self.session.scalar(select(Review).where(Review.order_item_id == order_item_id)),
        )

    async def review_detail(
        self,
        review_no: str,
        *,
        for_update: bool = False,
    ) -> tuple[Review, Order, OrderItem, User, ProductSku] | None:
        statement = (
            select(Review, Order, OrderItem, User, ProductSku)
            .join(Order, Order.id == Review.order_id)
            .join(OrderItem, OrderItem.id == Review.order_item_id)
            .join(User, User.id == Review.user_id)
            .join(ProductSku, ProductSku.id == Review.sku_id)
            .where(Review.review_no == review_no)
        )
        if for_update:
            statement = statement.with_for_update(of=Review)
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1], row[2], row[3], row[4]) if row else None

    async def admin_review_detail(
        self,
        review_no: str,
        *,
        for_update: bool = False,
    ) -> tuple[Review, Order, OrderItem, User, Product, ProductSku, Store] | None:
        statement = (
            select(Review, Order, OrderItem, User, Product, ProductSku, Store)
            .join(Order, Order.id == Review.order_id)
            .join(OrderItem, OrderItem.id == Review.order_item_id)
            .join(User, User.id == Review.user_id)
            .join(Product, Product.id == Review.product_id)
            .join(ProductSku, ProductSku.id == Review.sku_id)
            .join(Store, Store.id == Review.store_id)
            .where(Review.review_no == review_no)
        )
        if for_update:
            statement = statement.with_for_update(of=Review)
        row = (await self.session.execute(statement)).one_or_none()
        return tuple(row) if row is not None else None

    async def admin_reviews(
        self,
        *,
        scopes: Sequence[tuple[str, int]],
        review_status: str | None,
        limit: int,
    ) -> list[tuple[Review, Order, OrderItem, User, Product, ProductSku, Store]]:
        statement = (
            select(Review, Order, OrderItem, User, Product, ProductSku, Store)
            .join(Order, Order.id == Review.order_id)
            .join(OrderItem, OrderItem.id == Review.order_item_id)
            .join(User, User.id == Review.user_id)
            .join(Product, Product.id == Review.product_id)
            .join(ProductSku, ProductSku.id == Review.sku_id)
            .join(Store, Store.id == Review.store_id)
        )
        if ("platform", 0) not in scopes:
            store_ids = [scope_id for scope_type, scope_id in scopes if scope_type == "store"]
            if not store_ids:
                return []
            statement = statement.where(Review.store_id.in_(store_ids))
        if review_status is not None:
            statement = statement.where(Review.review_status == review_status)
        rows = (
            await self.session.execute(
                statement.order_by(Review.created_at.desc(), Review.id.desc()).limit(limit)
            )
        ).all()
        return [tuple(row) for row in rows]

    async def governance_history(self, review_id: int) -> list[ReviewGovernanceRecord]:
        return list(
            (
                await self.session.scalars(
                    select(ReviewGovernanceRecord)
                    .where(ReviewGovernanceRecord.review_id == review_id)
                    .order_by(ReviewGovernanceRecord.created_at, ReviewGovernanceRecord.id)
                )
            ).all()
        )

    async def user_pending_items(
        self,
        *,
        user_id: int,
        completed_since: datetime,
        order_no: str | None,
        position: CursorPosition | None,
        limit: int,
    ) -> tuple[list[tuple[OrderItem, Order]], bool]:
        statement = (
            select(OrderItem, Order)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.user_id == user_id,
                Order.order_status == "completed",
                Order.fulfillment_status == "received",
                Order.completed_at.is_not(None),
                Order.completed_at >= completed_since,
                OrderItem.review_status == "pending",
                ~exists().where(Review.order_item_id == OrderItem.id),
            )
        )
        if order_no is not None:
            statement = statement.where(Order.order_no == order_no)
        statement, reverse = _owned_cursor(
            statement,
            timestamp_column=Order.completed_at,
            id_column=OrderItem.id,
            position=position,
        )
        rows = list((await self.session.execute(statement.limit(limit + 1))).all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        if reverse:
            rows.reverse()
        return [(row[0], row[1]) for row in rows], has_more

    async def user_reviews(
        self,
        *,
        user_id: int,
        order_no: str | None,
        position: CursorPosition | None,
        limit: int,
    ) -> tuple[list[tuple[Review, Order, OrderItem]], bool]:
        statement = (
            select(Review, Order, OrderItem)
            .join(Order, Order.id == Review.order_id)
            .join(OrderItem, OrderItem.id == Review.order_item_id)
            .where(Review.user_id == user_id)
        )
        if order_no is not None:
            statement = statement.where(Order.order_no == order_no)
        statement, reverse = _owned_cursor(
            statement,
            timestamp_column=Review.created_at,
            id_column=Review.id,
            position=position,
        )
        rows = list((await self.session.execute(statement.limit(limit + 1))).all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        if reverse:
            rows.reverse()
        return [(row[0], row[1], row[2]) for row in rows], has_more

    async def review_files(
        self,
        file_nos: list[str],
        *,
        for_update: bool = False,
    ) -> list[FileObject]:
        if not file_nos:
            return []
        statement = (
            select(FileObject).where(FileObject.file_no.in_(file_nos)).order_by(FileObject.id)
        )
        if for_update:
            statement = statement.with_for_update()
        return list((await self.session.scalars(statement)).all())

    async def owner_images(self, review_id: int) -> list[tuple[ReviewImage, FileObject]]:
        rows = (
            await self.session.execute(
                select(ReviewImage, FileObject)
                .join(FileObject, FileObject.object_key == ReviewImage.object_key)
                .where(
                    ReviewImage.review_id == review_id,
                    ReviewImage.image_status == "active",
                    FileObject.file_status == "active",
                )
                .order_by(ReviewImage.sort_order, ReviewImage.id)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def owner_images_many(
        self, review_ids: Sequence[int]
    ) -> dict[int, list[tuple[ReviewImage, FileObject]]]:
        if not review_ids:
            return {}
        rows = (
            await self.session.execute(
                select(ReviewImage, FileObject)
                .join(FileObject, FileObject.object_key == ReviewImage.object_key)
                .where(
                    ReviewImage.review_id.in_(review_ids),
                    ReviewImage.image_status == "active",
                    FileObject.file_status == "active",
                )
                .order_by(ReviewImage.review_id, ReviewImage.sort_order, ReviewImage.id)
            )
        ).all()
        result: dict[int, list[tuple[ReviewImage, FileObject]]] = {}
        for image, file in rows:
            result.setdefault(image.review_id, []).append((image, file))
        return result

    async def append_for_review(
        self,
        review_id: int,
        *,
        for_update: bool = False,
    ) -> ReviewAppendRecord | None:
        statement = select(ReviewAppendRecord).where(ReviewAppendRecord.review_id == review_id)
        if for_update:
            statement = statement.with_for_update()
        return cast(ReviewAppendRecord | None, await self.session.scalar(statement))

    async def owner_appends(self, review_ids: Sequence[int]) -> dict[int, ReviewAppendRecord]:
        if not review_ids:
            return {}
        rows = list(
            (
                await self.session.scalars(
                    select(ReviewAppendRecord).where(ReviewAppendRecord.review_id.in_(review_ids))
                )
            ).all()
        )
        return {item.review_id: item for item in rows}

    async def owner_append_images(
        self, append_record_id: int
    ) -> list[tuple[ReviewAppendImage, FileObject]]:
        rows = (
            await self.session.execute(
                select(ReviewAppendImage, FileObject)
                .join(FileObject, FileObject.object_key == ReviewAppendImage.object_key)
                .where(
                    ReviewAppendImage.append_record_id == append_record_id,
                    ReviewAppendImage.image_status == "active",
                    FileObject.file_status == "active",
                )
                .order_by(ReviewAppendImage.sort_order, ReviewAppendImage.id)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def owner_append_images_many(
        self, append_record_ids: Sequence[int]
    ) -> dict[int, list[tuple[ReviewAppendImage, FileObject]]]:
        if not append_record_ids:
            return {}
        rows = (
            await self.session.execute(
                select(ReviewAppendImage, FileObject)
                .join(FileObject, FileObject.object_key == ReviewAppendImage.object_key)
                .where(
                    ReviewAppendImage.append_record_id.in_(append_record_ids),
                    ReviewAppendImage.image_status == "active",
                    FileObject.file_status == "active",
                )
                .order_by(
                    ReviewAppendImage.append_record_id,
                    ReviewAppendImage.sort_order,
                    ReviewAppendImage.id,
                )
            )
        ).all()
        result: dict[int, list[tuple[ReviewAppendImage, FileObject]]] = {}
        for image, file in rows:
            result.setdefault(image.append_record_id, []).append((image, file))
        return result

    async def reply_for_review(self, review_id: int) -> ReviewReply | None:
        return cast(
            ReviewReply | None,
            await self.session.scalar(
                select(ReviewReply).where(
                    ReviewReply.review_id == review_id,
                    ReviewReply.reply_status == "published",
                )
            ),
        )

    async def rating_distribution(self, product_id: int) -> tuple[dict[int, int], int]:
        rows = (
            await self.session.execute(
                select(Review.rating, func.count(Review.id))
                .where(
                    Review.product_id == product_id,
                    Review.review_status == "published",
                    Review.published_at.is_not(None),
                )
                .group_by(Review.rating)
            )
        ).all()
        image_count = await self.session.scalar(
            select(func.count(func.distinct(Review.id))).where(
                Review.product_id == product_id,
                Review.review_status == "published",
                Review.published_at.is_not(None),
                exists().where(
                    ReviewImage.review_id == Review.id,
                    ReviewImage.scan_status == "safe",
                    ReviewImage.image_status == "active",
                ),
            )
        )
        return {int(rating): int(count) for rating, count in rows}, int(image_count or 0)

    async def public_reviews(
        self,
        *,
        product_id: int,
        rating: int | None,
        has_image: bool | None,
        sku_no: str | None,
        sort: str,
        position: CursorPosition | None,
        limit: int,
    ) -> tuple[list[tuple[Review, User, ProductSku]], bool]:
        statement = (
            select(Review, User, ProductSku)
            .join(User, User.id == Review.user_id)
            .join(ProductSku, ProductSku.id == Review.sku_id)
            .where(
                Review.product_id == product_id,
                Review.review_status == "published",
                Review.published_at.is_not(None),
            )
        )
        if rating is not None:
            statement = statement.where(Review.rating == rating)
        if sku_no is not None:
            statement = statement.where(ProductSku.sku_no == sku_no)
        image_exists = exists().where(
            ReviewImage.review_id == Review.id,
            ReviewImage.scan_status == "safe",
            ReviewImage.image_status == "active",
        )
        if has_image is True:
            statement = statement.where(image_exists)
        elif has_image is False:
            statement = statement.where(~image_exists)
        statement, reverse = _review_cursor(statement, sort, position)
        rows = list((await self.session.execute(statement.limit(limit + 1))).all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        if reverse:
            rows.reverse()
        return [(row[0], row[1], row[2]) for row in rows], has_more

    async def images(
        self, review_ids: Sequence[int]
    ) -> dict[int, list[tuple[ReviewImage, FileObject]]]:
        if not review_ids:
            return {}
        rows = (
            await self.session.execute(
                select(ReviewImage, FileObject)
                .join(FileObject, FileObject.object_key == ReviewImage.object_key)
                .where(
                    ReviewImage.review_id.in_(review_ids),
                    ReviewImage.scan_status == "safe",
                    ReviewImage.image_status == "active",
                    FileObject.visibility == "public_derivative",
                    FileObject.scan_status == "safe",
                    FileObject.file_status == "active",
                )
                .order_by(ReviewImage.review_id, ReviewImage.sort_order, ReviewImage.id)
            )
        ).all()
        result: dict[int, list[tuple[ReviewImage, FileObject]]] = {}
        for image, file in rows:
            result.setdefault(image.review_id, []).append((image, file))
        return result

    async def appends(self, review_ids: Sequence[int]) -> dict[int, ReviewAppendRecord]:
        if not review_ids:
            return {}
        rows = list(
            (
                await self.session.scalars(
                    select(ReviewAppendRecord).where(
                        ReviewAppendRecord.review_id.in_(review_ids),
                        ReviewAppendRecord.append_status == "published",
                        ReviewAppendRecord.moderation_status == "passed",
                    )
                )
            ).all()
        )
        return {item.review_id: item for item in rows}

    async def append_images(
        self, append_record_ids: Sequence[int]
    ) -> dict[int, list[tuple[ReviewAppendImage, FileObject]]]:
        if not append_record_ids:
            return {}
        rows = (
            await self.session.execute(
                select(ReviewAppendImage, FileObject)
                .join(FileObject, FileObject.object_key == ReviewAppendImage.object_key)
                .where(
                    ReviewAppendImage.append_record_id.in_(append_record_ids),
                    ReviewAppendImage.scan_status == "safe",
                    ReviewAppendImage.image_status == "active",
                    FileObject.visibility == "public_derivative",
                    FileObject.scan_status == "safe",
                    FileObject.file_status == "active",
                )
                .order_by(
                    ReviewAppendImage.append_record_id,
                    ReviewAppendImage.sort_order,
                    ReviewAppendImage.id,
                )
            )
        ).all()
        result: dict[int, list[tuple[ReviewAppendImage, FileObject]]] = {}
        for image, file in rows:
            result.setdefault(image.append_record_id, []).append((image, file))
        return result

    async def replies(self, review_ids: Sequence[int]) -> dict[int, ReviewReply]:
        if not review_ids:
            return {}
        rows = list(
            (
                await self.session.scalars(
                    select(ReviewReply).where(
                        ReviewReply.review_id.in_(review_ids),
                        ReviewReply.reply_status == "published",
                    )
                )
            ).all()
        )
        return {item.review_id: item for item in rows}


def _review_cursor(
    statement: Select[tuple[Review, User, ProductSku]],
    sort: str,
    position: CursorPosition | None,
) -> tuple[Select[tuple[Review, User, ProductSku]], bool]:
    reverse = position is not None and position.direction == "previous"
    descending = sort == "newest"
    if reverse:
        descending = not descending
    if position is not None:
        if len(position.values) != 2:
            raise ValueError("review cursor must contain two values")
        timestamp = datetime.fromisoformat(position.values[0])
        review_id = int(position.values[1])
        key = tuple_(Review.published_at, Review.id)
        statement = statement.where(
            key < (timestamp, review_id) if descending else key > (timestamp, review_id)
        )
    published_order = Review.published_at.desc() if descending else Review.published_at.asc()
    id_order = Review.id.desc() if descending else Review.id.asc()
    return statement.order_by(published_order, id_order), reverse


def _owned_cursor(
    statement: Select[Any],
    *,
    timestamp_column: Any,
    id_column: Any,
    position: CursorPosition | None,
) -> tuple[Select[Any], bool]:
    reverse = position is not None and position.direction == "previous"
    descending = not reverse
    if position is not None:
        if len(position.values) != 2:
            raise ValueError("review cursor must contain two values")
        timestamp = datetime.fromisoformat(position.values[0])
        row_id = int(position.values[1])
        key = tuple_(timestamp_column, id_column)
        statement = statement.where(
            key < (timestamp, row_id) if descending else key > (timestamp, row_id)
        )
    time_order = timestamp_column.desc() if descending else timestamp_column.asc()
    id_order = id_column.desc() if descending else id_column.asc()
    return statement.order_by(time_order, id_order), reverse
