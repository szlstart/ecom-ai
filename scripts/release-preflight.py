#!/usr/bin/env python3
"""Fail-closed static release checks without printing secret values."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DIGEST_IMAGE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
REQUIRED = {
    "ECOM_API_IMAGE",
    "ECOM_FRONTEND_IMAGE",
    "ECOM_PUBLIC_ORIGIN",
    "ECOM_ALLOWED_ORIGINS",
    "ECOM_MYSQL_DSN",
    "ECOM_POSTGRES_DSN",
    "ECOM_MYSQL_MIGRATION_DSN",
    "ECOM_POSTGRES_MIGRATION_DSN",
    "ECOM_REDIS_URL",
    "ECOM_AGENT_MODEL_API_URL",
    "ECOM_AGENT_MODEL_API_KEY",
    "ECOM_AGENT_MODEL_NAME",
    "ECOM_ACCESS_TOKEN_SECRET",
    "ECOM_SECURITY_HMAC_SECRET",
    "ECOM_FIELD_ENCRYPTION_KEY",
    "ECOM_OBJECT_STORAGE_ENDPOINT",
    "ECOM_OBJECT_STORAGE_PUBLIC_ENDPOINT",
    "ECOM_OBJECT_STORAGE_ACCESS_KEY",
    "ECOM_OBJECT_STORAGE_SECRET_KEY",
    "ECOM_FILE_SCANNER_HOST",
    "ECOM_OTEL_EXPORTER_OTLP_ENDPOINT",
}
PLACEHOLDERS = ("change-me", "<", ">", "example.com", "localhost", "127.0.0.1")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_no, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid environment entry at line {line_no}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid environment key at line {line_no}")
        values[key] = value.strip().strip("\"'")
    return values


def require_tls(label: str, value: str) -> None:
    parsed = urlparse(value)
    if label == "Redis":
        if parsed.scheme != "rediss":
            raise ValueError("Redis endpoint must use rediss://")
        return
    ssl_values = {entry.lower() for entry in parse_qs(parsed.query).get("ssl", [])}
    accepted = {
        "1",
        "true",
        "required",
        "require",
        "verify_ca",
        "verify_identity",
        "verify-ca",
        "verify-full",
    }
    if not ssl_values.intersection(accepted):
        raise ValueError(f"{label} DSN must explicitly enable TLS")


def validate(values: dict[str, str]) -> None:
    missing = sorted(key for key in REQUIRED if not values.get(key))
    if missing:
        raise ValueError("missing required keys: " + ", ".join(missing))

    for key in ("ECOM_API_IMAGE", "ECOM_FRONTEND_IMAGE"):
        if not DIGEST_IMAGE.fullmatch(values[key]):
            raise ValueError(f"{key} must be pinned by a sha256 digest")

    for key in REQUIRED - {"ECOM_API_IMAGE", "ECOM_FRONTEND_IMAGE"}:
        lowered = values[key].lower()
        if any(marker in lowered for marker in PLACEHOLDERS):
            raise ValueError(f"{key} contains a development or placeholder value")

    https_keys = (
        "ECOM_PUBLIC_ORIGIN",
        "ECOM_OBJECT_STORAGE_ENDPOINT",
        "ECOM_OBJECT_STORAGE_PUBLIC_ENDPOINT",
        "ECOM_OTEL_EXPORTER_OTLP_ENDPOINT",
        "ECOM_AGENT_MODEL_API_URL",
    )
    for key in https_keys:
        if not values[key].startswith("https://"):
            raise ValueError(f"{key} must use HTTPS")
    origins = [
        item.strip()
        for item in values["ECOM_ALLOWED_ORIGINS"].split(",")
        if item.strip()
    ]
    if not origins or any(not item.startswith("https://") for item in origins):
        raise ValueError("ECOM_ALLOWED_ORIGINS must contain explicit HTTPS origins")

    access_secret = values["ECOM_ACCESS_TOKEN_SECRET"]
    hmac_secret = values["ECOM_SECURITY_HMAC_SECRET"]
    if len(access_secret) < 32 or len(hmac_secret) < 32:
        raise ValueError("authentication secrets must contain at least 32 characters")
    require_tls("MySQL", values["ECOM_MYSQL_DSN"])
    require_tls("PostgreSQL", values["ECOM_POSTGRES_DSN"])
    require_tls("MySQL migration", values["ECOM_MYSQL_MIGRATION_DSN"])
    require_tls("PostgreSQL migration", values["ECOM_POSTGRES_MIGRATION_DSN"])
    require_tls("Redis", values["ECOM_REDIS_URL"])


def validate_compose(rendered: str) -> None:
    config = json.loads(rendered)
    services = config.get("services", {})
    expected = {
        "api",
        "frontend",
        "file-worker",
        "batch-worker",
        "order-timeout-worker",
        "payment-reconcile-worker",
        "logistics-sync-worker",
        "admin-approval-worker",
        "realtime-outbox-worker",
        "agent-runtime-worker",
        "knowledge-indexer",
        "ai-memory-cleanup-worker",
        "account-deletion-worker",
    }
    if set(services) != expected:
        raise ValueError(
            "production app profile contains an unexpected or missing service"
        )
    for name, service in services.items():
        if service.get("build"):
            raise ValueError(f"{name} contains a production build directive")
        image = service.get("image", "")
        if not DIGEST_IMAGE.fullmatch(image):
            raise ValueError(f"{name} image is not digest pinned")
        for volume in service.get("volumes", []):
            if volume.get("type") == "bind":
                raise ValueError(f"{name} contains a source bind mount")
        ports = service.get("ports", [])
        if name != "frontend" and ports:
            raise ValueError(f"{name} unexpectedly publishes a port")
        for port in ports:
            if port.get("host_ip") != "127.0.0.1":
                raise ValueError(
                    "frontend must be exposed only to the local TLS ingress"
                )
        environment = service.get("environment", {})
        for forbidden in ("ECOM_MYSQL_MIGRATION_DSN", "ECOM_POSTGRES_MIGRATION_DSN"):
            if forbidden in environment:
                raise ValueError(f"{name} unexpectedly receives a migration credential")
        model_secret = "ECOM_AGENT_MODEL_API_KEY"
        if name == "agent-runtime-worker":
            for required in (
                "ECOM_AGENT_MODEL_REQUIRED",
                "ECOM_AGENT_MODEL_API_URL",
                model_secret,
                "ECOM_AGENT_MODEL_NAME",
            ):
                if required not in environment:
                    raise ValueError(f"agent-runtime-worker is missing {required}")
        elif model_secret in environment:
            raise ValueError(f"{name} unexpectedly receives the Agent model credential")


def sanitized_process_error(
    exc: subprocess.CalledProcessError, values: dict[str, str]
) -> str:
    """Return actionable Compose diagnostics without exposing configured secrets."""

    detail = (exc.stderr or exc.stdout or str(exc)).strip()
    for value in sorted(values.values(), key=len, reverse=True):
        if value:
            detail = detail.replace(value, "[REDACTED]")
    detail = re.sub(r"(?i)(password|secret|token|key)=([^\s&]+)", r"\1=[REDACTED]", detail)
    if len(detail) > 2000:
        detail = detail[:2000] + "... [truncated]"
    return detail or f"Docker Compose exited with status {exc.returncode}"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env_path = Path(os.environ.get("ENV_FILE", root / ".env.production")).resolve()
    if not env_path.is_file():
        print(
            f"release preflight failed: environment file not found: {env_path}",
            file=sys.stderr,
        )
        return 2
    try:
        values = read_env(env_path)
        validate(values)
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(env_path),
                "-f",
                str(root / "compose.yaml"),
                "-f",
                str(root / "compose.production.yaml"),
                "--profile",
                "app",
                "config",
                "--format",
                "json",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **values},
        )
        validate_compose(result.stdout)
    except subprocess.CalledProcessError as exc:
        print(
            "release preflight failed: " + sanitized_process_error(exc, values),
            file=sys.stderr,
        )
        return 2
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        print(f"release preflight failed: {exc}", file=sys.stderr)
        return 2
    print(
        "release preflight passed: immutable images, external TLS dependencies, "
        "and production Compose validated"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
