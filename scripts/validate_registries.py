from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def load(name: str) -> dict[str, Any]:
    with (DOCS / name).open(encoding="utf-8") as file:
        value = yaml.safe_load(file)
    if not isinstance(value, dict):
        raise ValueError(f"{name}: root must be a mapping")
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
            if isinstance(value, str) and ":" in value and value not in registered_permissions
        }
        if missing:
            raise ValueError(f"{route['requirement_id']}: unregistered permissions {missing}")

    for name, aggregate in domains["aggregates"].items():
        states = set(aggregate["states"])
        for transition in aggregate["transitions"]:
            referenced = set(transition["from"]) | {transition["to"]}
            if not referenced <= states:
                raise ValueError(f"{name}/{transition['command']}: unknown state")

    print(
        "Registry validation passed: "
        f"{len(prefixes)} ID prefixes, {len(permission_codes)} permissions, "
        f"{len(routes)} routes, {len(domains['aggregates'])} aggregates."
    )


if __name__ == "__main__":
    try:
        validate()
    except (KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        print(f"Registry validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

