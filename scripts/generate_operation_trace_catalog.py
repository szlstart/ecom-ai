"""Generate packaged, operation-level OpenAPI trace contracts."""

from __future__ import annotations

import hashlib
import os
import pprint
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "traceability.yaml"
TARGET = ROOT / "backend" / "app" / "generated" / "operation_trace_catalog.py"

os.environ.setdefault("ECOM_READINESS_CHECKS_ENABLED", "false")
sys.path.insert(0, str(ROOT / "backend"))

from app.main import create_app


def permission_codes(dependant: Dependant) -> list[str]:
    result: set[str] = set()
    stack = [dependant]
    while stack:
        current = stack.pop()
        call = current.call
        values = getattr(call, "__permission_codes__", ())
        result.update(str(value) for value in values)
        stack.extend(current.dependencies)
    return sorted(result)


def owner_index(traceability: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for route in traceability["routes"]:
        owner = {
            "requirement_id": route["requirement_id"],
            "owner_kind": "vue_route",
            "scope_policy": route["authorization"],
            "test_case_ids": route["tests"],
        }
        for operation_id in route["operations"]:
            if operation_id != "ui_only":
                result[operation_id].append(owner)
    for component in traceability.get("global_components", []):
        owner = {
            "requirement_id": component["requirement_id"],
            "owner_kind": component["owner_kind"],
            "scope_policy": component["authorization"],
            "test_case_ids": component["tests"],
        }
        for operation_id in component["operations"]:
            result[operation_id].append(owner)
    for raw in traceability.get("operation_owners", []):
        owner = {
            "requirement_id": raw["requirement_id"],
            "owner_kind": raw["owner_kind"],
            "scope_policy": raw["authorization"],
            "test_case_ids": raw["tests"],
        }
        result[raw["operation_id"]].append(owner)
    return result


def idempotency_policy(
    operation: dict[str, Any],
    method: str,
    policy_config: dict[str, Any],
) -> str:
    headers = {
        parameter.get("name"): bool(parameter.get("required"))
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "header"
    }
    policies: list[str] = []
    if "Idempotency-Key" in headers:
        policies.append("idempotency_key_required")
    if "If-Match" in headers:
        policies.append("if_match_required_by_domain")
    if policies:
        return "+".join(policies)
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "safe_read"
    if any(owner["owner_kind"] == "webhook" for owner in operation.get("_owners", [])):
        return "provider_event_idempotency"
    operation_id = str(operation["operationId"])
    overrides = policy_config.get("operation_overrides", {})
    override = overrides.get(operation_id) if isinstance(overrides, dict) else None
    if isinstance(override, str) and override:
        return override
    defaults = policy_config.get("default_method_policies", {})
    default = defaults.get(method) if isinstance(defaults, dict) else None
    if isinstance(default, str) and default:
        return default
    raise ValueError(
        f"{operation_id}: {method} write operation has no explicit idempotency/retry policy"
    )


def audit_event(
    operation_id: str,
    method: str,
    owners: list[dict[str, Any]],
    permissions: list[str],
) -> str:
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "access." + operation_id if permissions else "none"
    return "command." + operation_id


def main() -> None:
    source_bytes = SOURCE.read_bytes()
    traceability = yaml.safe_load(source_bytes)
    policy_config = traceability["idempotency_policy"]
    allowed_policies = set(policy_config["allowed_values"])
    owners = owner_index(traceability)
    app = create_app()
    raw_schema = app.openapi()
    routes = {
        route.operation_id: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.operation_id is not None
    }
    contracts: dict[str, dict[str, object]] = {}
    for path_item in raw_schema["paths"].values():
        for method_name, operation in path_item.items():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            operation_id = str(operation["operationId"])
            operation_owners = owners.get(operation_id, [])
            if not operation_owners:
                raise ValueError(f"{operation_id}: operation owner is missing")
            route = routes[operation_id]
            method = method_name.upper()
            requirement_ids = sorted({owner["requirement_id"] for owner in operation_owners})
            owner_kinds = sorted({owner["owner_kind"] for owner in operation_owners})
            scope_policies: list[str] = []
            for owner in operation_owners:
                policy = owner["scope_policy"]
                policy_values = policy if isinstance(policy, list) else [policy]
                for value in policy_values:
                    normalized = str(value)
                    if normalized not in scope_policies:
                        scope_policies.append(normalized)
            test_case_ids = sorted(
                {
                    str(case_id)
                    for owner in operation_owners
                    for case_id in owner["test_case_ids"]
                }
            )
            permissions = permission_codes(route.dependant)
            operation_with_owners = {**operation, "_owners": operation_owners}
            policy = idempotency_policy(operation_with_owners, method, policy_config)
            if policy not in allowed_policies:
                raise ValueError(f"{operation_id}: unknown idempotency policy {policy!r}")
            contracts[operation_id] = {
                "x-requirement-id": requirement_ids,
                "x-owner-kind": owner_kinds,
                "x-permission-codes": permissions,
                "x-scope-policy": scope_policies,
                "x-domain-command": "none" if method in {"GET", "HEAD", "OPTIONS"} else operation_id,
                "x-audit-event": audit_event(
                    operation_id, method, operation_owners, permissions
                ),
                "x-idempotency-policy": policy,
                "x-test-case-ids": test_case_ids,
            }
    overrides = policy_config.get("operation_overrides", {})
    if not isinstance(overrides, dict):
        raise TypeError("idempotency_policy.operation_overrides must be a mapping")
    for operation_id, override in overrides.items():
        contract = contracts.get(str(operation_id))
        if contract is None:
            raise ValueError(f"{operation_id}: stale idempotency policy override")
        if contract["x-idempotency-policy"] != override:
            raise ValueError(
                f"{operation_id}: override {override!r} is shadowed by "
                f"{contract['x-idempotency-policy']!r}; remove or update the override"
            )
    rendered = (
        '"""Generated from docs/traceability.yaml; do not edit manually."""\n\n'
        f'SOURCE_SHA256 = "{hashlib.sha256(source_bytes).hexdigest()}"\n'
        "OPERATIONS: dict[str, dict[str, object]] = "
        f"{pprint.pformat(contracts, width=100, sort_dicts=True)}\n"
    )
    TARGET.write_text(rendered, encoding="utf-8")
    subprocess.run([sys.executable, "-m", "ruff", "format", "--quiet", str(TARGET)], check=True)
    print(f"Generated {len(contracts)} operation trace contracts in {TARGET}")


if __name__ == "__main__":
    main()
