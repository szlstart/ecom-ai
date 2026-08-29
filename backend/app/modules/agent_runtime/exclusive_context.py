from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.modules.agent_runtime.models import AgentDefinition, AgentRun, AgentVersion
from app.modules.agent_runtime.store_context import ContextRef, _parse_context_refs
from app.modules.identity.models import User
from app.modules.knowledge.skill_registry import SkillRegistry
from app.modules.messaging.models import Conversation, ConversationContext, Message

EXCLUSIVE_AGENT_TOOL_CODES = frozenset(
    {
        "catalog.search_products",
        "catalog.compare_products",
        "order.list_user_orders",
        "order.get_user_order_detail",
        "logistics.get_user_order_shipments",
        "after_sale.check_refund_eligibility",
        "after_sale.build_refund_draft",
        "after_sale.submit_refund_application",
        "after_sale.list_user_refunds",
        "after_sale.get_user_refund_detail",
        "support.create_platform_ticket",
        "support.get_ticket_status",
    }
)


@dataclass(frozen=True)
class TrustedExclusiveAgentContext:
    run: AgentRun
    conversation: Conversation
    trigger: Message
    user: User
    agent_version: AgentVersion
    allowed_tools: frozenset[str]
    context_refs: dict[str, ContextRef]

    @property
    def trusted_scope(self) -> dict[str, object]:
        return {
            "user_no": self.user.user_no,
            "conversation_no": self.conversation.conversation_no,
            "scope_type": "platform",
        }


class ExclusiveContextBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def build(self, run: AgentRun) -> TrustedExclusiveAgentContext:
        row = (
            await self.session.execute(
                select(Conversation, Message, User, AgentVersion, AgentDefinition)
                .select_from(Conversation)
                .join_from(Conversation, Message, Message.id == run.trigger_message_id)
                .join_from(Conversation, User, User.id == Conversation.user_id)
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
            raise _context_error("AGENT_TRUSTED_SCOPE_UNAVAILABLE")
        conversation, trigger, user, version, definition = row
        if (
            conversation.conversation_type != "exclusive"
            or conversation.store_id is not None
            or trigger.conversation_id != conversation.id
            or trigger.sender_type != "user"
            or trigger.sender_id != user.id
            or user.user_status != "active"
            or definition.agent_type != "exclusive_service"
            or definition.agent_code != "exclusive_support"
            or definition.scope_type != "platform"
            or definition.store_id is not None
            or definition.agent_status != "active"
            or version.version_status != "published"
        ):
            raise _context_error("AGENT_TRUSTED_SCOPE_MISMATCH")
        try:
            allowed_tools = await SkillRegistry(self.session).effective_tools(
                version, definition.agent_code
            )
        except PermissionError as exc:
            raise _context_error("AGENT_TOOL_POLICY_INVALID") from exc
        if not allowed_tools or not allowed_tools <= EXCLUSIVE_AGENT_TOOL_CODES:
            raise _context_error("AGENT_TOOL_POLICY_INVALID")
        return TrustedExclusiveAgentContext(
            run=run,
            conversation=conversation,
            trigger=trigger,
            user=user,
            agent_version=version,
            allowed_tools=allowed_tools,
            context_refs=_parse_context_refs(run.context_snapshot),
        )

    async def require_active_context(
        self, context: TrustedExclusiveAgentContext, context_type: str
    ) -> ContextRef:
        ref = context.context_refs.get(context_type)
        if ref is None:
            raise _context_error("AGENT_CONTEXT_REQUIRED")
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
            raise _context_error("AGENT_CONTEXT_VERSION_STALE")
        return ref


def _context_error(code: str) -> ApplicationError:
    return ApplicationError(
        status=409,
        code=code,
        title="Agent context unavailable",
        detail="专属客服上下文不存在、已变化或不可访问。",
    )
