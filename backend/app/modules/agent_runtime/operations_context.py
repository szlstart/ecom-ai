from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.security import utc_now
from app.modules.agent_runtime.models import AgentDefinition, AgentRun, AgentVersion
from app.modules.identity.models import User
from app.modules.knowledge.skill_registry import SkillRegistry
from app.modules.messaging.models import Conversation, Message
from app.modules.rbac.models import Role, UserRole
from app.modules.stores.models import Store

MERCHANT_TOOLS = frozenset(
    {
        "store_ops.overview",
        "store_ops.catalog_summary",
        "store_ops.order_summary",
        "store_ops.inventory_risks",
        "support.create_platform_ticket",
        "support.get_ticket_status",
    }
)
ADMIN_TOOLS = frozenset(
    {
        "governance.platform_overview",
        "governance.user_summary",
        "governance.store_summary",
        "governance.order_summary",
        "observability.runtime_health",
    }
)


@dataclass(frozen=True)
class TrustedOperationsContext:
    run: AgentRun
    conversation: Conversation
    trigger: Message
    user: User
    agent_definition: AgentDefinition
    agent_version: AgentVersion
    allowed_tools: frozenset[str]
    audience: Literal["merchant", "admin"]
    store: Store | None

    @property
    def trusted_scope(self) -> dict[str, object]:
        return {
            "user_no": self.user.user_no,
            "conversation_no": self.conversation.conversation_no,
            "scope_type": "store" if self.store else "platform",
            "store_no": self.store.store_no if self.store else None,
        }


class OperationsContextBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def build(self, run: AgentRun) -> TrustedOperationsContext:
        row = (
            await self.session.execute(
                select(Conversation, Message, User, AgentVersion, AgentDefinition)
                .select_from(AgentRun)
                .join(Conversation, Conversation.id == AgentRun.conversation_id)
                .join(Message, Message.id == AgentRun.trigger_message_id)
                .join_from(Conversation, User, User.id == Conversation.user_id)
                .join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)
                .join_from(
                    AgentVersion,
                    AgentDefinition,
                    AgentDefinition.id == AgentVersion.agent_id,
                )
                .where(AgentRun.id == run.id)
            )
        ).one_or_none()
        if row is None:
            raise _context_error("AGENT_TRUSTED_SCOPE_UNAVAILABLE")
        conversation, trigger, user, version, definition = row
        base_valid = (
            conversation.conversation_type == "exclusive"
            and conversation.store_id is None
            and trigger.conversation_id == conversation.id
            and trigger.sender_type == "user"
            and trigger.sender_id == user.id
            and user.user_status == "active"
            and definition.scope_type == "platform"
            and definition.store_id is None
            and definition.agent_status == "active"
            and version.version_status == "published"
        )
        if not base_valid or definition.agent_code not in {"merchant_copilot", "admin_copilot"}:
            raise _context_error("AGENT_TRUSTED_SCOPE_MISMATCH")

        store: Store | None = None
        if definition.agent_code == "merchant_copilot":
            store_id = await self.session.scalar(
                select(UserRole.scope_id)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    UserRole.user_id == user.id,
                    UserRole.grant_status == "active",
                    or_(UserRole.expires_at.is_(None), UserRole.expires_at > utc_now()),
                    Role.role_code == "store_operator",
                    Role.role_status == "active",
                    Role.deleted_at.is_(None),
                    UserRole.scope_type == "store",
                )
                .order_by(UserRole.id)
            )
            store = await self.session.get(Store, store_id) if store_id else None
            if store is None or store.owner_user_id != user.id:
                raise _context_error("AGENT_STORE_SCOPE_INVALID")
            audience: Literal["merchant", "admin"] = "merchant"
            maximum_tools = MERCHANT_TOOLS
        else:
            admin_grant = await self.session.scalar(
                select(UserRole.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    UserRole.user_id == user.id,
                    UserRole.grant_status == "active",
                    or_(UserRole.expires_at.is_(None), UserRole.expires_at > utc_now()),
                    Role.role_code == "platform_super_admin",
                    Role.role_status == "active",
                    Role.deleted_at.is_(None),
                    UserRole.scope_type == "platform",
                    UserRole.scope_id == 0,
                )
            )
            if admin_grant is None:
                raise _context_error("AGENT_ADMIN_SCOPE_INVALID")
            audience = "admin"
            maximum_tools = ADMIN_TOOLS

        try:
            allowed_tools = await SkillRegistry(self.session).effective_tools(
                version, definition.agent_code
            )
        except PermissionError as exc:
            raise _context_error("AGENT_TOOL_POLICY_INVALID") from exc
        if not allowed_tools or not allowed_tools <= maximum_tools:
            raise _context_error("AGENT_TOOL_POLICY_INVALID")
        return TrustedOperationsContext(
            run=run,
            conversation=conversation,
            trigger=trigger,
            user=user,
            agent_definition=definition,
            agent_version=version,
            allowed_tools=allowed_tools,
            audience=audience,
            store=store,
        )


def _context_error(code: str) -> ApplicationError:
    return ApplicationError(
        status=409,
        code=code,
        title="Agent context unavailable",
        detail="智能助理的身份或数据范围不可用。",
    )
