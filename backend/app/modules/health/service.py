from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal

from redis.exceptions import RedisError
from sqlalchemy import case, func, select

from app.core.config import Settings
from app.core.security import utc_now
from app.database.mysql import mysql_session, probe_mysql
from app.database.postgres import probe_postgres
from app.database.redis import get_redis, probe_redis
from app.integrations.object_storage import get_object_storage
from app.modules.agent_runtime.models import AgentDefinition, AgentVersion
from app.modules.health.schemas import DependencyStatus, ReadinessResponse
from app.modules.knowledge.embedding import embedding_provider
from app.modules.system.models import OutboxEvent

Probe = Callable[[], Awaitable[DependencyStatus]]
REQUIRED_AGENT_CODES = frozenset(
    {"exclusive_support", "store_support", "merchant_copilot", "admin_copilot"}
)


async def get_readiness(settings: Settings) -> ReadinessResponse:
    probes: dict[str, Probe] = {
        "mysql": lambda: _database_probe(probe_mysql, settings, required=True),
        "redis": lambda: _database_probe(probe_redis, settings, required=True),
        "postgres": lambda: _database_probe(probe_postgres, settings, required=False),
        "object_storage": lambda: _object_storage_probe(settings),
        "malware_scanner": lambda: _scanner_probe(settings),
        "agent_runtime": lambda: _agent_runtime_status(settings),
        "agent_model": lambda: _agent_model_status(settings),
        "embedding": lambda: _embedding_status(settings),
        "outbox": lambda: _outbox_status(settings),
    }
    if not settings.readiness_checks_enabled:
        return ReadinessResponse(
            status="ready",
            dependencies={name: DependencyStatus(status="skipped") for name in probes},
        )

    results = await asyncio.gather(*(probe() for probe in probes.values()))
    dependencies = dict(zip(probes, results, strict=True))
    if any(item.required and item.status == "down" for item in dependencies.values()):
        overall: Literal["ready", "degraded", "not_ready"] = "not_ready"
    elif any(item.status in {"down", "degraded", "unknown"} for item in dependencies.values()):
        overall = "degraded"
    else:
        overall = "ready"
    return ReadinessResponse(status=overall, dependencies=dependencies)


async def _database_probe(
    probe: Callable[[float], Awaitable[None]], settings: Settings, *, required: bool
) -> DependencyStatus:
    try:
        await probe(settings.dependency_timeout_seconds)
    except Exception:
        return DependencyStatus(status="down", required=required, code="DEPENDENCY_UNAVAILABLE")
    return DependencyStatus(status="up", required=required)


async def _object_storage_probe(settings: Settings) -> DependencyStatus:
    if not settings.object_storage_enabled:
        return DependencyStatus(status="skipped", code="OBJECT_STORAGE_DISABLED")
    try:
        await asyncio.wait_for(
            get_object_storage().probe(), timeout=settings.dependency_timeout_seconds
        )
    except Exception:
        return DependencyStatus(
            status="down", required=False, code="OBJECT_STORAGE_UNAVAILABLE"
        )
    return DependencyStatus(status="up", required=False)


async def _scanner_probe(settings: Settings) -> DependencyStatus:
    if not settings.file_scanner_enabled:
        return DependencyStatus(status="skipped", code="MALWARE_SCANNER_DISABLED")
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(settings.file_scanner_host, settings.file_scanner_port),
            timeout=settings.dependency_timeout_seconds,
        )
        writer.close()
        await writer.wait_closed()
    except Exception:
        return DependencyStatus(
            status="down", required=False, code="MALWARE_SCANNER_UNAVAILABLE"
        )
    return DependencyStatus(status="up", required=False)


