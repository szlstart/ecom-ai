from typing import cast

from sqlalchemy import ForeignKeyConstraint, String, UniqueConstraint

from app.database.base import MySQLBase
from app.modules.catalog import models as catalog_models  # noqa: F401
from app.modules.files import models as file_models  # noqa: F401
from app.modules.inventory import models as inventory_models  # noqa: F401
from app.modules.reviews import models as review_models  # noqa: F401
from app.modules.stores import models as store_models  # noqa: F401
from app.modules.system import models as system_models  # noqa: F401


def test_phase03_tables_are_registered_in_shared_metadata() -> None:
    expected = {
        "stores",
        "store_certifications",
        "store_certification_events",
        "store_categories",
        "store_follows",
        "store_service_policies",
        "store_product_groups",
        "store_product_group_items",
        "shipping_templates",
        "shipping_template_rules",
        "store_announcements",
        "store_featured_products",
        "categories",
        "brands",
        "products",
        "product_skus",
        "product_images",
        "product_attributes",
        "product_favorites",
        "product_faqs",
        "product_status_logs",
        "product_fulfillment_profiles",
        "product_content_versions",
        "product_faq_versions",
        "inventories",
        "inventory_reservations",
        "inventory_logs",
        "reviews",
        "review_images",
        "review_replies",
        "review_append_records",
        "file_upload_sessions",
        "file_objects",
        "admin_batch_jobs",
        "admin_batch_job_items",
    }

    assert expected <= set(MySQLBase.metadata.tables)
    visibility_type = MySQLBase.metadata.tables["file_objects"].c.visibility.type
    assert isinstance(visibility_type, String)
    assert visibility_type.length == 24
    job_status_type = MySQLBase.metadata.tables["admin_batch_jobs"].c.job_status.type
    assert isinstance(job_status_type, String)
    assert job_status_type.length is not None
    assert job_status_type.length >= len("awaiting_confirmation")


def test_product_cycle_foreign_keys_have_mysql_safe_explicit_names() -> None:
    products = MySQLBase.metadata.tables["products"]
    product_faqs = MySQLBase.metadata.tables["product_faqs"]
    optional_names = {
        constraint.name
        for table in (products, product_faqs)
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }

    expected = {
        "fk_products_current_content_version",
        "fk_products_published_content_version",
        "fk_products_default_sku",
        "fk_product_faqs_current_version",
        "fk_product_faqs_published_version",
    }
    assert None not in optional_names
    names = {cast(str, name) for name in optional_names if name is not None}
    assert expected <= names
    assert all(len(name) <= 64 for name in names)


def test_inventory_and_relationship_uniqueness_are_database_backed() -> None:
    expected_constraints = {
        "inventories": {"uk_inventories_sku"},
        "inventory_reservations": {
            "uk_inventory_reservations_no",
            "uk_inventory_reservations_order_item",
            "uk_inventory_reservations_idempotency",
        },
        "product_skus": {
            "uk_product_skus_no",
            "uk_product_skus_spec_signature",
            "uk_product_skus_store_merchant_code",
        },
        "store_product_group_items": {"uk_store_group_items_product"},
    }

    for table_name, required in expected_constraints.items():
        table = MySQLBase.metadata.tables[table_name]
        actual = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        assert required <= actual
