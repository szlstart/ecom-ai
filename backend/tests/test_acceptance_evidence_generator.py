from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load_generator() -> ModuleType:
    path = ROOT / "scripts" / "generate_acceptance_evidence.py"
    spec = importlib.util.spec_from_file_location("generate_acceptance_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_junit_selectors_resolve_parameterized_pytest_and_nested_vitest_names() -> None:
    module = _load_generator()
    backend = module.ET.fromstring(
        '<testcase classname="tests.test_security" '
        'name="test_scope_is_denied[store-a]" />'
    )
    frontend = module.ET.fromstring(
        '<testcase classname="src/router/routes.test.ts" '
        'name="route contract &gt; registers deterministic recovery routes" />'
    )
    browser = module.ET.fromstring(
        '<testcase classname="release-acceptance.spec.ts" '
        'name="HOME-BROWSER renders deterministic content" />'
    )

    assert module.testcase_selector(backend, "backend") == (
        "backend/tests/test_security.py::test_scope_is_denied"
    )
    assert module.testcase_selector(frontend, "frontend") == (
        "frontend/src/router/routes.test.ts::registers deterministic recovery routes"
    )
    assert module.testcase_selector(browser, "browser") == (
        "frontend/e2e/release-acceptance.spec.ts::HOME-BROWSER renders deterministic content"
    )


def test_junit_failure_state_is_never_reported_as_passing() -> None:
    module = _load_generator()
    passing = module.ET.fromstring('<testcase classname="tests.test_a" name="test_a" />')
    failing = module.ET.fromstring(
        '<testcase classname="tests.test_a" name="test_a"><failure /></testcase>'
    )
    errored = module.ET.fromstring(
        '<testcase classname="tests.test_a" name="test_a"><error /></testcase>'
    )
    skipped = module.ET.fromstring(
        '<testcase classname="tests.test_a" name="test_a"><skipped /></testcase>'
    )

    assert module.testcase_status(passing) == "passed"
    assert module.testcase_status(failing) == "failed"
    assert module.testcase_status(errored) == "error"
    assert module.testcase_status(skipped) == "skipped"
