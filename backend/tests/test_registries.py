from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_DIRECTORY = ROOT / "docs"


def load_registry(name: str) -> dict[str, object]:
    with (REGISTRY_DIRECTORY / name).open(encoding="utf-8") as registry_file:
        value = yaml.safe_load(registry_file)
    assert isinstance(value, dict)
    return value


def test_all_registries_are_normative_and_versioned() -> None:
    for registry_path in sorted(REGISTRY_DIRECTORY.glob("*.yaml")):
        registry = load_registry(registry_path.name)
        assert registry["status"] == "normative"
        assert isinstance(registry["version"], int)


def test_id_prefixes_are_unique() -> None:
    registry = load_registry("id_registry.yaml")
    resources = registry["resources"]
    assert isinstance(resources, dict)
    prefixes = [resource["prefix"] for resource in resources.values()]
    assert len(prefixes) == len(set(prefixes))


def test_traceability_has_unique_routes_and_requirements() -> None:
    registry = load_registry("traceability.yaml")
    routes = registry["routes"]
    assert isinstance(routes, list)
    route_paths = [route["route"] for route in routes]
    requirement_ids = [route["requirement_id"] for route in routes]
    assert len(route_paths) == len(set(route_paths))
    assert len(requirement_ids) == len(set(requirement_ids))


def test_traceability_references_registered_permissions() -> None:
    traceability = load_registry("traceability.yaml")
    permissions = load_registry("permission_registry.yaml")
    permission_codes = {item["code"] for item in permissions["permissions"]}
    referenced: set[str] = set()
    for route in traceability["routes"]:
        authorization = route["authorization"]
        values = authorization if isinstance(authorization, list) else [authorization]
        referenced.update(value for value in values if isinstance(value, str) and ":" in value)
    assert referenced <= permission_codes


def test_domain_transitions_reference_registered_states() -> None:
    registry = load_registry("domain_registry.yaml")
    aggregates = registry["aggregates"]
    assert isinstance(aggregates, dict)
    for aggregate in aggregates.values():
        states = set(aggregate["states"])
        for transition in aggregate["transitions"]:
            assert set(transition["from"]) <= states
            assert transition["to"] in states
