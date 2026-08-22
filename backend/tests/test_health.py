from httpx import AsyncClient


async def test_liveness_returns_request_id(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"].startswith("req_")


async def test_readiness_can_skip_external_dependencies(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert {item["status"] for item in payload["dependencies"].values()} == {"skipped"}


async def test_untrusted_request_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get("/health/live", headers={"x-request-id": "not-safe"})

    assert response.headers["x-request-id"] != "not-safe"
    assert len(response.headers["x-request-id"]) == 30
