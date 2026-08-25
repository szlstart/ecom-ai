from __future__ import annotations

import argparse
import ast
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OPENAPI_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    subject: str
    detail: str


def load_yaml(name: str) -> dict[str, Any]:
    value = yaml.safe_load((DOCS / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{name}: root must be a mapping")
    return value


def openapi_operations() -> dict[str, tuple[str, str, dict[str, Any]]]:
    document = json.loads((DOCS / "openapi-v1.json").read_text(encoding="utf-8"))
    result: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method not in OPENAPI_METHODS or not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if isinstance(operation_id, str):
                result[operation_id] = (method.upper(), path, operation)
    return result


def frontend_routes() -> list[tuple[str, str]]:
    source = (ROOT / "frontend/src/router/index.ts").read_text(encoding="utf-8")
    route_pattern = re.compile(r"^\s*\{\s*path:\s*'([^']+)'.*requirementId:\s*'([^']+)'", re.MULTILINE)
    result: list[tuple[str, str]] = []
    parent_path: str | None = None
    for line in source.splitlines():
        match = route_pattern.match(line)
        if match is None:
            continue
        path, requirement_id = match.groups()
        indentation = len(line) - len(line.lstrip())
        if indentation == 2 and line.rstrip().endswith("children: ["):
            parent_path = path
        elif indentation == 2:
            parent_path = None
        normalized_path = path
        if indentation == 4 and parent_path is not None:
            if parent_path == "/":
                normalized_path = "/" + path.lstrip("/")
            else:
                normalized_path = parent_path.rstrip("/") + "/" + path.lstrip("/")
        result.append((normalized_path, requirement_id))
    return result


def component_exists(component: str) -> bool:
    search_roots = (
        ROOT / "frontend/src/pages",
        ROOT / "frontend/src/components",
        ROOT / "frontend/src/layouts",
    )
    return any(any(directory.rglob(component)) for directory in search_roots)


def collected_test_selectors() -> set[str]:
    selectors: set[str] = set()
    for path in sorted((ROOT / "backend/tests").glob("test_*.py")):
        relative = path.relative_to(ROOT).as_posix()
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in module.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                selectors.add(f"{relative}::{node.name}")
    pattern = re.compile(r"\b(?:it|test)\(\s*(['\"])(.*?)\1", re.DOTALL)
    for path in sorted((ROOT / "frontend/src").rglob("*.test.ts")):
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        selectors.update(f"{relative}::{match.group(2)}" for match in pattern.finditer(source))
    return selectors


def test_family_id(value: str) -> str | None:
    if value.endswith("-*") and len(value) > 2:
        return value[:-2]
    return None


def audit() -> dict[str, Any]:
    traceability = load_yaml("traceability.yaml")
    permissions = load_yaml("permission_registry.yaml")
    domains = load_yaml("domain_registry.yaml")
    test_registry = load_yaml("test_evidence_registry.yaml")
    registered_permissions = {item["code"] for item in permissions["permissions"]}
    required_route_fields = set(traceability["required_fields"])
    required_extensions = set(
        traceability["operation_contract"]["openapi_required_extensions"]
    )
    allowed_owner_kinds = set(traceability["operation_contract"]["owner_kinds"])
    routes = traceability["routes"]
    global_components = traceability.get("global_components", [])
    operations = openapi_operations()
    findings: list[Finding] = []
    referenced_test_families: set[str] = set()
    collected_selectors = collected_test_selectors()
    allowed_test_layers = set(test_registry.get("allowed_layers", []))
    registered_test_families = test_registry.get("families", {})
    if not isinstance(registered_test_families, dict):
        registered_test_families = {}
        findings.append(
            Finding(
                "TEST_REGISTRY_INVALID",
                "error",
                "test_evidence_registry.yaml",
                "families must be a mapping",
            )
        )
    for family_id, registration in registered_test_families.items():
        if not isinstance(registration, dict):
            findings.append(
                Finding("TEST_FAMILY_INVALID", "error", str(family_id), "must be a mapping")
            )
            continue
        layers = registration.get("layers")
        selectors = registration.get("selectors")
        if (
            not isinstance(layers, list)
            or not layers
            or any(not isinstance(layer, str) for layer in layers)
            or not set(layers) <= allowed_test_layers
        ):
            findings.append(
                Finding("TEST_FAMILY_LAYERS_INVALID", "error", str(family_id), str(layers))
            )
        if not isinstance(selectors, list) or not selectors:
            findings.append(
                Finding(
                    "TEST_FAMILY_SELECTORS_MISSING",
                    "error",
                    str(family_id),
                    "at least one exact selector is required",
                )
            )
            continue
        for selector in selectors:
            if not isinstance(selector, str) or selector not in collected_selectors:
                findings.append(
                    Finding(
                        "TEST_SELECTOR_NOT_COLLECTED",
                        "error",
                        str(family_id),
                        str(selector),
                    )
                )

    requirement_counts = Counter(item.get("requirement_id") for item in routes)
    path_counts = Counter(item.get("route") for item in routes)
    for requirement_id, count in sorted(requirement_counts.items()):
        if count != 1:
            findings.append(
                Finding("TRACE_DUPLICATE_REQUIREMENT", "error", str(requirement_id), f"count={count}")
            )
    for path, count in sorted(path_counts.items()):
        if count != 1:
            findings.append(Finding("TRACE_DUPLICATE_ROUTE", "error", str(path), f"count={count}"))

    for route in routes:
        requirement_id = str(route.get("requirement_id", "<missing>"))
        missing_fields = sorted(required_route_fields - route.keys())
        if missing_fields:
            findings.append(
                Finding(
                    "TRACE_ROUTE_FIELDS_MISSING",
                    "error",
                    requirement_id,
                    ", ".join(missing_fields),
                )
            )
        component = route.get("component")
        if isinstance(component, str) and not component_exists(component):
            findings.append(
                Finding("TRACE_COMPONENT_MISSING", "error", requirement_id, component)
            )
        authorization = route.get("authorization", [])
        values = authorization if isinstance(authorization, list) else [authorization]
        for value in values:
            if isinstance(value, str) and ":" in value and value not in registered_permissions:
                findings.append(
                    Finding("TRACE_PERMISSION_UNKNOWN", "error", requirement_id, value)
                )
        for test_id in route.get("tests", []):
            family_id = test_family_id(test_id) if isinstance(test_id, str) else None
            if family_id is None or family_id not in registered_test_families:
                findings.append(
                    Finding(
                        "TRACE_TEST_FAMILY_UNKNOWN",
                        "error",
                        requirement_id,
                        str(test_id),
                    )
                )
            else:
                referenced_test_families.add(family_id)

    router_entries = frontend_routes()
    router_requirement_counts = Counter(requirement_id for _, requirement_id in router_entries)
    trace_requirements = set(requirement_counts)
    layout_only = {"ADM-SHELL-01"}
    for requirement_id, count in sorted(router_requirement_counts.items()):
        if count > 1:
            findings.append(
                Finding("ROUTER_REQUIREMENT_DUPLICATE", "error", requirement_id, f"count={count}")
            )
        if requirement_id not in trace_requirements and requirement_id not in layout_only:
            findings.append(
                Finding("ROUTER_REQUIREMENT_UNREGISTERED", "error", requirement_id, "missing from traceability.yaml")
            )
    for requirement_id in sorted(trace_requirements - set(router_requirement_counts)):
        findings.append(
            Finding("TRACE_ROUTE_NOT_IMPLEMENTED", "error", requirement_id, "missing from Vue router")
        )
    trace_path_by_requirement = {
        route["requirement_id"]: route["route"] for route in routes
    }
    for path, requirement_id in router_entries:
        expected_path = trace_path_by_requirement.get(requirement_id)
        if expected_path is not None and path != expected_path:
            findings.append(
                Finding(
                    "ROUTER_PATH_MISMATCH",
                    "error",
                    requirement_id,
                    f"router={path}; traceability={expected_path}",
                )
            )

    owner_rows = [*routes, *global_components]
    for component in global_components:
        for test_id in component.get("tests", []):
            family_id = test_family_id(test_id) if isinstance(test_id, str) else None
            if family_id is None or family_id not in registered_test_families:
                findings.append(
                    Finding(
                        "TRACE_TEST_FAMILY_UNKNOWN",
                        "error",
                        component["requirement_id"],
                        str(test_id),
                    )
                )
            else:
                referenced_test_families.add(family_id)
    owners_by_operation: dict[str, list[str]] = {}
    for owner in owner_rows:
        for operation_id in owner.get("operations", []):
            if operation_id == "ui_only":
                continue
            owners_by_operation.setdefault(operation_id, []).append(owner["requirement_id"])
    for owner in traceability.get("operation_owners", []):
        owners_by_operation.setdefault(owner["operation_id"], []).append(owner["requirement_id"])
        for test_id in owner.get("tests", []):
            family_id = test_family_id(test_id) if isinstance(test_id, str) else None
            if family_id is None or family_id not in registered_test_families:
                findings.append(
                    Finding(
                        "TRACE_TEST_FAMILY_UNKNOWN",
                        "error",
                        owner["requirement_id"],
                        str(test_id),
                    )
                )
            else:
                referenced_test_families.add(family_id)

    for operation_id, owner_ids in sorted(owners_by_operation.items()):
        if operation_id not in operations:
            findings.append(
                Finding(
                    "TRACE_OPERATION_MISSING",
                    "error",
                    operation_id,
                    f"declared by {', '.join(owner_ids)}",
                )
            )
    for operation_id, (method, path, operation) in sorted(operations.items()):
        owner_ids = owners_by_operation.get(operation_id, [])
        if not owner_ids:
            findings.append(
                Finding("OPENAPI_OPERATION_ORPHAN", "error", operation_id, f"{method} {path}")
            )
        missing_extensions = sorted(required_extensions - operation.keys())
        if missing_extensions:
            findings.append(
                Finding(
                    "OPENAPI_TRACE_EXTENSIONS_MISSING",
                    "error",
                    operation_id,
                    ", ".join(missing_extensions),
                )
            )
            continue
        requirement_ids = operation["x-requirement-id"]
        owner_kinds = operation["x-owner-kind"]
        permission_codes = operation["x-permission-codes"]
        scope_policies = operation["x-scope-policy"]
        test_case_ids = operation["x-test-case-ids"]
        list_extensions = {
            "x-requirement-id": requirement_ids,
            "x-owner-kind": owner_kinds,
            "x-permission-codes": permission_codes,
            "x-scope-policy": scope_policies,
            "x-test-case-ids": test_case_ids,
        }
        for extension, value in list_extensions.items():
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item for item in value
            ):
                findings.append(
                    Finding(
                        "OPENAPI_TRACE_EXTENSION_INVALID",
                        "error",
                        operation_id,
                        f"{extension} must be a list of non-empty strings",
                    )
                )
        if isinstance(test_case_ids, list):
            for test_id in test_case_ids:
                family_id = test_family_id(test_id) if isinstance(test_id, str) else None
                if family_id is None or family_id not in registered_test_families:
                    findings.append(
                        Finding(
                            "OPENAPI_TEST_FAMILY_UNKNOWN",
                            "error",
                            operation_id,
                            str(test_id),
                        )
                    )
                else:
                    referenced_test_families.add(family_id)
        if isinstance(requirement_ids, list) and not set(requirement_ids) <= set(owner_ids):
            findings.append(
                Finding(
                    "OPENAPI_REQUIREMENT_OWNER_MISMATCH",
                    "error",
                    operation_id,
                    f"declared={requirement_ids}; owners={owner_ids}",
                )
            )
        if isinstance(owner_kinds, list) and not set(owner_kinds) <= allowed_owner_kinds:
            findings.append(
                Finding(
                    "OPENAPI_OWNER_KIND_UNKNOWN",
                    "error",
                    operation_id,
                    ", ".join(sorted(set(owner_kinds) - allowed_owner_kinds)),
                )
            )
        if isinstance(permission_codes, list) and not set(permission_codes) <= registered_permissions:
            findings.append(
                Finding(
                    "OPENAPI_PERMISSION_UNKNOWN",
                    "error",
                    operation_id,
                    ", ".join(sorted(set(permission_codes) - registered_permissions)),
                )
            )
        for extension in ("x-domain-command", "x-audit-event", "x-idempotency-policy"):
            value = operation[extension]
            if not isinstance(value, str) or not value:
                findings.append(
                    Finding(
                        "OPENAPI_TRACE_EXTENSION_INVALID",
                        "error",
                        operation_id,
                        f"{extension} must be a non-empty string",
                    )
                )

    aggregate_evidence = test_registry.get("domain_aggregates", {})
    if not isinstance(aggregate_evidence, dict):
        aggregate_evidence = {}
    registered_aggregates = set(domains.get("aggregates", {}))
    for aggregate in sorted(registered_aggregates):
        families = aggregate_evidence.get(aggregate)
        if not isinstance(families, list) or not families:
            findings.append(
                Finding(
                    "DOMAIN_TEST_EVIDENCE_MISSING",
                    "error",
                    aggregate,
                    "no registered test family",
                )
            )
            continue
        unknown_families = sorted(
            family for family in families if family not in registered_test_families
        )
        referenced_test_families.update(
            family for family in families if family in registered_test_families
        )
        if unknown_families:
            findings.append(
                Finding(
                    "DOMAIN_TEST_FAMILY_UNKNOWN",
                    "error",
                    aggregate,
                    ", ".join(unknown_families),
                )
            )
    for aggregate in sorted(set(aggregate_evidence) - registered_aggregates):
        findings.append(
            Finding(
                "DOMAIN_TEST_AGGREGATE_UNKNOWN",
                "error",
                aggregate,
                "not present in domain_registry.yaml",
            )
        )
    for family_id in sorted(set(registered_test_families) - referenced_test_families):
        findings.append(
            Finding(
                "TEST_FAMILY_ORPHAN",
                "error",
                family_id,
                "not referenced by traceability, OpenAPI or a domain aggregate",
            )
        )

    counts = Counter(finding.code for finding in findings)
    return {
        "schema_version": 1,
        "decision": "pass" if not findings else "no_go",
        "summary": {
            "trace_routes": len(routes),
            "vue_route_entries": len(router_entries),
            "openapi_operations": len(operations),
            "owned_operations": len(set(operations) & set(owners_by_operation)),
            "finding_count": len(findings),
            "registered_test_families": len(registered_test_families),
            "collected_test_selectors": len(collected_selectors),
            "domain_aggregates_with_evidence": len(aggregate_evidence),
            "finding_counts": dict(sorted(counts.items())),
        },
        "findings": [asdict(finding) for finding in findings],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit section 3.34.19 traceability gates")
    parser.add_argument("--strict", action="store_true", help="return non-zero when findings exist")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts/acceptance/current/traceability-audit.json",
    )
    args = parser.parse_args()
    report = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Decision: {report['decision']}; report: {args.output}")
    if args.strict and report["decision"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
