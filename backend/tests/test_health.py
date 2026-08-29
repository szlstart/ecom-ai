from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from app.database.mysql import probe_mysql
from app.database.postgres import probe_postgres
from app.modules.health import service as health_service
from app.modules.health.schemas import DependencyStatus


async def test_liveness_returns_request_id(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "0.1.0",
        "build_sha": "development",
    }
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


async def test_metrics_is_visible_only_to_configured_monitoring_networks(
    client: AsyncClient,
) -> None:
    internal = await client.get("/metrics")
    assert internal.status_code == 200
    external = await client.get("/metrics", headers={"X-Forwarded-For": "203.0.113.9"})
    assert external.status_code == 404
    assert "http_requests_total" not in external.text


async def test_optional_dependency_failure_is_degraded_not_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def database_probe(
        probe: Callable[[float], Awaitable[None]],
        settings: Settings,
        *,
        required: bool,
    ) -> DependencyStatus:
        del settings
        status = "down" if probe is probe_postgres else "up"
        return DependencyStatus(status=status, required=required)

    async def skipped(_settings: Settings) -> DependencyStatus:
        return DependencyStatus(status="skipped")

    async def up(_settings: Settings) -> DependencyStatus:
        return DependencyStatus(status="up")

    monkeypatch.setattr(health_service, "_database_probe", database_probe)
    monkeypatch.setattr(health_service, "_object_storage_probe", skipped)
    monkeypatch.setattr(health_service, "_scanner_probe", skipped)
    monkeypatch.setattr(health_service, "_agent_runtime_status", skipped)
    monkeypatch.setattr(health_service, "_agent_model_status", skipped)
    monkeypatch.setattr(health_service, "_embedding_status", skipped)
    monkeypatch.setattr(health_service, "_outbox_status", up)
    result = await health_service.get_readiness(Settings(readiness_checks_enabled=True))
    assert result.status == "degraded"
    assert result.dependencies["postgres"].required is False


async def test_required_dependency_failure_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def database_probe(
        probe: Callable[[float], Awaitable[None]],
        settings: Settings,
        *,
        required: bool,
    ) -> DependencyStatus:
        del settings
        status = "down" if probe is probe_mysql else "up"
        return DependencyStatus(status=status, required=required)

    async def skipped(_settings: Settings) -> DependencyStatus:
        return DependencyStatus(status="skipped")

    monkeypatch.setattr(health_service, "_database_probe", database_probe)
    monkeypatch.setattr(health_service, "_object_storage_probe", skipped)
    monkeypatch.setattr(health_service, "_scanner_probe", skipped)
    monkeypatch.setattr(health_service, "_agent_runtime_status", skipped)
    monkeypatch.setattr(health_service, "_agent_model_status", skipped)
    monkeypatch.setattr(health_service, "_embedding_status", skipped)
    monkeypatch.setattr(health_service, "_outbox_status", skipped)
    result = await health_service.get_readiness(Settings(readiness_checks_enabled=True))
    assert result.status == "not_ready"
    assert result.dependencies["mysql"].required is True


async def test_agent_runtime_requires_published_agent_for_every_portal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ScalarRows:
        def all(self) -> list[str]:
            return ["exclusive_support", "store_support", "merchant_copilot"]

    class Session:
        async def scalars(self, _statement: object) -> ScalarRows:
            return ScalarRows()

    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(health_service, "mysql_session", sessions)
    result = await health_service._agent_runtime_status(
        Settings(
            agent_model_required=True,
            agent_model_api_url="https://model.test/v1/chat/completions",
            agent_model_api_key="test-only-key",
            agent_model_name="test-model",
        )
    )

    assert result.status == "down"
    assert result.code == "AGENT_VERSION_UNAVAILABLE:admin_copilot"
