from __future__ import annotations

import argparse
import hashlib
import json
import os
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DEFAULT_ARTIFACT_ROOT = ROOT / "artifacts" / "acceptance" / "current"
OPENAPI_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


@dataclass(frozen=True)
class ExecutedCase:
    selector: str
    status: str
    element: ET.Element


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path}: root must be a mapping")
    return value


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path}: root must be an object")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def family_id(test_case_id: str) -> str:
    if not test_case_id.endswith("-*") or len(test_case_id) <= 2:
        raise ValueError(f"invalid test family reference: {test_case_id}")
    return test_case_id[:-2]


def testcase_status(element: ET.Element) -> str:
    if element.find("failure") is not None:
        return "failed"
    if element.find("error") is not None:
        return "error"
    if element.find("skipped") is not None:
        return "skipped"
    return "passed"


def testcase_selector(element: ET.Element, source: str) -> str | None:
    classname = element.attrib.get("classname", "")
    name = element.attrib.get("name", "")
    if not classname or not name:
        return None
    if source == "backend":
        module_path = classname.replace(".", "/") + ".py"
        function_name = name.split("[", 1)[0]
        return f"backend/{module_path}::{function_name}"
    if source == "frontend":
        test_name = name.rsplit(" > ", 1)[-1]
        return f"frontend/{classname}::{test_name}"
    if source == "browser":
        return f"frontend/e2e/{classname}::{name}"
    raise ValueError(f"unknown JUnit source: {source}")


def collect_executed_cases(paths: list[tuple[Path, str]]) -> dict[str, list[ExecutedCase]]:
    result: dict[str, list[ExecutedCase]] = {}
    for path, source in paths:
        root = ET.parse(path).getroot()
        for testcase in root.iter("testcase"):
            selector = testcase_selector(testcase, source)
            if selector is None:
                continue
            result.setdefault(selector, []).append(
                ExecutedCase(
                    selector=selector,
                    status=testcase_status(testcase),
                    element=deepcopy(testcase),
                )
            )
    return result


def openapi_operations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path, path_item in document.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in OPENAPI_METHODS or not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str):
                continue
            result[operation_id] = {
                "operation_id": operation_id,
                "method": method.upper(),
                "path": path,
                "requirement_ids": operation.get("x-requirement-id", []),
                "owner_kinds": operation.get("x-owner-kind", []),
                "permission_codes": operation.get("x-permission-codes", []),
                "scope_policies": operation.get("x-scope-policy", []),
                "domain_command": operation.get("x-domain-command"),
                "audit_event": operation.get("x-audit-event"),
                "idempotency_policy": operation.get("x-idempotency-policy"),
                "test_case_ids": operation.get("x-test-case-ids", []),
            }
    return result


def requirement_owners(traceability: dict[str, Any]) -> list[dict[str, Any]]:
    owners: list[dict[str, Any]] = []
    for owner_kind, entries in (
        ("vue_route", traceability.get("routes", [])),
        ("global_ui", traceability.get("global_components", [])),
    ):
        for entry in entries:
            owners.append({**entry, "owner_kind": entry.get("owner_kind", owner_kind)})
    for entry in traceability.get("operation_owners", []):
        owners.append(
            {
                **entry,
                "operations": [entry["operation_id"]],
                "owner_kind": entry["owner_kind"],
            }
        )
    return owners


def write_test_report(path: Path, cases: list[ExecutedCase], requirement_id: str) -> None:
    failures = sum(case.status == "failed" for case in cases)
    errors = sum(case.status == "error" for case in cases)
    skipped = sum(case.status == "skipped" for case in cases)
    suite = ET.Element(
        "testsuite",
        {
            "name": requirement_id,
            "tests": str(len(cases)),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
        },
    )
    for case in cases:
        suite.append(case.element)
    suites = ET.Element("testsuites", {"name": f"acceptance evidence {requirement_id}"})
    suites.append(suite)
    ET.indent(suites)
    ET.ElementTree(suites).write(path, encoding="utf-8", xml_declaration=True)


