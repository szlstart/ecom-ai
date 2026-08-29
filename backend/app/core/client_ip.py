from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any, cast

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send


class TrustedProxyMiddleware:
    def __init__(self, app: ASGIApp, trusted_proxy_cidrs: str) -> None:
        self.app = app
        self.trusted = _networks(trusted_proxy_cidrs.split(","))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            peer = scope.get("client")
            peer_ip = str(peer[0]) if peer else "unknown"
            forwarded = _header(scope, b"x-forwarded-for")
            client_ip, source = resolve_client_ip(peer_ip, forwarded, self.trusted)
            state = scope.setdefault("state", {})
            state["client_ip"] = client_ip
            state["client_ip_source"] = source
        await self.app(scope, receive, send)


def request_client_ip(request: Request) -> str:
    value: Any = getattr(request.state, "client_ip", None)
    if isinstance(value, str) and value:
        return value
    return request.client.host if request.client is not None else "unknown"


def ip_in_cidrs(address: str, cidrs: str) -> bool:
    parsed = _address(address)
    return parsed is not None and _trusted(parsed, _networks(cidrs.split(",")))


def resolve_client_ip(
    peer_ip: str,
    forwarded_for: str | None,
    trusted: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> tuple[str, str]:
    peer = _address(peer_ip)
    if peer is None:
        return "unknown", "invalid_peer"
    if not _trusted(peer, trusted) or not forwarded_for:
        return peer.compressed, "peer"
    raw_chain = [item.strip() for item in forwarded_for.split(",")]
    if not raw_chain or len(raw_chain) > 16:
        return peer.compressed, "peer_invalid_forwarded_chain"
    chain = [_address(item) for item in raw_chain]
    if any(item is None for item in chain):
        return peer.compressed, "peer_invalid_forwarded_chain"
    resolved = [item for item in chain if item is not None] + [peer]
    for item in reversed(resolved):
        if not _trusted(item, trusted):
            return item.compressed, "trusted_proxy_chain"
    return resolved[0].compressed, "trusted_proxy_chain"


def _networks(
    values: Iterable[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in values:
        candidate = value.strip()
        if candidate:
            networks.append(ipaddress.ip_network(candidate, strict=False))
    return tuple(networks)


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _trusted(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def _header(scope: Scope, name: bytes) -> str | None:
    values = [value for key, value in scope.get("headers", []) if key.lower() == name]
    if len(values) != 1:
        return None
    try:
        return cast(bytes, values[0]).decode("ascii")
    except UnicodeDecodeError:
        return None
