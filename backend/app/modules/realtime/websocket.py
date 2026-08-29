from __future__ import annotations

import asyncio
import json
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from fastapi import WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.database.mysql import mysql_session
from app.database.redis import get_redis
from app.modules.identity.access_policy import load_identity_eligibility
from app.modules.identity.models import AuthSession, User
from app.modules.rbac.repository import RbacRepository
from app.modules.realtime.channels import (
    admin_platform_channel,
    admin_store_channel,
    admin_user_channel,
    user_channel,
)
from app.modules.realtime.tickets import RealtimeTicket, RealtimeTicketService

PROTOCOL = "ecom.realtime.v1"
_TICKET_PREFIX = "ticket."


@dataclass(frozen=True)
class RealtimePrincipal:
    ticket: RealtimeTicket
    channels: tuple[str, ...]
    connection_expires_at: datetime


class _SlowConsumerError(Exception):
    pass


class _RealtimeDependencyError(Exception):
    pass


class _ProtocolError(Exception):
    pass


async def realtime_websocket(websocket: WebSocket) -> None:
    settings = get_settings()
    if not _origin_allowed(websocket, settings):
        await websocket.close(code=4403, reason="origin rejected")
        return
    raw_ticket = _ticket_from_subprotocols(websocket)
    if raw_ticket is None:
        await websocket.close(code=4401, reason="realtime ticket required")
        return
    redis = get_redis()
    try:
        ticket = await RealtimeTicketService(redis, settings).consume(raw_ticket)
    except Exception:
        await websocket.close(code=1013, reason="realtime temporarily unavailable")
        return
    if ticket is None:
        await websocket.close(code=4401, reason="realtime ticket invalid")
        return
    principal = await _principal(ticket, settings)
    if principal is None:
        await websocket.close(code=4401, reason="session unavailable")
        return

    connection_no = new_prefixed_ulid("wsc_")
    await websocket.accept(subprotocol=PROTOCOL)
    try:
        await _register_connection(redis, settings, principal, connection_no)
    except RedisError:
        await websocket.close(code=1013, reason="realtime temporarily unavailable")
        return

    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(
        maxsize=settings.realtime_connection_queue_size
    )
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(*principal.channels)
        await queue.put(
            {
                "schema_version": 1,
                "event_id": new_prefixed_ulid("rte_"),
                "type": "connection.ready",
                "occurred_at": utc_now().isoformat() + "Z",
                "data": {
                    "connection_id": connection_no,
                    "audience": principal.ticket.audience,
                },
            }
        )
        last_pong = [time.monotonic()]
        tasks = {
            asyncio.create_task(_send_frames(websocket, queue)),
            asyncio.create_task(_receive_frames(websocket, settings, last_pong)),
            asyncio.create_task(_listen_pubsub(pubsub, queue)),
            asyncio.create_task(
                _heartbeat(
                    queue,
                    redis,
                    settings,
                    principal,
                    connection_no,
                    last_pong,
                )
            ),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            error = task.exception()
            if isinstance(error, _SlowConsumerError):
                await _safe_close(websocket, 1013, "slow consumer")
            elif isinstance(error, _RealtimeDependencyError):
                await _safe_close(websocket, 1013, "realtime temporarily unavailable")
            elif isinstance(error, _ProtocolError):
                await _safe_close(websocket, 1008, "invalid client frame")
            elif isinstance(error, WebSocketDisconnect):
                # The peer has already completed (or abandoned) the close handshake.
                # Sending a second close frame raises WebSocketDisconnect with real
                # browsers and turns an ordinary navigation into an ASGI traceback.
                pass
            elif error is not None:
                await _safe_close(websocket, 1011, "realtime connection failed")
    except (RedisError, _RealtimeDependencyError):
        await _safe_close(websocket, 1013, "realtime temporarily unavailable")
    finally:
        await pubsub.aclose()  # type: ignore[no-untyped-call]
        await _unregister_connection(redis, settings, principal, connection_no)


async def _principal(ticket: RealtimeTicket, settings: Settings) -> RealtimePrincipal | None:
    async for session in mysql_session():
        row = (
            await session.execute(
                select(User, AuthSession)
                .join(AuthSession, AuthSession.user_id == User.id)
                .where(
                    User.user_no == ticket.user_no,
                    AuthSession.session_no == ticket.session_no,
                    AuthSession.audience == ticket.audience,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        user, auth_session = row
        now = utc_now()
        if (
            user.user_status != "active"
            or user.permission_version != ticket.permission_version
            or auth_session.revoked_at is not None
            or auth_session.expires_at <= now
        ):
            return None
        eligibility = await load_identity_eligibility(session, user.id, now)
        if not eligibility.allows_session(ticket.audience, auth_session.client_type):
            return None
        try:
            access_expires_at = datetime.fromisoformat(ticket.access_expires_at)
        except ValueError:
            return None
        expires_at = min(access_expires_at, auth_session.expires_at)
        if expires_at <= now:
            return None
        if ticket.audience == "user":
            channels: tuple[str, ...] = (user_channel(settings.environment, user.user_no),)
        else:
            rows = await RbacRepository(session).permissions_for_user(user.id, now)
            scopes = {
                (grant.scope_type, grant.scope_id)
                for permission, grant, _role in rows
                if permission.permission_code == "support:queue_read"
            }
            if not scopes:
                return None
            resolved = {
                admin_user_channel(settings.environment, user.user_no),
                user_channel(settings.environment, user.user_no),
            }
            if ("platform", 0) in scopes:
                resolved.add(admin_platform_channel(settings.environment))
            resolved.update(
                admin_store_channel(settings.environment, scope_id)
                for scope_type, scope_id in scopes
                if scope_type == "store"
            )
            channels = tuple(sorted(resolved))
        return RealtimePrincipal(
            ticket=ticket,
            channels=channels,
            connection_expires_at=expires_at,
        )
    return None


async def _send_frames(websocket: WebSocket, queue: asyncio.Queue[dict[str, object]]) -> None:
    while True:
        await websocket.send_json(await queue.get())


async def _receive_frames(websocket: WebSocket, settings: Settings, last_pong: list[float]) -> None:
    while True:
        raw = await websocket.receive_text()
        if len(raw.encode()) > settings.realtime_max_client_frame_bytes:
            raise _ProtocolError
        try:
            frame = cast(dict[str, object], json.loads(raw))
        except (TypeError, json.JSONDecodeError) as exc:
            raise _ProtocolError from exc
        frame_type = frame.get("type")
        if frame_type == "client.pong":
            last_pong[0] = time.monotonic()
        elif frame_type != "client.ping":
            raise _ProtocolError


async def _listen_pubsub(pubsub: object, queue: asyncio.Queue[dict[str, object]]) -> None:
    while True:
        try:
            message = await pubsub.get_message(timeout=1.0)  # type: ignore[attr-defined]
        except RedisError as exc:
            raise _RealtimeDependencyError from exc
        if not message:
            continue
        raw = message.get("data")
        if not isinstance(raw, str):
            continue
        try:
            frame = cast(dict[str, object], json.loads(raw))
        except (TypeError, json.JSONDecodeError):
            continue
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull as exc:
            raise _SlowConsumerError from exc


async def _heartbeat(
    queue: asyncio.Queue[dict[str, object]],
    redis: Redis,
    settings: Settings,
    principal: RealtimePrincipal,
    connection_no: str,
    last_pong: list[float],
) -> None:
    while True:
        await asyncio.sleep(settings.realtime_heartbeat_seconds)
        if utc_now() >= principal.connection_expires_at:
            raise WebSocketDisconnect(code=4401)
        if time.monotonic() - last_pong[0] > settings.realtime_connection_lease_seconds:
            raise WebSocketDisconnect(code=1001)
        try:
            await _renew_connection(redis, settings, principal, connection_no)
        except RedisError as exc:
            raise _RealtimeDependencyError from exc
        try:
            queue.put_nowait(
                {
                    "schema_version": 1,
                    "event_id": new_prefixed_ulid("rte_"),
                    "type": "server.ping",
                    "occurred_at": utc_now().isoformat() + "Z",
                    "data": {},
                }
            )
        except asyncio.QueueFull as exc:
            raise _SlowConsumerError from exc


async def _register_connection(
    redis: Redis,
    settings: Settings,
    principal: RealtimePrincipal,
    connection_no: str,
) -> None:
    await _renew_connection(redis, settings, principal, connection_no, include_metadata=True)


async def _renew_connection(
    redis: Redis,
    settings: Settings,
    principal: RealtimePrincipal,
    connection_no: str,
    *,
    include_metadata: bool = False,
) -> None:
    now_ms = int(time.time() * 1000)
    expires_ms = now_ms + settings.realtime_connection_lease_seconds * 1000
    connection_key = _connection_key(settings, connection_no)
    user_connections = _user_connections_key(settings, principal.ticket.user_no)
    instance_connections = _instance_connections_key(settings)
    pipeline = redis.pipeline(transaction=True)
    if include_metadata:
        pipeline.hset(
            connection_key,
            mapping={
                "user_no": principal.ticket.user_no,
                "audience": principal.ticket.audience,
                "instance": socket.gethostname(),
                "connected_at": utc_now().isoformat() + "Z",
            },
        )
    pipeline.expire(connection_key, settings.realtime_connection_lease_seconds)
    pipeline.zadd(user_connections, {connection_no: expires_ms})
    pipeline.expire(user_connections, settings.realtime_connection_lease_seconds * 2)
    pipeline.zadd(instance_connections, {connection_no: expires_ms})
    pipeline.expire(instance_connections, settings.realtime_connection_lease_seconds * 2)
    await pipeline.execute()


async def _unregister_connection(
    redis: Redis,
    settings: Settings,
    principal: RealtimePrincipal,
    connection_no: str,
) -> None:
    try:
        pipeline = redis.pipeline(transaction=True)
        pipeline.delete(_connection_key(settings, connection_no))
        pipeline.zrem(_user_connections_key(settings, principal.ticket.user_no), connection_no)
        pipeline.zrem(_instance_connections_key(settings), connection_no)
        await pipeline.execute()
    except RedisError:
        return


def _origin_allowed(websocket: WebSocket, settings: Settings) -> bool:
    origin = websocket.headers.get("origin")
    return origin is not None and origin in settings.cors_origins


def _ticket_from_subprotocols(websocket: WebSocket) -> str | None:
    protocols = websocket.scope.get("subprotocols", [])
    if PROTOCOL not in protocols:
        return None
    for item in protocols:
        if isinstance(item, str) and item.startswith(_TICKET_PREFIX):
            return item.removeprefix(_TICKET_PREFIX)
    return None


def _connection_key(settings: Settings, connection_no: str) -> str:
    return f"ecom:{settings.environment}:ws:connection:{connection_no}:v1"


def _user_connections_key(settings: Settings, user_no: str) -> str:
    return f"ecom:{settings.environment}:ws:user-connections:{user_no}:v1"


def _instance_connections_key(settings: Settings) -> str:
    return f"ecom:{settings.environment}:ws:instance-connections:{socket.gethostname()}:v1"


async def _safe_close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except (RuntimeError, WebSocketDisconnect):
        return
