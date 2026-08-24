from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent_runtime.models import AgentVersion
from app.modules.knowledge.models import (
    AgentSkillBinding,
    RuntimeKillSwitch,
    SkillDefinition,
    SkillToolBinding,
    SkillVersion,
    ToolDefinition,
    ToolVersion,
)


@dataclass(frozen=True)
class SkillToolPolicy:
    tool_code: str
    effect: str
    confirmation_policy: str
    call_budget: int
    timeout_ms: int


@dataclass(frozen=True)
class SkillExecutionPlan:
    skill_code: str
    skill_version_no: int
    instructions: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    tools: tuple[SkillToolPolicy, ...]

    @property
    def allowed_tools(self) -> frozenset[str]:
        denied = {item.tool_code for item in self.tools if item.effect == "deny"}
        return frozenset(
            item.tool_code
            for item in self.tools
            if item.effect == "allow" and item.tool_code not in denied
        )


class SkillRegistry:
    """Loads a fixed, published Skill snapshot for one immutable Agent Version."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def load(self, agent_version_id: int, skill_code: str) -> SkillExecutionPlan:
        agent_version = await self.session.get(AgentVersion, agent_version_id)
        if agent_version is None or agent_version.version_status not in {"published", "retired"}:
            raise PermissionError("agent version is not executable")
        agent_allowlist = {
            str(tool_code)
            for tool_code in agent_version.tool_allowlist
            if isinstance(tool_code, str)
        }
        row = (
            await self.session.execute(
                select(SkillDefinition, SkillVersion)
                .join(SkillVersion, SkillVersion.skill_id == SkillDefinition.id)
                .join(
                    AgentSkillBinding,
                    AgentSkillBinding.skill_version_id == SkillVersion.id,
                )
                .where(
                    AgentSkillBinding.agent_version_id == agent_version_id,
                    AgentSkillBinding.binding_status == "active",
                    SkillDefinition.skill_code == skill_code,
                    SkillDefinition.skill_status == "active",
                    SkillVersion.version_status == "published",
                )
            )
        ).one_or_none()
        if row is None:
            raise PermissionError("skill is not bound to this agent version")
        definition, version = row
        disabled = await self.session.scalar(
            select(RuntimeKillSwitch.id).where(
                RuntimeKillSwitch.target_type == "skill",
                RuntimeKillSwitch.target_code == definition.skill_code,
                RuntimeKillSwitch.is_active.is_(True),
            )
        )
        if disabled:
            raise PermissionError("skill is disabled by kill switch")
        bindings = list(
            (
                await self.session.execute(
                    select(SkillToolBinding, ToolDefinition)
                    .join(ToolVersion, ToolVersion.id == SkillToolBinding.tool_version_id)
                    .join(ToolDefinition, ToolDefinition.id == ToolVersion.tool_id)
                    .where(
                        SkillToolBinding.skill_version_id == version.id,
                        ToolVersion.version_status == "published",
                        ToolDefinition.tool_status == "active",
                    )
                    .order_by(ToolDefinition.tool_code, SkillToolBinding.id)
                )
            ).all()
        )
        return SkillExecutionPlan(
            skill_code=definition.skill_code,
            skill_version_no=version.version_no,
            instructions=version.instructions,
            input_schema=version.input_schema,
            output_schema=version.output_schema,
            tools=tuple(
                SkillToolPolicy(
                    tool_code=tool.tool_code,
                    effect=binding.permission_effect,
                    confirmation_policy=binding.confirmation_policy,
                    call_budget=binding.call_budget,
                    timeout_ms=binding.timeout_ms,
                )
                for binding, tool in bindings
                if binding.permission_effect == "deny" or tool.tool_code in agent_allowlist
            ),
        )
