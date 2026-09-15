from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent_runtime.models import AgentConversationState
from app.modules.agent_runtime.prompt_safety import safe_untrusted_excerpt
from app.modules.messaging.models import Conversation, Message

STATE_SCHEMA_VERSION = "conversation_state_v2"
MAX_CARD_SETS = 8
MAX_ACTIONS = 8
MAX_TOPIC_FRAMES = 6
MAX_CORRECTIONS = 6


@dataclass(frozen=True)
class ConversationStateSnapshot:
    topic_generation: int
    source_sequence_no: int
    payload: dict[str, object]

    def planning_projection(self) -> dict[str, object]:
        # Keep the durable state richer than the prompt projection.  Long chats
        # must not let old card collections crowd the current user turn and
        # recent verbatim context out of the model budget.
        compact_payload = dict(self.payload)
        for key, limit in (
            ("card_sets", 4),
            ("last_successful_actions", 4),
            ("topic_frames", 4),
            ("resolved_tasks", 4),
            ("explicit_corrections", 4),
        ):
            value = compact_payload.get(key)
            if isinstance(value, list):
                compact_payload[key] = value[-limit:]
        return {
            **compact_payload,
            "schema_version": STATE_SCHEMA_VERSION,
            "topic_generation": self.topic_generation,
            "source_sequence_no": self.source_sequence_no,
            "trust_level": "conversation_continuity_only",
            "business_fact_authoritative": False,
        }

    def evidence_projection(self) -> dict[str, object]:
        card_sets = self.payload.get("card_sets")
        actions = self.payload.get("last_successful_actions")
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "topic_generation": self.topic_generation,
            "source_sequence_no": self.source_sequence_no,
            "active_domain": self.payload.get("active_domain"),
            "active_intent": self.payload.get("active_intent"),
            "card_set_count": len(card_sets) if isinstance(card_sets, list) else 0,
            "successful_action_count": len(actions) if isinstance(actions, list) else 0,
            "business_fact_authoritative": False,
        }


