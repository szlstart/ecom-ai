import asyncio
from collections.abc import Awaitable, Callable

from app.core.config import Settings
from app.database.mysql import probe_mysql
from app.database.postgres import probe_postgres
from app.database.redis import probe_redis
from app.modules.health.schemas import DependencyStatus, ReadinessResponse

Probe = Callable[[float], Awaitable[None]]


async def get_readiness(settings: Settings) -> ReadinessResponse:
    if not settings.readiness_checks_enabled:
        return ReadinessResponse(
            status="ready",
            dependencies={
                "mysql": DependencyStatus(status="skipped"),
                "postgres": DependencyStatus(status="skipped"),
                "redis": DependencyStatus(status="skipped"),
            },
        )

    probes: dict[str, Probe] = {
        "mysql": probe_mysql,
        "postgres": probe_postgres,
        "redis": probe_redis,
    }
    results = await asyncio.gather(
        *(probe(settings.dependency_timeout_seconds) for probe in probes.values()),
        return_exceptions=True,
    )
    dependencies = {
        name: DependencyStatus(status="down" if isinstance(result, BaseException) else "up")
        for name, result in zip(probes, results, strict=True)
    }
    overall = "ready" if all(item.status == "up" for item in dependencies.values()) else "not_ready"
    return ReadinessResponse(status=overall, dependencies=dependencies)
