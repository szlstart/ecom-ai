from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.modules.agent_runtime.models import AgentDefinition, AgentRun, AgentVersion
from app.modules.identity.models import User
from app.modules.messaging.models import Conversation, ConversationContext, Message
from app.modules.stores.models import Store

STORE_AGENT_TOOL_CODES = frozenset(
    {
        "catalog.get_product",
        "catalog.compare_skus",
        "catalog.compare_products",
        "catalog.search_store_products",
        "catalog.get_inventory_availability",
        "catalog.get_store_policy",
        "order.get_store_order_summary",
        "logistics.get_store_order_shipments",
        "support.create_store_ticket",
        "support.get_ticket_status",
    }
)


@dataclass(frozen=True)
class ContextRef:
    context_no: str
    context_type: str
    context_version: int
    resource_no: str
    resource_version: int | None
    expires_at: datetime | None


@dataclass(frozen=True)
class TrustedStoreAgentContext:
    run: AgentRun
    conversation: Conversation
    trigger: Message
    user: User
    store: Store
    agent_version: AgentVersion
    allowed_tools: frozenset[str]
    context_refs: dict[str, ContextRef]

    @property
    def trusted_scope(self) -> dict[str, object]:
        return {
            "user_no": self.user.user_no,
            "conversation_no": self.conversation.conversation_no,
            "store_no": self.store.store_no,
        }


class StoreContextBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def build(self, run: AgentRun) -> TrustedStoreAgentContext:
        row = (
            await self.session.execute(
                select(Conversation, Message, User, Store, AgentVersion, AgentDefinition)
                .select_from(Conversation)
                .join_from(Conversation, Message, Message.id == run.trigger_message_id)
                .join_from(Conversation, User, User.id == Conversation.user_id)
                .join_from(Conversation, Store, Store.id == Conversation.store_id)
                .join_from(Conversation, AgentVersion, AgentVersion.id == run.agent_version_id)
                .join_from(
                    AgentVersion,
                    AgentDefinition,
                    AgentDefinition.id == AgentVersion.agent_id,
                )
                .where(Conversation.id == run.conversation_id)
            )
        ).one_or_none()
        if row is None:
            raise _context_error("AGENT_TRUSTED_SCOPE_UNAVAILABLE", "可信会话范围不存在。")
        conversation, trigger, user, store, version, definition = row
        if (
            conversation.conversation_type != "store"
            or conversation.store_id is None
            or trigger.conversation_id != conversation.id
            or trigger.sender_type != "user"
            or trigger.sender_id != user.id
            or store.store_status != "active"
            or definition.agent_type != "store_service"
            or definition.agent_code != "store_support"
            or definition.agent_status != "active"
            or version.version_status != "published"
            or not _definition_matches_store(definition, store.id)
        ):
            raise _context_error("AGENT_TRUSTED_SCOPE_MISMATCH", "会话与店铺 Agent 范围不匹配。")
        allowed_tools = frozenset(item for item in version.tool_allowlist if isinstance(item, str))
        if not allowed_tools or not allowed_tools <= STORE_AGENT_TOOL_CODES:
            raise _context_error("AGENT_TOOL_POLICY_INVALID", "Agent 工具策略无效。")
        refs = _parse_context_refs(run.context_snapshot)
        return TrustedStoreAgentContext(
            run=run,
            conversation=conversation,
            trigger=trigger,
            user=user,
            store=store,
            agent_version=version,
            allowed_tools=allowed_tools,
            context_refs=refs,
        )

    async def require_active_context(
        self,
        context: TrustedStoreAgentContext,
        context_type: str,
        *,
        resource_no: str | None = None,
    ) -> ContextRef:
        ref = context.context_refs.get(context_type)
        if ref is None or (resource_no is not None and ref.resource_no != resource_no):
            raise _context_error("AGENT_CONTEXT_REQUIRED", "请先从对应页面选择要咨询的内容。")
        current = await self.session.scalar(
            select(ConversationContext).where(
                ConversationContext.conversation_id == context.conversation.id,
                ConversationContext.context_no == ref.context_no,
                ConversationContext.context_type == context_type,
                ConversationContext.resource_no == ref.resource_no,
                ConversationContext.context_version == ref.context_version,
                ConversationContext.context_status == "active",
            )
        )
        now = utc_now()
        if (
            current is None
            or current.resource_version != ref.resource_version
            or (current.expires_at is not None and current.expires_at <= now)
            or (ref.expires_at is not None and ref.expires_at <= now)
        ):
            raise _context_error("AGENT_CONTEXT_VERSION_STALE", "页面上下文已变化，请重新选择。")
        return ref


def _definition_matches_store(definition: AgentDefinition, store_id: int) -> bool:
    return (definition.scope_type == "store" and definition.store_id == store_id) or (
        definition.scope_type == "platform"
        and definition.store_id is None
        and definition.strategy_reuse_approved
    )


def _parse_context_refs(values: list[dict[str, object]]) -> dict[str, ContextRef]:
    result: dict[str, ContextRef] = {}
    for item in values:
        try:
            context_version_value = item["context_version"]
            resource_version_value = item.get("resource_version")
            if not isinstance(context_version_value, int) or isinstance(
                context_version_value, bool
            ):
                raise TypeError("context version must be an integer")
            if resource_version_value is not None and (
                not isinstance(resource_version_value, int)
                or isinstance(resource_version_value, bool)
            ):
                raise TypeError("resource version must be an integer")
            expires_value = item.get("expires_at")
            expires_at = (
                datetime.fromisoformat(expires_value)
                if isinstance(expires_value, str) and expires_value
                else None
            )
            context_type = str(item["context_type"])
            ref = ContextRef(
                context_no=str(item["context_id"]),
                context_type=context_type,
                context_version=context_version_value,
                resource_no=str(item["resource_id"]),
                resource_version=(
                    resource_version_value if resource_version_value is not None else None
                ),
                expires_at=expires_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _context_error(
                "AGENT_CONTEXT_SNAPSHOT_INVALID", "Agent 上下文快照无效。"
            ) from exc
        if context_type in result:
            raise _context_error("AGENT_CONTEXT_SNAPSHOT_INVALID", "上下文类型重复。")
        result[context_type] = ref
    return result


def _context_error(code: str, detail: str) -> ApplicationError:
    return ApplicationError(
        status=409,
        code=code,
        title="Agent context unavailable",
        detail=detail,
    )
