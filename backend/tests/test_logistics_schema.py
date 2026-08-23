from sqlalchemy import CheckConstraint, UniqueConstraint

from app.database.base import MySQLBase
from app.modules.logistics import models as logistics_models  # noqa: F401


def test_logistics_schema_has_privacy_deduplication_and_state_guards() -> None:
    assert {
        "shipments",
        "shipment_items",
        "shipment_tracks",
        "logistics_sync_logs",
    } <= set(MySQLBase.metadata.tables)
    shipments = MySQLBase.metadata.tables["shipments"]
    assert {
        "tracking_no_ciphertext",
        "tracking_no_hash",
        "tracking_no_masked",
    } <= set(shipments.columns.keys())
    assert "tracking_no" not in shipments.columns.keys()
    uniques = {item.name for item in shipments.constraints if isinstance(item, UniqueConstraint)}
    checks = {item.name for item in shipments.constraints if isinstance(item, CheckConstraint)}
    assert {"uk_shipments_no", "uk_shipments_tracking"} <= uniques
    assert {
        "ck_shipments_shipment_status",
        "ck_shipments_shipment_delivery_estimate",
        "ck_shipments_shipment_estimate_source",
    } <= checks

    tracks = MySQLBase.metadata.tables["shipment_tracks"]
    track_uniques = {item.name for item in tracks.constraints if isinstance(item, UniqueConstraint)}
    assert {
        "uk_shipment_tracks_event",
        "uk_shipment_tracks_fallback",
    } <= track_uniques
