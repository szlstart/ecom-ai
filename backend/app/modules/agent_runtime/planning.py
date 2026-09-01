from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ResponseStrategy = Literal["answer", "clarify", "handoff", "refuse"]


class ModelAgentDecision(BaseModel):
    """Strict provider output for one bounded Agent planning decision.

    The model may describe what it believes is needed, but the server still narrows
    capabilities, human handoff and response strategy against the selected intent.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    intent: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)
    required_capabilities: list[str] = Field(max_length=8)
    missing_slots: list[str] = Field(max_length=6)
    continuation_of_previous_turn: bool
    needs_human: bool
    handoff_reason: str | None = Field(default=None, max_length=200)
    response_strategy: ResponseStrategy
    search_text: str | None = Field(default=None, max_length=120)


def decision_json_schema(
    intents: Sequence[str], capabilities: Sequence[str]
) -> dict[str, object]:
    """Return a provider schema with closed intent/capability enums."""

    schema = ModelAgentDecision.model_json_schema()
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("Agent decision schema properties are missing")
    intent_schema = properties.get("intent")
    capability_schema = properties.get("required_capabilities")
    if not isinstance(intent_schema, dict) or not isinstance(capability_schema, dict):
        raise RuntimeError("Agent decision schema is invalid")
    intent_schema["enum"] = list(intents)
    items = capability_schema.get("items")
    if not isinstance(items, dict):
        raise RuntimeError("Agent capability schema is invalid")
    items["enum"] = list(capabilities)
    # Compatible providers are more reliable when every field is explicitly required,
    # including nullable fields.
    schema["required"] = list(properties)
    schema["additionalProperties"] = False
    return schema


def validate_model_decision(
    raw: Mapping[str, Any],
    *,
    intents: Sequence[str],
    capabilities_by_intent: Mapping[str, Sequence[str]],
) -> ModelAgentDecision:
    """Validate and narrow an untrusted provider plan against server policy."""

    payload = {key: value for key, value in raw.items() if not str(key).startswith("_provider_")}
    try:
        decision = ModelAgentDecision.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("model returned an invalid Agent decision") from exc
    if decision.intent not in intents:
        raise ValueError("model returned an unsupported Agent intent")
    allowed = tuple(dict.fromkeys(capabilities_by_intent.get(decision.intent, ())))
    requested = tuple(dict.fromkeys(decision.required_capabilities))
    if any(item not in allowed for item in requested):
        raise ValueError("model requested a capability outside the selected intent")
    capabilities = requested or allowed
    missing_slots = tuple(
        item.strip()[:64]
        for item in dict.fromkeys(decision.missing_slots)
        if item.strip()
    )
    human_handoff = decision.intent == "human_handoff"
    strategy: ResponseStrategy
    if human_handoff:
        strategy = "handoff"
    elif missing_slots and decision.confidence < 0.65:
        strategy = "clarify"
    elif decision.response_strategy in {"handoff", "refuse"}:
        # A model cannot independently escalate or refuse a permitted request.
        strategy = "answer"
    else:
        strategy = decision.response_strategy
    return decision.model_copy(
        update={
            "required_capabilities": list(capabilities),
            "missing_slots": list(missing_slots),
            "needs_human": human_handoff,
            "handoff_reason": decision.handoff_reason if human_handoff else None,
            "response_strategy": strategy,
        }
    )
