from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from app.core.exceptions import ApplicationError
from app.modules.payments.domain import PAYMENT_TRANSITIONS, require_payment_transition


def _registry() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "docs" / "domain_registry.yaml"
    with path.open(encoding="utf-8") as registry_file:
        value = yaml.safe_load(registry_file)
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def test_payment_transitions_match_the_normative_registry() -> None:
    expected = {
        item["command"]: (frozenset(item["from"]), item["to"])
        for item in _registry()["aggregates"]["payment"]["transitions"]
    }
    assert PAYMENT_TRANSITIONS == expected


def test_every_unregistered_payment_transition_is_rejected() -> None:
    states = {
        state for sources, target in PAYMENT_TRANSITIONS.values() for state in (*sources, target)
    }
    for command, (sources, target) in PAYMENT_TRANSITIONS.items():
        for state in states:
            if state in sources:
                assert require_payment_transition(state, command) == target
            else:
                with pytest.raises(ApplicationError) as error:
                    require_payment_transition(state, command)
                assert error.value.code == "PAYMENT_STATE_CONFLICT"
