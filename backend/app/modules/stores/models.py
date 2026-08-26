from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import BIGINT, BINARY, DECIMAL, INTEGER, MEDIUMTEXT, VARBINARY
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import (
    AppendOnlyMySQLModel,
    MutableMySQLModel,
    MySQLBase,
    SoftDeleteMySQLModel,
)


class Store(MutableMySQLModel, MySQLBase):
    __tablename__ = "stores"
    __table_args__ = (
        UniqueConstraint("store_no", name="uk_stores_store_no"),
        UniqueConstraint("store_name_normalized", name="uk_stores_name_normalized"),
        Index("idx_stores_owner", "owner_user_id"),
        Index("idx_stores_status_created", "store_status", "created_at", "id"),
    )

    store_no: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    store_name: Mapped[str] = mapped_column(String(128), nullable=False)
    store_name_normalized: Mapped[str] = mapped_column(String(128), nullable=False)
    logo_object_key: Mapped[str | None] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(String(2000))
    store_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    service_phone_ciphertext: Mapped[bytes | None] = mapped_column(VARBINARY(512))
    rating_score: Mapped[Decimal] = mapped_column(
        DECIMAL(3, 2), nullable=False, default=0, server_default="0"
    )
    rating_count: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    follower_count: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    sales_count: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), nullable=False, default=0, server_default="0"
    )
    store_name_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class StoreCertification(MutableMySQLModel, MySQLBase):
    __tablename__ = "store_certifications"
    __table_args__ = (
        UniqueConstraint("certification_no", name="uk_store_certifications_no"),
        Index(
            "idx_store_cert_store_type",
            "store_id",
            "certification_type",
            "review_status",
        ),
        Index("idx_store_cert_expiry", "review_status", "valid_until"),
    )

    certification_no: Mapped[str] = mapped_column(String(40), nullable=False)
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    certification_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_name_ciphertext: Mapped[bytes] = mapped_column(VARBINARY(1024), nullable=False)
    certificate_no_ciphertext: Mapped[bytes] = mapped_column(VARBINARY(1024), nullable=False)
    certificate_no_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    current_material_version: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), nullable=False, default=1, server_default="1"
    )
    evidence_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    reviewed_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    decision_reason_code: Mapped[str | None] = mapped_column(String(64))
    decision_reason: Mapped[str | None] = mapped_column(String(1000))
    resubmitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)


class StoreCategory(MutableMySQLModel, MySQLBase):
    __tablename__ = "store_categories"
    __table_args__ = (
        UniqueConstraint("store_id", "category_id", name="uk_store_categories_store_category"),
    )

    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("categories.id"), nullable=False
    )
    approval_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    approved_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class StoreFollow(SoftDeleteMySQLModel, MySQLBase):
    __tablename__ = "store_follows"
    __table_args__ = (
        UniqueConstraint("store_id", "active_user_id", name="uk_store_follows_active_user"),
        Index("idx_store_follows_user", "user_id", "deleted_at", "followed_at"),
    )

    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    followed_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    active_user_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        Computed("CASE WHEN deleted_at IS NULL THEN user_id ELSE NULL END"),
    )


class StoreServicePolicy(MutableMySQLModel, MySQLBase):
    __tablename__ = "store_service_policies"
    __table_args__ = (
        UniqueConstraint("policy_no", name="uk_store_service_policies_no"),
        UniqueConstraint(
            "store_id", "policy_type", "policy_version", name="uk_store_policy_family_version"
        ),
        Index(
            "idx_store_policy_public",
            "store_id",
            "policy_type",
            "policy_status",
            "effective_at",
            "expires_at",
        ),
    )

    policy_no: Mapped[str] = mapped_column(String(40), nullable=False)
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    policy_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    content_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    policy_version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    policy_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    supersedes_policy_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("store_service_policies.id")
    )
    created_by: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False
    )
    published_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    withdrawn_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))


class StoreProductGroup(MutableMySQLModel, MySQLBase):
    __tablename__ = "store_product_groups"
    __table_args__ = (
        UniqueConstraint("group_no", name="uk_store_product_groups_no"),
        UniqueConstraint(
            "store_id",
            "active_parent_scope_id",
            "active_group_name_normalized",
            name="uk_store_product_groups_active_name",
        ),
        Index(
            "idx_store_groups_navigation",
            "store_id",
            "group_status",
            "parent_id",
            "sort_order",
            "id",
        ),
    )

    group_no: Mapped[str] = mapped_column(String(40), nullable=False)
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    parent_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("store_product_groups.id")
    )
    group_name: Mapped[str] = mapped_column(String(64), nullable=False)
    group_name_normalized: Mapped[str] = mapped_column(String(64), nullable=False)
    group_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    active_parent_scope_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        Computed("CASE WHEN group_status = 'active' THEN COALESCE(parent_id, 0) ELSE NULL END"),
    )
    active_group_name_normalized: Mapped[str | None] = mapped_column(
        String(64),
        Computed("CASE WHEN group_status = 'active' THEN group_name_normalized ELSE NULL END"),
    )