class ConversationStateRuntime:
    """Persist one typed continuity snapshot per conversation.

    The state owns conversational focus and card ordering. It deliberately does
    not own prices, inventory, balances, fulfillment state, or other volatile
    commerce facts; those are refreshed through tools on every relevant turn.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def load(
        self,
        conversation: Conversation,
        *,
        before_sequence: int | None = None,
    ) -> ConversationStateSnapshot | None:
        row = await self.session.scalar(
            select(AgentConversationState).where(
                AgentConversationState.conversation_id == conversation.id
            )
        )
        if row is None or row.state_schema_version != STATE_SCHEMA_VERSION:
            return None
        if before_sequence is not None and row.source_sequence_no >= before_sequence:
            return None
        payload = dict(row.state_payload) if isinstance(row.state_payload, Mapping) else {}
        return ConversationStateSnapshot(
            topic_generation=max(1, int(row.topic_generation)),
            source_sequence_no=max(0, int(row.source_sequence_no)),
            payload=payload,
        )

    async def record_agent_response(
        self,
        conversation: Conversation,
        trigger: Message,
        response: Message,
        trace: Mapping[str, Any],
    ) -> ConversationStateSnapshot:
        row = await self.session.scalar(
            select(AgentConversationState)
            .where(AgentConversationState.conversation_id == conversation.id)
            .with_for_update()
        )
        previous = (
            dict(row.state_payload)
            if row is not None and isinstance(row.state_payload, Mapping)
            else {}
        )
        intent = _text(trace.get("intent"), 64) or "general_chat"
        domain = _domain_for_intent(intent, _text(previous.get("active_domain"), 32))
        previous_domain = _text(previous.get("active_domain"), 32)
        topic_generation = max(1, int(row.topic_generation)) if row is not None else 1
        if previous_domain and domain and previous_domain != domain:
            topic_generation += 1

        payload = response.content_payload if isinstance(response.content_payload, Mapping) else {}
        card_sets = _existing_list(previous.get("card_sets"))
        new_sets = _card_sets(response, intent, topic_generation)
        if new_sets:
            card_sets.extend(new_sets)
            card_sets = card_sets[-MAX_CARD_SETS:]

        actions = _existing_list(previous.get("last_successful_actions"))
        actions.extend(_successful_actions(response, trace, intent, topic_generation))
        actions = actions[-MAX_ACTIONS:]

        frames = _existing_list(previous.get("topic_frames"))
        frame = {
            "generation": topic_generation,
            "domain": domain,
            "intent": intent,
            "goal": _text(trigger.text_content, 800),
            "trigger_message_id": trigger.message_no,
            "response_message_id": response.message_no,
        }
        if frames and frames[-1].get("generation") == topic_generation:
            frames[-1] = frame
        else:
            frames.append(frame)
        frames = frames[-MAX_TOPIC_FRAMES:]

        previous_corrections = previous.get("explicit_corrections")
        corrections = (
            [str(value) for value in previous_corrections if isinstance(value, str)]
            if isinstance(previous_corrections, list)
            else []
        )
        correction = _explicit_correction(trigger.text_content or "")
        if correction and correction not in corrections:
            corrections.append(correction)

        steps = trace.get("steps")
        step_values = (
            [item for item in steps if isinstance(item, Mapping)] if isinstance(steps, list) else []
        )
        open_tasks = [
            _text(item.get("objective") or item.get("label"), 300)
            for item in step_values
            if str(item.get("status") or "").casefold() in {"waiting", "pending", "running"}
        ]
        resolved_tasks = [
            _text(item.get("objective") or item.get("label"), 300)
            for item in step_values
            if str(item.get("status") or "").casefold() in {"completed", "succeeded"}
        ]
        state_payload: dict[str, object] = {
            "active_domain": domain,
            "active_domains": _active_domains(trace, domain),
            "active_intent": intent,
            "active_goal": _text(trigger.text_content, 800),
            "last_user_message_id": trigger.message_no,
            "last_agent_message_id": response.message_no,
            "pending_assistant_question": _pending_question(response.text_content),
            "active_resources": _active_resources(payload),
            "card_sets": card_sets,
            "last_successful_actions": actions,
            "pending_approval": _pending_approval(trace, response),
            "open_tasks": [item for item in open_tasks if item],
            "resolved_tasks": [item for item in resolved_tasks if item][-8:],
            "explicit_corrections": corrections[-MAX_CORRECTIONS:],
            "topic_frames": frames,
            "facts_requiring_refresh": [
                "price",
                "inventory",
                "cart",
                "wallet",
                "order",
                "logistics",
                "refund",
                "favorites",
                "address",
                "revenue",
                "store_operations",
                "support_queue",
                "runtime_health",
            ],
        }
        if row is None:
            row = AgentConversationState(
                conversation_id=conversation.id,
                state_schema_version=STATE_SCHEMA_VERSION,
                topic_generation=topic_generation,
                source_sequence_no=response.sequence_no,
                state_payload=state_payload,
            )
            self.session.add(row)
        else:
            row.topic_generation = topic_generation
            row.source_sequence_no = response.sequence_no
            row.state_payload = state_payload
            row.version += 1
        await self.session.flush()
        return ConversationStateSnapshot(topic_generation, response.sequence_no, state_payload)

    async def clear(self, conversation_id: int) -> None:
        row = await self.session.scalar(
            select(AgentConversationState)
            .where(AgentConversationState.conversation_id == conversation_id)
            .with_for_update()
        )
        if row is not None:
            await self.session.delete(row)
            await self.session.flush()


def latest_card_set(
    state: ConversationStateSnapshot | None,
    card_type: str,
    *,
    minimum_count: int = 1,
) -> list[dict[str, object]] | None:
    if state is None:
        return None
    values = state.payload.get("card_sets")
    if not isinstance(values, list):
        return None
    for raw in reversed(values):
        if not isinstance(raw, Mapping) or raw.get("card_type") != card_type:
            continue
        cards = raw.get("cards")
        values = (
            [dict(item) for item in cards if isinstance(item, Mapping)]
            if isinstance(cards, list)
            else []
        )
        if not values:
            return []
        if len(values) >= minimum_count:
            return values
    return None


def _card_sets(response: Message, intent: str, generation: int) -> list[dict[str, object]]:
    payload = response.content_payload if isinstance(response.content_payload, Mapping) else {}
    result: list[dict[str, object]] = []
    definitions = (
        ("product", "product_cards", "product_id"),
        ("order", "order_cards", "order_id"),
        ("address", "address_cards", "address_id"),
    )
    for card_type, key, id_key in definitions:
        raw_cards = payload.get(key)
        relevant_empty = (
            card_type == "product"
            and intent in {"product_search", "product_compare", "personalized_recommendation"}
        ) or (card_type == "order" and intent == "order_lookup")
        if not isinstance(raw_cards, list) and not relevant_empty:
            continue
        cards = [
            _card_reference(card, id_key)
            for card in (raw_cards if isinstance(raw_cards, list) else [])
            if isinstance(card, Mapping)
        ]
        result.append(
            {
                "card_set_id": f"{response.message_no}:{card_type}",
                "card_type": card_type,
                "message_id": response.message_no,
                "sequence_no": response.sequence_no,
                "topic_generation": generation,
                "intent": intent,
                "cards": [item for item in cards if item],
            }
        )
    raw_detail_cards = payload.get("detail_cards")
    if isinstance(raw_detail_cards, list):
        grouped: dict[str, list[dict[str, object]]] = {}
        for raw_card in raw_detail_cards:
            if not isinstance(raw_card, Mapping):
                continue
            kind = _text(raw_card.get("kind"), 48) or "detail"
            reference = _card_reference(raw_card, "resource_id")
            if reference:
                grouped.setdefault(kind, []).append(reference)
        for kind, cards in grouped.items():
            result.append(
                {
                    "card_set_id": f"{response.message_no}:detail:{kind}",
                    "card_type": f"detail:{kind}",
                    "message_id": response.message_no,
                    "sequence_no": response.sequence_no,
                    "topic_generation": generation,
                    "intent": intent,
                    "cards": cards[:8],
                }
            )
    return result


def _card_reference(card: Mapping[str, Any], id_key: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in (
        id_key,
        "kind",
        "title",
        "badge",
        "product_id",
        "product_name",
        "sku_id",
        "sku_name",
        "order_id",
        "refund_id",
        "shipment_id",
        "address_id",
    ):
        value = card.get(key)
        if isinstance(value, str) and value:
            result[key] = safe_untrusted_excerpt(value, 240)
    items = card.get("items")
    if isinstance(items, list):
        result["items"] = [
            {
                key: safe_untrusted_excerpt(str(item[key]), 180)
                for key in ("product_id", "product_name", "sku_id", "sku_name")
                if isinstance(item, Mapping) and item.get(key) is not None
            }
            for item in items[:3]
            if isinstance(item, Mapping)
        ]
    return result


def _successful_actions(
    response: Message,
    trace: Mapping[str, Any],
    intent: str,
    generation: int,
) -> list[dict[str, object]]:
    steps = trace.get("steps")
    if not isinstance(steps, list):
        return []
    payload = response.content_payload if isinstance(response.content_payload, Mapping) else {}
    resources = _active_resources(payload)
    result: list[dict[str, object]] = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        tool_step = step.get("tool_call") if step.get("kind") == "delegation" else step
        if not isinstance(tool_step, Mapping):
            continue
        if str(tool_step.get("status") or step.get("status") or "").casefold() not in {
            "completed",
            "succeeded",
        }:
            continue
        tool_code = tool_step.get("tool_code")
        if not isinstance(tool_code, str) or not tool_code or tool_code in {"none", "multi_agent"}:
            continue
        result.append(
            {
                "action_id": f"{response.message_no}:{len(result) + 1}",
                "tool_code": tool_code,
                "intent": intent,
                "message_id": response.message_no,
                "topic_generation": generation,
                "resource_refs": resources,
            }
        )
    return result


def _active_domains(trace: Mapping[str, Any], fallback: str) -> list[str]:
    subtasks = trace.get("subtasks")
    values: list[str] = []
    if isinstance(subtasks, list):
        for item in subtasks:
            if not isinstance(item, Mapping):
                continue
            specialist = item.get("specialist")
            if isinstance(specialist, str) and specialist and specialist not in values:
                values.append(specialist[:32])
    return values[:4] or [fallback]


def _active_resources(payload: Mapping[str, Any]) -> list[dict[str, object]]:
    sources = payload.get("sources")
    result: list[dict[str, object]] = []
    if isinstance(sources, list):
        for raw in sources[:8]:
            if not isinstance(raw, Mapping):
                continue
            resource_type = raw.get("type")
            resource_no = raw.get("id")
            if isinstance(resource_type, str) and isinstance(resource_no, str):
                result.append(
                    {
                        "resource_type": resource_type[:32],
                        "resource_id": resource_no[:64],
                        "resource_version": raw.get("version")
                        if isinstance(raw.get("version"), int)
                        else None,
                    }
                )
    return result


def _pending_approval(trace: Mapping[str, Any], response: Message) -> dict[str, object] | None:
    status = str(trace.get("status") or "").casefold()
    if status not in {"waiting", "waiting_confirmation", "pending_confirmation"}:
        return None
    return {
        "run_id": response.ai_run_no,
        "message_id": response.message_no,
        "status": status,
        "intent": _text(trace.get("intent"), 64),
    }


def _domain_for_intent(intent: str, fallback: str | None) -> str:
    if intent == "compound_request":
        return "multi"
    groups = {
        "catalog": {
            "product_search",
            "product_compare",
            "personalized_recommendation",
            "product_qa",
            "product_recommend",
            "sku_compare",
            "inventory_lookup",
        },
        "cart": {
            "cart_lookup",
            "cart_add",
            "cart_update",
            "cart_remove",
            "cart_clear",
            "checkout_preview",
        },
        "order": {
            "order_lookup",
            "order_explain",
            "logistics_lookup",
            "refund_precheck",
            "refund_eligibility",
            "refund_progress",
            "review_draft",
        },
        "account": {
            "address_lookup",
            "wallet_lookup",
            "favorites_lookup",
            "favorite_update",
            "memory_lookup",
        },
        "policy": {"policy_qa"},
        "support": {"human_handoff", "human_service_capabilities"},
        "merchant_catalog": {"catalog"},
        "merchant_inventory": {"inventory"},
        "operations_order": {"orders"},
        "governance_user": {"users"},
        "governance_store": {"stores"},
        "observability": {"runtime"},
        "operations": {
            "overview",
            "complex_platform_diagnosis",
            "complex_store_diagnosis",
            "operation_guide",
        },
    }
    for domain, intents in groups.items():
        if intent in intents:
            return domain
    return fallback or "general"


def _pending_question(value: str | None) -> str | None:
    text = _text(value, 800)
    if text and (
        text.rstrip().endswith(("?", "？"))
        or "需要我" in text
        or "是否要" in text
    ):
        return text
    return None


def _explicit_correction(value: str) -> str | None:
    compact = re.sub(r"\s+", "", value)
    if any(marker in compact for marker in ("不是这个", "不是刚才", "我说的是", "改成", "更正")):
        return safe_untrusted_excerpt(value, 500)
    return None


def _existing_list(value: object) -> list[dict[str, object]]:
    return (
        [dict(item) for item in value if isinstance(item, Mapping)]
        if isinstance(value, list)
        else []
    )


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return safe_untrusted_excerpt(normalized, limit) if normalized else None
