from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PERMISSION_SOURCE = DOCS / "permission_registry.yaml"
PERMISSION_CATALOG = ROOT / "backend" / "app" / "generated" / "permission_catalog.py"
OPERATION_TRACE_CATALOG = (
    ROOT / "backend" / "app" / "generated" / "operation_trace_catalog.py"
)


def load(name: str) -> dict[str, Any]:
    with (DOCS / name).open(encoding="utf-8") as file:
        value = yaml.safe_load(file)
    if not isinstance(value, dict):
        raise TypeError(f"{name}: root must be a mapping")
    if value.get("status") != "normative" or not isinstance(value.get("version"), int):
        raise ValueError(f"{name}: normative integer version is required")
    return value


def ensure_unique(values: list[str], label: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"{label}: duplicate values {duplicates}")


def validate() -> None:
    ids = load("id_registry.yaml")
    permissions = load("permission_registry.yaml")
    traceability = load("traceability.yaml")
    domains = load("domain_registry.yaml")
    test_evidence = load("test_evidence_registry.yaml")

    prefixes = [item["prefix"] for item in ids["resources"].values()]
    ensure_unique(prefixes, "ID prefixes")

    permission_codes = [item["code"] for item in permissions["permissions"]]
    ensure_unique(permission_codes, "Permission codes")

    routes = traceability["routes"]
    ensure_unique([item["route"] for item in routes], "Vue routes")
    ensure_unique([item["requirement_id"] for item in routes], "Requirement IDs")
    registered_permissions = set(permission_codes)
    for route in routes:
        authorization = route["authorization"]
        values = authorization if isinstance(authorization, list) else [authorization]
        missing = {
            value
            for value in values
            if isinstance(value, str)
            and ":" in value
            and value not in registered_permissions
        }
        if missing:
            raise ValueError(
                f"{route['requirement_id']}: unregistered permissions {missing}"
            )

    for name, aggregate in domains["aggregates"].items():
        states = set(aggregate["states"])
        for transition in aggregate["transitions"]:
            referenced = set(transition["from"]) | {transition["to"]}
            if not referenced <= states:
                raise ValueError(f"{name}/{transition['command']}: unknown state")

    validate_generated_permission_catalog()
    validate_generated_operation_trace_catalog(traceability)

    print(
        "Registry validation passed: "
        f"{len(prefixes)} ID prefixes, {len(permission_codes)} permissions, "
        f"{len(routes)} routes, {len(domains['aggregates'])} aggregates."
        f" {len(test_evidence['families'])} test families are registered."
    )


def validate_generated_permission_catalog() -> None:
    if not PERMISSION_CATALOG.exists():
        raise ValueError("generated permission catalog is missing")
    spec = importlib.util.spec_from_file_location(
        "permission_catalog", PERMISSION_CATALOG
    )
    if spec is None or spec.loader is None:
        raise ValueError("generated permission catalog cannot be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = hashlib.sha256(PERMISSION_SOURCE.read_bytes()).hexdigest()
    if module.SOURCE_SHA256 != expected:
        raise ValueError(
            "generated permission catalog is stale; run scripts/generate_permission_catalog.py"
        )


def validate_generated_operation_trace_catalog(traceability: dict[str, Any]) -> None:
    if not OPERATION_TRACE_CATALOG.exists():
        raise ValueError("generated operation trace catalog is missing")
    spec = importlib.util.spec_from_file_location(
        "operation_trace_catalog", OPERATION_TRACE_CATALOG
    )
    if spec is None or spec.loader is None:
        raise ValueError("generated operation trace catalog cannot be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = hashlib.sha256((DOCS / "traceability.yaml").read_bytes()).hexdigest()
    if module.SOURCE_SHA256 != expected:
        raise ValueError(
            "generated operation trace catalog is stale; "
            "run scripts/generate_operation_trace_catalog.py"
        )
    required = set(traceability["operation_contract"]["openapi_required_extensions"])
    for operation_id, contract in module.OPERATIONS.items():
        missing = required - contract.keys()
        if missing:
            raise ValueError(f"{operation_id}: generated trace extensions missing {missing}")


if __name__ == "__main__":
    try:
        validate()
    except (KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        print(f"Registry validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