class StoreProductGroupItem(MutableMySQLModel, MySQLBase):
    __tablename__ = "store_product_group_items"
    __table_args__ = (
        UniqueConstraint(
            "store_product_group_id", "product_id", name="uk_store_group_items_product"
        ),
        Index("idx_store_group_items_order", "store_product_group_id", "sort_order", "id"),
    )

    store_product_group_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("store_product_groups.id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("products.id"), nullable=False
    )
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")


class ShippingTemplate(MutableMySQLModel, MySQLBase):
    __tablename__ = "shipping_templates"
    __table_args__ = (
        UniqueConstraint("template_no", name="uk_shipping_templates_no"),
        UniqueConstraint(
            "template_family_no", "policy_version", name="uk_shipping_template_family_version"
        ),
        UniqueConstraint(
            "store_id", "current_template_family_no", name="uk_shipping_template_current_family"
        ),
        CheckConstraint("dispatch_min_hours <= dispatch_max_hours", name="shipping_dispatch_range"),
    )

    template_no: Mapped[str] = mapped_column(String(40), nullable=False)
    template_family_no: Mapped[str] = mapped_column(String(40), nullable=False)
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    template_name: Mapped[str] = mapped_column(String(128), nullable=False)
    delivery_type: Mapped[str] = mapped_column(String(32), nullable=False)
    charge_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    template_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    dispatch_min_hours: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    dispatch_max_hours: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    policy_version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    current_template_family_no: Mapped[str | None] = mapped_column(
        String(40),
        Computed("CASE WHEN template_status = 'effective' THEN template_family_no ELSE NULL END"),
    )


class ShippingTemplateRule(MutableMySQLModel, MySQLBase):
    __tablename__ = "shipping_template_rules"
    __table_args__ = (
        CheckConstraint(
            "estimated_min_days IS NULL OR estimated_min_days <= estimated_max_days",
            name="shipping_estimate_range",
        ),
        Index("idx_shipping_rules_template", "shipping_template_id", "rule_status", "id"),
    )

    shipping_template_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("shipping_templates.id"), nullable=False
    )
    region_scope: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    first_unit: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    additional_unit: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    first_fee_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    additional_fee_amount: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    estimated_min_days: Mapped[int | None] = mapped_column(SmallInteger)
    estimated_max_days: Mapped[int | None] = mapped_column(SmallInteger)
    rule_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class StoreAnnouncement(MutableMySQLModel, MySQLBase):
    __tablename__ = "store_announcements"
    __table_args__ = (
        UniqueConstraint("announcement_no", name="uk_store_announcements_no"),
        Index(
            "idx_store_announcements_public",
            "store_id",
            "announcement_status",
            "starts_at",
            "ends_at",
            "sort_order",
        ),
    )

    announcement_no: Mapped[str] = mapped_column(String(40), nullable=False)
    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(String(2000), nullable=False)
    announcement_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")


class StoreFeaturedProduct(MutableMySQLModel, MySQLBase):
    __tablename__ = "store_featured_products"
    __table_args__ = (
        UniqueConstraint(
            "store_id", "product_id", "slot_type", name="uk_store_featured_product_slot"
        ),
        Index("idx_store_featured_active", "store_id", "slot_type", "starts_at", "ends_at"),
    )

    store_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("stores.id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("products.id"), nullable=False
    )
    slot_type: Mapped[str] = mapped_column(String(16), nullable=False)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class StoreCertificationEvent(AppendOnlyMySQLModel, MySQLBase):
    __tablename__ = "store_certification_events"
    __table_args__ = (
        UniqueConstraint("event_no", name="uk_store_certification_events_no"),
        UniqueConstraint(
            "certification_id",
            "material_version",
            "event_type",
            name="uk_store_cert_event_material_type",
        ),
        Index("idx_store_cert_events_cert", "certification_id", "created_at", "id"),
    )

    event_no: Mapped[str] = mapped_column(String(40), nullable=False)
    certification_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("store_certifications.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    material_version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    evidence_file_ids: Mapped[list[str] | None] = mapped_column(JSON)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(1000))
    required_materials: Mapped[list[dict[str, object]] | None] = mapped_column(JSON)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("users.id"))
    certification_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
