from app.main import create_app


def test_user_logistics_operations_and_role_specific_dtos_are_published() -> None:
    schema = create_app().openapi()
    expected = {
        ("/api/v1/orders/{order_id}/shipments", "get"): "Shipment_ListMine",
        ("/api/v1/shipments/{shipment_id}", "get"): "Shipment_GetMine",
        ("/api/v1/shipments/{shipment_id}/tracks", "get"): "ShipmentTrack_ListMine",
        ("/api/v1/shipments/{shipment_id}/refreshes", "post"): "ShipmentRefresh_Create",
        ("/api/v1/admin/orders/{order_id}/shipments", "post"): "AdminShipment_Create",
        ("/api/v1/admin/shipments/{shipment_id}", "get"): "AdminShipment_Get",
    }
    for (path, method), operation_id in expected.items():
        assert schema["paths"][path][method]["operationId"] == operation_id

    schemas = schema["components"]["schemas"]
    summary_fields = schemas["UserOrderShipmentSummary"]["properties"]
    detail_fields = schemas["UserShipmentDetail"]["properties"]
    admin_fields = schemas["AdminShipmentDetail"]["properties"]
    create_fields = schemas["AdminShipmentCreateRequest"]["properties"]
    assert "tracking_no" not in summary_fields
    assert "tracking_no_masked" in summary_fields
    assert "tracking_no" in detail_fields
    assert "tracking_no" not in admin_fields
    assert create_fields["tracking_no"]["writeOnly"] is True