def generate(
    *,
    output_root: Path,
    traceability_path: Path,
    test_registry_path: Path,
    openapi_path: Path,
    trace_report_path: Path,
    backend_junit_path: Path,
    frontend_junit_path: Path,
    browser_junit_path: Path,
    source_commit: str | None,
) -> dict[str, Any]:
    traceability = load_yaml(traceability_path)
    test_registry = load_yaml(test_registry_path)
    openapi = openapi_operations(load_json(openapi_path))
    trace_report = load_json(trace_report_path)
    if trace_report.get("decision") != "pass":
        raise ValueError("traceability audit must pass before evidence can be generated")
    executed = collect_executed_cases(
        [
            (backend_junit_path, "backend"),
            (frontend_junit_path, "frontend"),
            (browser_junit_path, "browser"),
        ]
    )
    families = test_registry.get("families", {})
    if not isinstance(families, dict):
        raise TypeError("test evidence families must be a mapping")

    evidence_root = output_root / "requirements"
    evidence_root.mkdir(parents=True, exist_ok=True)
    requirement_summaries: list[dict[str, Any]] = []
    errors: list[str] = []
    for owner in sorted(requirement_owners(traceability), key=lambda item: item["requirement_id"]):
        requirement_id = owner["requirement_id"]
        operation_ids = owner.get("operations", [])
        operation_snapshots = [openapi[item] for item in operation_ids if item != "ui_only"]
        missing_operations = [
            item for item in operation_ids if item != "ui_only" and item not in openapi
        ]
        test_refs = set(owner.get("tests", []))
        for operation in operation_snapshots:
            test_refs.update(operation["test_case_ids"])
        family_ids = sorted(family_id(item) for item in test_refs)
        selectors: list[str] = []
        layers: set[str] = set()
        for registered_family in family_ids:
            registration = families.get(registered_family)
            if not isinstance(registration, dict):
                errors.append(f"{requirement_id}: unknown test family {registered_family}")
                continue
            selectors.extend(registration.get("selectors", []))
            layers.update(registration.get("layers", []))
        selectors = sorted(set(selectors))
        selected_cases: list[ExecutedCase] = []
        missing_selectors: list[str] = []
        for selector in selectors:
            cases = executed.get(selector, [])
            if not cases:
                missing_selectors.append(selector)
            selected_cases.extend(cases)
        failed_cases = [case.selector for case in selected_cases if case.status != "passed"]
        if missing_operations:
            errors.append(f"{requirement_id}: missing operations {missing_operations}")
        if missing_selectors:
            errors.append(f"{requirement_id}: selectors not executed {missing_selectors}")
        if failed_cases:
            errors.append(f"{requirement_id}: non-passing cases {sorted(set(failed_cases))}")

        requirement_dir = evidence_root / requirement_id
        requirement_dir.mkdir(parents=True, exist_ok=True)
        contract_snapshot = {
            "schema_version": 1,
            "requirement_id": requirement_id,
            "owner_kind": owner["owner_kind"],
            "route": owner.get("route"),
            "component": owner.get("component"),
            "audience": owner.get("audience"),
            "authorization": owner.get("authorization"),
            "domain_rules": owner.get("domain_rules", []),
            "operations": operation_snapshots,
        }
        (requirement_dir / "contract-snapshot.json").write_text(
            json.dumps(contract_snapshot, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        write_test_report(requirement_dir / "test-report.xml", selected_cases, requirement_id)
        execution_binding_status = (
            "pass"
            if not missing_operations and not missing_selectors and not failed_cases
            else "no_go"
        )
        evidence = {
            "schema_version": 1,
            "requirement_id": requirement_id,
            "source_commit": source_commit,
            "evidence_scope": "traceability_and_registered_test_execution",
            "execution_binding_gate": execution_binding_status,
            "test_families": family_ids,
            "test_layers": sorted(layers),
            "exact_selectors": selectors,
            "executed_case_count": len(selected_cases),
            "missing_selectors": missing_selectors,
            "failed_selectors": sorted(set(failed_cases)),
            "artifacts": {
                "contract_snapshot": "contract-snapshot.json",
                "test_report": "test-report.xml",
                "traceability_audit": "../../traceability-audit.json",
            },
            "source_digests": {
                "traceability_audit_sha256": sha256(trace_report_path),
                "backend_junit_sha256": sha256(backend_junit_path),
                "frontend_junit_sha256": sha256(frontend_junit_path),
                "browser_junit_sha256": sha256(browser_junit_path),
                "openapi_sha256": sha256(openapi_path),
            },
            "evidence_safety": {
                "contains_test_names_only": True,
                "contains_request_or_response_bodies": False,
                "contains_credentials_or_tokens": False,
            },
        }
        (requirement_dir / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        requirement_summaries.append(
            {
                "requirement_id": requirement_id,
                "owner_kind": owner["owner_kind"],
                "execution_binding_gate": execution_binding_status,
                "operation_count": len(operation_snapshots),
                "executed_case_count": len(selected_cases),
                "evidence_path": f"requirements/{requirement_id}/evidence.json",
            }
        )

    index = {
        "schema_version": 1,
        "source_commit": source_commit,
        "decision": "pass" if not errors else "no_go",
        "requirement_count": len(requirement_summaries),
        "requirements": requirement_summaries,
        "errors": errors,
    }
    (output_root / "requirement-evidence-index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return index


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate per-requirement acceptance evidence from executed JUnit reports"
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--traceability", type=Path, default=DOCS / "traceability.yaml")
    parser.add_argument(
        "--test-registry", type=Path, default=DOCS / "test_evidence_registry.yaml"
    )
    parser.add_argument("--openapi", type=Path, default=DOCS / "openapi-v1.json")
    parser.add_argument(
        "--trace-report",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT / "traceability-audit.json",
    )
    parser.add_argument(
        "--backend-junit",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT / "quality" / "backend-junit.xml",
    )
    parser.add_argument(
        "--frontend-junit",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT / "quality" / "frontend-junit.xml",
    )
    parser.add_argument(
        "--browser-junit",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT / "quality" / "browser-junit.xml",
    )
    parser.add_argument("--source-commit", default=os.environ.get("GITHUB_SHA"))
    args = parser.parse_args()
    index = generate(
        output_root=args.output_root,
        traceability_path=args.traceability,
        test_registry_path=args.test_registry,
        openapi_path=args.openapi,
        trace_report_path=args.trace_report,
        backend_junit_path=args.backend_junit,
        frontend_junit_path=args.frontend_junit,
        browser_junit_path=args.browser_junit,
        source_commit=args.source_commit,
    )
    print(
        f"Generated {index['requirement_count']} requirement evidence bundles; "
        f"decision={index['decision']}"
    )
    if index["decision"] != "pass":
        for error in index["errors"]:
            print(f"- {error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
