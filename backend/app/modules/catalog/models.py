from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY, DECIMAL, INTEGER, MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import (
    AppendOnlyMySQLModel,
    MutableMySQLModel,
    MySQLBase,
    SoftDeleteMySQLModel,
)


class Category(MutableMySQLModel, MySQLBase):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("category_no", name="uk_categories_no"),
        UniqueConstraint("category_code", name="uk_categories_code"),
        Index("idx_categories_parent_sort", "parent_id", "sort_order", "id"),
    )

    category_no: Mapped[str] = mapped_column(String(40), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("categories.id")
    )
    category_name: Mapped[str] = mapped_column(String(64), nullable=False)
    category_code: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    icon_object_key: Mapped[str | None] = mapped_column(String(512))
    category_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class Brand(MutableMySQLModel, MySQLBase):
    __tablename__ = "brands"
    __table_args__ = (
        UniqueConstraint("brand_no", name="uk_brands_no"),
        UniqueConstraint("brand_name_normalized", name="uk_brands_name_normalized"),
    )

    brand_no: Mapped[str] = mapped_column(String(40), nullable=False)
    brand_name: Mapped[str] = mapped_column(String(128), nullable=False)
    brand_name_normalized: Mapped[str] = mapped_column(String(128), nullable=False)
    logo_object_key: Mapped[str | None] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(String(2000))
    brand_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class Product(MutableMySQLModel, MySQLBase):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("product_no", name="uk_products_product_no"),
        Index("idx_products_store_status", "store_id", "product_status", "created_at", "id"),
        Index("idx_products_category_status", "category_id", "product_status", "id"),
    )

    product_no: Mapped[str] = mapped_column(String(40), nullable=False)
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("categories.id"), nullable=False
    )
    brand_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("brands.id"))
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    subtitle: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(String(2000))
    current_detail_content_version_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "product_content_versions.id",
            name="fk_products_current_content_version",
            use_alter=True,
        ),
    )
    published_detail_content_version_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "product_content_versions.id",
            name="fk_products_published_content_version",
            use_alter=True,
        ),
    )
    product_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    default_sku_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey("product_skus.id", name="fk_products_default_sku", use_alter=True),
    )
    min_price_amount: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    max_price_amount: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    sales_count: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    review_count: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    rating_score: Mapped[Decimal] = mapped_column(
        DECIMAL(3, 2), nullable=False, default=0, server_default="0"
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    off_shelf_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class ProductSku(MutableMySQLModel, MySQLBase):
    __tablename__ = "product_skus"
    __table_args__ = (
        UniqueConstraint("sku_no", name="uk_product_skus_no"),
        UniqueConstraint("product_id", "spec_signature", name="uk_product_skus_spec_signature"),
        UniqueConstraint(
            "store_id", "active_merchant_sku_code", name="uk_product_skus_store_merchant_code"
        ),
        CheckConstraint("market_price_amount >= sale_price_amount", name="sku_market_price"),
        Index("idx_skus_product_status", "product_id", "sku_status", "id"),
    )

    sku_no: Mapped[str] = mapped_column(String(40), nullable=False)
    product_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("products.id"), nullable=False
    )
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    merchant_sku_code: Mapped[str | None] = mapped_column(String(64))
    active_merchant_sku_code: Mapped[str | None] = mapped_column(
        String(64),
        Computed("CASE WHEN sku_status = 'active' THEN merchant_sku_code ELSE NULL END"),
    )
    sku_name: Mapped[str] = mapped_column(String(255), nullable=False)
    spec_values: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False)
    spec_signature: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    sale_price_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    market_price_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    weight_grams: Mapped[int | None] = mapped_column(INTEGER(unsigned=True))
    barcode: Mapped[str | None] = mapped_column(String(64))
    sku_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class ProductImage(MutableMySQLModel, MySQLBase):
    __tablename__ = "product_images"
    __table_args__ = (
        UniqueConstraint("product_id", "sku_id", "sort_order", name="uk_product_images_order"),
        UniqueConstraint("active_product_main_scope", name="uk_product_images_active_spu_main"),
        Index(
            "idx_product_images_product_sort",
            "product_id",
            "sku_id",
            "image_type",
            "sort_order",
            "id",
        ),
    )

    product_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("products.id"), nullable=False
    )
    sku_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("product_skus.id"))
    file_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("file_objects.id"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    image_type: Mapped[str] = mapped_column(String(16), nullable=False)
    alt_text: Mapped[str | None] = mapped_column(String(255))
    width: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    height: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    sort_order: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    image_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    active_product_main_scope: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        Computed(
            "CASE WHEN sku_id IS NULL AND image_type = 'main' AND image_status = 'active' "
            "THEN product_id ELSE NULL END"
        ),
    )