async def _agent_model_status(settings: Settings) -> DependencyStatus:
    if settings.agent_model_api_url is None or settings.agent_model_name is None:
        return DependencyStatus(
            status=("down" if settings.agent_model_required else "skipped"),
            required=False,
            code="MODEL_PROVIDER_NOT_CONFIGURED",
        )
    key = f"ecom:{settings.environment}:agent:model-provider-health:v1"
    try:
        cached = await get_redis().get(key)
        payload = json.loads(cached) if cached else None
    except (RedisError, json.JSONDecodeError, TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        return DependencyStatus(
            status=("down" if settings.agent_model_required else "unknown"),
            required=False,
            code="MODEL_PROVIDER_NOT_PROBED",
        )
    provider_status = payload.get("status")
    if provider_status == "available":
        return DependencyStatus(status="up", required=False)
    return DependencyStatus(
        status=("down" if settings.agent_model_required else "degraded"),
        required=False,
        code=str(payload.get("error_code") or "MODEL_PROVIDER_DEGRADED"),
    )


async def _agent_runtime_status(settings: Settings) -> DependencyStatus:
    """Verify that every portal has an active Agent with a published version."""
    try:
        async for session in mysql_session():
            codes = set(
                (
                    await session.scalars(
                        select(AgentDefinition.agent_code)
                        .join(AgentVersion, AgentVersion.agent_id == AgentDefinition.id)
                        .where(
                            AgentDefinition.agent_code.in_(REQUIRED_AGENT_CODES),
                            AgentDefinition.agent_status == "active",
                            AgentVersion.version_status == "published",
                        )
                        .distinct()
                    )
                ).all()
            )
            break
        else:
            raise RuntimeError("MySQL session unavailable")
    except Exception:
        return DependencyStatus(
            status="unknown", required=False, code="AGENT_RUNTIME_STATUS_UNAVAILABLE"
        )
    if missing := REQUIRED_AGENT_CODES - codes:
        return DependencyStatus(
            status=("down" if settings.agent_model_required else "degraded"),
            required=False,
            code=f"AGENT_VERSION_UNAVAILABLE:{','.join(sorted(missing))}",
        )
    return DependencyStatus(status="up", required=False)


async def _embedding_status(settings: Settings) -> DependencyStatus:
    if settings.embedding_api_url is None or settings.embedding_api_key is None:
        return DependencyStatus(status="skipped", code="EMBEDDING_PROVIDER_NOT_CONFIGURED")
    key = f"ecom:{settings.environment}:embedding:provider-health:v1"
    try:
        cached = await get_redis().get(key)
        payload = json.loads(cached) if cached else None
    except (RedisError, json.JSONDecodeError, TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        if payload.get("status") == "available":
            return DependencyStatus(status="up", required=False)
        return DependencyStatus(
            status="degraded",
            required=False,
            code=str(payload.get("error_code") or "EMBEDDING_PROVIDER_DEGRADED"),
        )
    try:
        provider = embedding_provider(settings)
        vectors = await asyncio.wait_for(
            provider.embed(["embedding health probe"]),
            timeout=min(settings.embedding_timeout_seconds, settings.dependency_timeout_seconds),
        )
        if len(vectors) != 1 or len(vectors[0]) != settings.embedding_dimension:
            raise ValueError("embedding dimension mismatch")
    except Exception:
        result = {"status": "unavailable", "error_code": "EMBEDDING_PROVIDER_UNAVAILABLE"}
        try:
            await get_redis().set(key, json.dumps(result), ex=60)
        except RedisError:
            pass
        return DependencyStatus(
            status="degraded", required=False, code="EMBEDDING_PROVIDER_UNAVAILABLE"
        )
    try:
        await get_redis().set(key, json.dumps({"status": "available"}), ex=600)
    except RedisError:
        pass
    return DependencyStatus(status="up", required=False)


async def _outbox_status(settings: Settings) -> DependencyStatus:
    try:
        async for session in mysql_session():
            oldest, failed = (
                await session.execute(
                    select(
                        func.min(
                            case(
                                (
                                    OutboxEvent.event_status == "pending",
                                    OutboxEvent.created_at,
                                ),
                                else_=None,
                            )
                        ),
                        func.sum(
                            case(
                                (OutboxEvent.event_status == "failed", 1),
                                else_=0,
                            )
                        ),
                    )
                )
            ).one()
            break
        else:
            raise RuntimeError("MySQL session unavailable")
    except Exception:
        return DependencyStatus(status="unknown", code="OUTBOX_STATUS_UNAVAILABLE")
    if int(failed or 0) > 0:
        return DependencyStatus(status="degraded", code="OUTBOX_DEAD_LETTERS_PRESENT")
    if isinstance(oldest, datetime):
        age = max(0, int((utc_now() - oldest).total_seconds()))
        if age > settings.readiness_outbox_lag_seconds:
            return DependencyStatus(status="degraded", code="OUTBOX_LAGGING")
    return DependencyStatus(status="up")
