from app.main import create_app


def test_user_logistics_operations_and_role_specific_dtos_are_published() -> None:
    schema = create_app().openapi()
    expected = {
        ("/api/v1/orders/{order_id}/shipments", "get"): "Shipment_ListMine",
        ("/api/v1/shipments/{shipment_id}", "get"): "Shipment_GetMine",
        ("/api/v1/shipments/{shipment_id}/tracks", "get"): "ShipmentTrack_ListMine",
        ("/api/v1/shipments/{shipment_id}/refreshes", "post"): "ShipmentRefresh_Create",
    }
    for (path, method), operation_id in expected.items():
        assert schema["paths"][path][method]["operationId"] == operation_id

    schemas = schema["components"]["schemas"]
    summary_fields = schemas["UserOrderShipmentSummary"]["properties"]
    detail_fields = schemas["UserShipmentDetail"]["properties"]
    assert "tracking_no" not in summary_fields
    assert "tracking_no_masked" in summary_fields
    assert "tracking_no" in detail_fields