class ProductAttribute(MutableMySQLModel, MySQLBase):
    __tablename__ = "product_attributes"
    __table_args__ = (
        UniqueConstraint("product_id", "attribute_code", name="uk_product_attributes_code"),
    )

    product_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("products.id"), nullable=False
    )
    attribute_code: Mapped[str] = mapped_column(String(64), nullable=False)
    attribute_name: Mapped[str] = mapped_column(String(128), nullable=False)
    value_text: Mapped[str] = mapped_column(String(1000), nullable=False)
    value_normalized: Mapped[str | None] = mapped_column(String(500))
    unit: Mapped[str | None] = mapped_column(String(32))
    is_searchable: Mapped[bool] = mapped_column(nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")


class ProductFavorite(SoftDeleteMySQLModel, MySQLBase):
    __tablename__ = "product_favorites"
    __table_args__ = (
        UniqueConstraint("product_id", "active_user_id", name="uk_product_favorites_active_user"),
        Index("idx_product_favorites_user", "user_id", "deleted_at", "favorited_at"),
    )

    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("products.id"), nullable=False
    )
    favorited_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    active_user_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        Computed("CASE WHEN deleted_at IS NULL THEN user_id ELSE NULL END"),
    )


class ProductFaq(MutableMySQLModel, MySQLBase):
    __tablename__ = "product_faqs"
    __table_args__ = (
        UniqueConstraint("faq_no", name="uk_product_faqs_no"),
        Index("idx_product_faqs_product_status", "product_id", "faq_status", "sort_order"),
    )

    faq_no: Mapped[str] = mapped_column(String(40), nullable=False)
    product_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("products.id"), nullable=False
    )
    question: Mapped[str] = mapped_column(String(1000), nullable=False)
    current_content_version_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "product_faq_versions.id", name="fk_product_faqs_current_version", use_alter=True
        ),
    )
    published_content_version_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "product_faq_versions.id",
            name="fk_product_faqs_published_version",
            use_alter=True,
        ),
    )
    faq_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class ProductStatusLog(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "product_status_logs"
    __table_args__ = (Index("idx_product_status_logs_product", "product_id", "created_at", "id"),)

    product_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("products.id"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(500))
    product_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))


class ProductFulfillmentProfile(MutableMySQLModel, MySQLBase):
    __tablename__ = "product_fulfillment_profiles"
    __table_args__ = (
        UniqueConstraint("product_id", name="uk_product_fulfillment_product"),
        CheckConstraint("dispatch_min_hours <= dispatch_max_hours", name="product_dispatch_range"),
    )

    product_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("products.id"), nullable=False
    )
    shipping_template_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("shipping_templates.id"), nullable=False
    )
    origin_region_code: Mapped[str] = mapped_column(String(32), nullable=False)
    dispatch_min_hours: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    dispatch_max_hours: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    purchase_notice: Mapped[str | None] = mapped_column(String(3000))
    profile_version: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=1, server_default="1"
    )


class ProductContentVersion(MutableMySQLModel, MySQLBase):
    __tablename__ = "product_content_versions"
    __table_args__ = (
        UniqueConstraint("content_version_no", name="uk_product_content_versions_no"),
        UniqueConstraint(
            "product_id", "content_version", name="uk_product_content_versions_product_version"
        ),
        CheckConstraint(
            "((public_content_format = 'structured_v1' AND safe_blocks IS NOT NULL "
            "AND safe_html IS NULL) OR (public_content_format = 'safe_html_v1' "
            "AND safe_blocks IS NULL AND safe_html IS NOT NULL))",
            name="product_content_public_payload",
        ),
    )

    content_version_no: Mapped[str] = mapped_column(String(40), nullable=False)
    product_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("products.id"), nullable=False
    )
    content_version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    source_format: Mapped[str] = mapped_column(String(16), nullable=False)
    source_content: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    source_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    public_content_format: Mapped[str] = mapped_column(String(24), nullable=False)
    safe_blocks: Mapped[list[dict[str, object]] | None] = mapped_column(JSON(none_as_null=True))
    safe_html: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    safe_text: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    content_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    sanitizer_policy_version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    content_schema_version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    security_scan_status: Mapped[str] = mapped_column(String(16), nullable=False)
    version_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    created_by: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    approved_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class ProductFaqVersion(MutableMySQLModel, MySQLBase):
    __tablename__ = "product_faq_versions"
    __table_args__ = (
        UniqueConstraint("faq_version_no", name="uk_product_faq_versions_no"),
        UniqueConstraint(
            "product_faq_id", "content_version", name="uk_product_faq_versions_faq_version"
        ),
        CheckConstraint(
            "((public_content_format = 'structured_v1' AND safe_blocks IS NOT NULL "
            "AND safe_html IS NULL) OR (public_content_format = 'safe_html_v1' "
            "AND safe_blocks IS NULL AND safe_html IS NOT NULL))",
            name="product_faq_public_payload",
        ),
    )

    faq_version_no: Mapped[str] = mapped_column(String(40), nullable=False)
    product_faq_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("product_faqs.id"), nullable=False
    )
    content_version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    source_format: Mapped[str] = mapped_column(String(16), nullable=False)
    source_content: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    public_content_format: Mapped[str] = mapped_column(String(24), nullable=False)
    safe_blocks: Mapped[list[dict[str, object]] | None] = mapped_column(JSON(none_as_null=True))
    safe_html: Mapped[str | None] = mapped_column(Text)
    safe_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    sanitizer_policy_version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    content_schema_version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    security_scan_status: Mapped[str] = mapped_column(String(16), nullable=False)
    version_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    created_by: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    approved_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
