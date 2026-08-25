from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.modules.agent_runtime.models import AgentDefinition, AgentVersion
from app.modules.knowledge.mcp_registry import server_for_tool
from app.modules.knowledge.models import (
    AgentSkillBinding,
    RuntimeKillSwitch,
    SkillDefinition,
    SkillToolBinding,
    SkillVersion,
    ToolDefinition,
    ToolVersion,
)
from app.modules.knowledge.publication_service import multi_agent_policy_is_publishable
from app.modules.knowledge.schemas import (
    AgentList,
    AgentSkillBindingCreate,
    AgentVersionCreate,
    AgentVersionSummary,
    AgentView,
    KillSwitchList,
    KillSwitchView,
    SkillDefinitionCreate,
    SkillList,
    SkillToolBindingCreate,
    SkillVersionCreate,
    SkillView,
    ToolCreate,
    ToolList,
    ToolVersionCreate,
    ToolVersionSummary,
    ToolView,
    VersionBindingView,
)
from app.modules.rbac.audit import record_admin_operation
from app.modules.rbac.dependencies import AdminAccess


class KnowledgeAdminService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def skills(self) -> SkillList:
        rows = list(
            (
                await self.session.scalars(
                    select(SkillDefinition).order_by(SkillDefinition.skill_code)
                )
            ).all()
        )
        return SkillList(items=[await self._skill_view(item) for item in rows])

    async def agents(self) -> AgentList:
        definitions = list(
            (
                await self.session.scalars(
                    select(AgentDefinition).order_by(AgentDefinition.agent_code)
                )
            ).all()
        )
        items: list[AgentView] = []
        for definition in definitions:
            versions = list(
                (
                    await self.session.scalars(
                        select(AgentVersion)
                        .where(AgentVersion.agent_id == definition.id)
                        .order_by(AgentVersion.version_no.desc())
                    )
                ).all()
            )
            items.append(
                AgentView(
                    agent_id=definition.agent_no,
                    agent_code=definition.agent_code,
                    display_name=definition.display_name,
                    scope_type=definition.scope_type,
                    status=definition.agent_status,
                    versions=[
                        AgentVersionSummary(
                            version_no=item.version_no,
                            status=item.version_status,
                            model_profile=item.model_profile,
                            tool_allowlist=[str(code) for code in item.tool_allowlist],
                            system_prompt=item.system_prompt,
                            policy_config=item.policy_config,
                        )
                        for item in versions
                    ],
                )
            )
        return AgentList(items=items)

    async def create_agent_version(
        self,
        access: AdminAccess,
        agent_no: str,
        payload: AgentVersionCreate,
    ) -> AgentView:
        agent = await self.session.scalar(
            select(AgentDefinition)
            .where(AgentDefinition.agent_no == agent_no)
            .with_for_update()
        )
        if agent is None:
            raise _not_found()
        latest = int(
            await self.session.scalar(
                select(func.max(AgentVersion.version_no)).where(AgentVersion.agent_id == agent.id)
            )
            or 0
        )
        self.session.add(
            AgentVersion(
                agent_id=agent.id,
                version_no=latest + 1,
                version_status="draft",
                system_prompt=payload.system_prompt,
                model_profile=payload.model_profile,
                tool_allowlist=payload.tool_allowlist,
                policy_config=payload.policy_config,
            )
        )
        agent.version += 1
        record_admin_operation(
            self.session,
            access,
            action="agent.version.create",
            target_type="agent",
            target_no=agent.agent_no,
            after={"version": latest + 1},
        )
        await self.session.commit()
        return next(item for item in (await self.agents()).items if item.agent_id == agent_no)

    async def publish_agent(
        self, access: AdminAccess, agent_no: str, version_no: int
    ) -> AgentView:
        agent = await self.session.scalar(
            select(AgentDefinition).where(AgentDefinition.agent_no == agent_no)
        )
        if agent is None:
            raise _not_found()
        version = await self.session.scalar(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent.id, AgentVersion.version_no == version_no)
            .with_for_update()
        )
        evaluation = version.policy_config.get("evaluation_report") if version else None
        if (
            version is None
            or version.version_status != "draft"
            or not isinstance(evaluation, dict)
            or not bool(evaluation.get("passed"))
            or not multi_agent_policy_is_publishable(version.policy_config)
        ):
            raise _conflict("AGENT_VERSION_NOT_PUBLISHABLE")
        old = list(
            (
                await self.session.scalars(
                    select(AgentVersion)
                    .where(
                        AgentVersion.agent_id == agent.id,
                        AgentVersion.version_status == "published",
                    )
                    .with_for_update()
                )
            ).all()
        )
        for item in old:
            item.version_status = "retired"
            item.version += 1
        version.version_status = "published"
        version.published_at = utc_now()
        version.version += 1
        record_admin_operation(
            self.session,
            access,
            action="agent.version.publish",
            target_type="agent",
            target_no=agent.agent_no,
            after={"version": version_no},
        )
        await self.session.commit()
        return next(item for item in (await self.agents()).items if item.agent_id == agent_no)

    async def bind_skill(
        self,
        access: AdminAccess,
        agent_no: str,
        agent_version_no: int,
        payload: AgentSkillBindingCreate,
    ) -> VersionBindingView:
        row = (
            await self.session.execute(
                select(AgentDefinition, AgentVersion)
                .join(AgentVersion, AgentVersion.agent_id == AgentDefinition.id)
                .where(
                    AgentDefinition.agent_no == agent_no,
                    AgentVersion.version_no == agent_version_no,
                )
                .with_for_update()
            )
        ).one_or_none()
        skill_row = (
            await self.session.execute(
                select(SkillDefinition, SkillVersion)
                .join(SkillVersion, SkillVersion.skill_id == SkillDefinition.id)
                .where(
                    SkillDefinition.skill_no == payload.skill_id,
                    SkillVersion.version_no == payload.skill_version_no,
                    SkillVersion.version_status == "published",
                )
            )
        ).one_or_none()
        if row is None or skill_row is None:
            raise _not_found()
        agent, agent_version = row
        skill, skill_version = skill_row
        if agent_version.version_status != "draft":
            raise _conflict("AGENT_VERSION_IMMUTABLE")
        binding = AgentSkillBinding(
            agent_version_id=agent_version.id,
            skill_version_id=skill_version.id,
            binding_status="active",
        )
        self.session.add(binding)
        agent_version.version += 1
        record_admin_operation(
            self.session,
            access,
            action="agent.skill.bind",
            target_type="agent_version",
            target_no=f"{agent.agent_no}:v{agent_version.version_no}",
            after={"skill": skill.skill_code, "skill_version": skill_version.version_no},
        )
        await self.session.commit()
        return VersionBindingView(
            binding_id=binding.id,
            source_version_no=agent_version.version_no,
            target_code=skill.skill_code,
            target_version_no=skill_version.version_no,
            effect="allow",
        )

    async def bind_tool(
        self,
        access: AdminAccess,
        skill_no: str,
        skill_version_no: int,
        payload: SkillToolBindingCreate,
    ) -> VersionBindingView:
        skill_row = (
            await self.session.execute(
                select(SkillDefinition, SkillVersion)
                .join(SkillVersion, SkillVersion.skill_id == SkillDefinition.id)
                .where(
                    SkillDefinition.skill_no == skill_no,
                    SkillVersion.version_no == skill_version_no,
                )
                .with_for_update()
            )
        ).one_or_none()
        tool_row = (
            await self.session.execute(
                select(ToolDefinition, ToolVersion)
                .join(ToolVersion, ToolVersion.tool_id == ToolDefinition.id)
                .where(
                    ToolDefinition.tool_code == payload.tool_code,
                    ToolVersion.version_no == payload.tool_version_no,
                    ToolVersion.version_status == "published",
                )
            )
        ).one_or_none()
        if skill_row is None or tool_row is None:
            raise _not_found()
        skill, skill_version = skill_row
        tool, tool_version = tool_row
        if skill_version.version_status != "draft":
            raise _conflict("SKILL_VERSION_IMMUTABLE")
        binding = SkillToolBinding(
            skill_version_id=skill_version.id,
            tool_version_id=tool_version.id,
            permission_effect=payload.permission_effect,
            confirmation_policy=payload.confirmation_policy,
            call_budget=payload.call_budget,
            timeout_ms=payload.timeout_ms,
        )
        self.session.add(binding)
        skill_version.version += 1
        record_admin_operation(
            self.session,
            access,
            action="skill.tool.bind",
            target_type="skill_version",
            target_no=f"{skill.skill_no}:v{skill_version.version_no}",
            after={
                "tool": tool.tool_code,
                "tool_version": tool_version.version_no,
                "effect": payload.permission_effect,
            },
        )
        await self.session.commit()
        return VersionBindingView(
            binding_id=binding.id,
            source_version_no=skill_version.version_no,
            target_code=tool.tool_code,
            target_version_no=tool_version.version_no,
            effect=payload.permission_effect,
        )

    async def kill_switches(self) -> KillSwitchList:
        rows = list(
            (
                await self.session.scalars(
                    select(RuntimeKillSwitch).order_by(
                        RuntimeKillSwitch.target_type, RuntimeKillSwitch.target_code
                    )
                )
            ).all()
        )
        return KillSwitchList(items=[_kill_switch_view(item) for item in rows])

    async def change_kill_switch(
        self,
        access: AdminAccess,
        *,
        target_type: str,
        target_code: str,
        active: bool,
        reason: str,
    ) -> KillSwitchView:
        if target_type not in {"agent", "skill", "tool", "mcp_server"}:
            raise _conflict("KILL_SWITCH_TARGET_INVALID")
        item = await self.session.scalar(
            select(RuntimeKillSwitch)
            .where(
                RuntimeKillSwitch.target_type == target_type,
                RuntimeKillSwitch.target_code == target_code,
            )
            .with_for_update()
        )
        if item is None:
            item = RuntimeKillSwitch(
                switch_no=new_prefixed_ulid("ksw_"),
                target_type=target_type,
                target_code=target_code,
                is_active=active,
                reason=reason,
                changed_by=access.context.user.id,
            )
            self.session.add(item)
        else:
            item.is_active = active
            item.reason = reason
            item.changed_by = access.context.user.id
            item.version += 1
        record_admin_operation(
            self.session,
            access,
            action="ai.kill_switch.activate" if active else "ai.kill_switch.deactivate",
            target_type=target_type,
            target_no=target_code,
            reason=reason,
            after={"active": active},
        )
        await self.session.commit()
        return _kill_switch_view(item)

    async def create_skill(self, access: AdminAccess, payload: SkillDefinitionCreate) -> SkillView:
        if await self.session.scalar(
            select(SkillDefinition).where(SkillDefinition.skill_code == payload.skill_code)
        ):
            raise _conflict("SKILL_CODE_EXISTS")
        item = SkillDefinition(
            skill_no=new_prefixed_ulid("skl_"),
            skill_code=payload.skill_code,
            display_name=payload.display_name,
            skill_status="active",
        )
        self.session.add(item)
        await self.session.flush()
        record_admin_operation(
            self.session,
            access,
            action="skill.create",
            target_type="skill",
            target_no=item.skill_no,
        )
        await self.session.commit()
        return await self._skill_view(item)

    async def create_version(
        self, access: AdminAccess, skill_no: str, payload: SkillVersionCreate
    ) -> SkillView:
        skill = await self.session.scalar(
            select(SkillDefinition).where(SkillDefinition.skill_no == skill_no).with_for_update()
        )
        if skill is None:
            raise _not_found()
        next_version = (
            int(
                await self.session.scalar(
                    select(func.max(SkillVersion.version_no)).where(
                        SkillVersion.skill_id == skill.id
                    )
                )
                or 0
            )
            + 1
        )
        self.session.add(
            SkillVersion(
                skill_id=skill.id,
                version_no=next_version,
                version_status="draft",
                input_schema=payload.input_schema,
                output_schema=payload.output_schema,
                instructions=payload.instructions,
                evaluation_report=payload.evaluation_report,
            )
        )
        record_admin_operation(
            self.session,
            access,
            action="skill.version.create",
            target_type="skill",
            target_no=skill.skill_no,
            after={"version": next_version},
        )
        await self.session.commit()
        return await self._skill_view(skill)

    async def publish(self, access: AdminAccess, skill_no: str, version_no: int) -> SkillView:
        skill = await self.session.scalar(
            select(SkillDefinition).where(SkillDefinition.skill_no == skill_no)
        )
        if skill is None:
            raise _not_found()
        version = await self.session.scalar(
            select(SkillVersion)
            .where(SkillVersion.skill_id == skill.id, SkillVersion.version_no == version_no)
            .with_for_update()
        )
        if version is None or version.version_status != "draft":
            raise _conflict("SKILL_VERSION_NOT_PUBLISHABLE")
        if not bool(version.evaluation_report.get("passed")):
            raise _conflict("SKILL_EVALUATION_REQUIRED")
        old = list(
            (
                await self.session.scalars(
                    select(SkillVersion)
                    .where(
                        SkillVersion.skill_id == skill.id,
                        SkillVersion.version_status == "published",
                    )
                    .with_for_update()
                )
            ).all()
        )
        for item in old:
            item.version_status = "retired"
            item.version += 1
        version.version_status = "published"
        version.published_at = utc_now()
        version.version += 1
        record_admin_operation(
            self.session,
            access,
            action="skill.version.publish",
            target_type="skill",
            target_no=skill.skill_no,
            after={"version": version_no},
        )
        await self.session.commit()
        return await self._skill_view(skill)

    async def tools(self) -> ToolList:
        rows = list(
            (
                await self.session.scalars(
                    select(ToolDefinition).order_by(ToolDefinition.tool_code)
                )
            ).all()
        )
        return ToolList(
            items=[_tool_view(item, await self._tool_versions(item.id)) for item in rows]
        )

    async def tool(self, tool_code: str) -> ToolView:
        item = await self.session.scalar(
            select(ToolDefinition).where(ToolDefinition.tool_code == tool_code)
        )
        if item is None:
            raise _not_found()
        return _tool_view(item, await self._tool_versions(item.id))

    async def create_tool(self, access: AdminAccess, payload: ToolCreate) -> ToolView:
        try:
            deployed_server = server_for_tool(payload.tool_code)
        except LookupError as exc:
            raise _conflict("TOOL_NOT_DEPLOYED") from exc
        if deployed_server.server_code != payload.server_code:
            raise _conflict("TOOL_SERVER_MISMATCH")
        if await self.session.scalar(
            select(ToolDefinition).where(ToolDefinition.tool_code == payload.tool_code)
        ):
            raise _conflict("TOOL_CODE_EXISTS")
        item = ToolDefinition(**payload.model_dump(), tool_status="draft")
        self.session.add(item)
        record_admin_operation(
            self.session,
            access,
            action="tool.create",
            target_type="tool",
            target_no=payload.tool_code,
        )
        await self.session.commit()
        return _tool_view(item)

    async def create_tool_version(
        self, access: AdminAccess, tool_code: str, payload: ToolVersionCreate
    ) -> ToolView:
        tool = await self.session.scalar(
            select(ToolDefinition).where(ToolDefinition.tool_code == tool_code).with_for_update()
        )
        if tool is None:
            raise _not_found()
        latest = int(
            await self.session.scalar(
                select(func.max(ToolVersion.version_no)).where(ToolVersion.tool_id == tool.id)
            )
            or 0
        )
        self.session.add(
            ToolVersion(
                tool_id=tool.id,
                version_no=latest + 1,
                version_status="draft",
                input_schema=payload.input_schema,
                output_schema=payload.output_schema,
                evaluation_report=payload.evaluation_report,
            )
        )
        record_admin_operation(
            self.session,
            access,
            action="tool.version.create",
            target_type="tool",
            target_no=tool.tool_code,
            after={"version": latest + 1},
        )
        await self.session.commit()
        return _tool_view(tool, await self._tool_versions(tool.id))

    async def publish_tool(self, access: AdminAccess, tool_code: str, version_no: int) -> ToolView:
        tool = await self.session.scalar(
            select(ToolDefinition).where(ToolDefinition.tool_code == tool_code)
        )
        if tool is None:
            raise _not_found()
        version = await self.session.scalar(
            select(ToolVersion)
            .where(ToolVersion.tool_id == tool.id, ToolVersion.version_no == version_no)
            .with_for_update()
        )
        if version is None or version.version_status != "draft":
            raise _conflict("TOOL_VERSION_NOT_PUBLISHABLE")
        if not bool(version.evaluation_report.get("passed")):
            raise _conflict("TOOL_EVALUATION_REQUIRED")
        old = list(
            (
                await self.session.scalars(
                    select(ToolVersion).where(
                        ToolVersion.tool_id == tool.id,
                        ToolVersion.version_status == "published",
                    )
                )
            ).all()
        )
        for item in old:
            item.version_status = "retired"
        version.version_status = "published"
        version.published_at = utc_now()
        tool.tool_status = "active"
        record_admin_operation(
            self.session,
            access,
            action="tool.version.publish",
            target_type="tool",
            target_no=tool.tool_code,
            after={"version": version_no},
        )
        await self.session.commit()
        return _tool_view(tool, await self._tool_versions(tool.id))

    async def rollback_tool(
        self, access: AdminAccess, tool_code: str, target_version_no: int
    ) -> ToolView:
        tool = await self.session.scalar(
            select(ToolDefinition)
            .where(ToolDefinition.tool_code == tool_code)
            .with_for_update()
        )
        if tool is None:
            raise _not_found()
        versions = list(
            (
                await self.session.scalars(
                    select(ToolVersion)
                    .where(ToolVersion.tool_id == tool.id)
                    .order_by(ToolVersion.version_no)
                    .with_for_update()
                )
            ).all()
        )
        target = next(
            (item for item in versions if item.version_no == target_version_no), None
        )
        current = next(
            (item for item in versions if item.version_status == "published"), None
        )
        if (
            target is None
            or current is None
            or target.id == current.id
            or target.version_status != "retired"
            or not bool(target.evaluation_report.get("passed"))
        ):
            raise _conflict("TOOL_VERSION_NOT_ROLLBACKABLE")
        current.version_status = "retired"
        current.version += 1
        target.version_status = "published"
        target.published_at = utc_now()
        target.version += 1
        tool.tool_status = "active"
        tool.version += 1
        record_admin_operation(
            self.session,
            access,
            action="tool.version.rollback",
            target_type="tool",
            target_no=tool.tool_code,
            before={"published_version": current.version_no},
            after={"published_version": target.version_no},
        )
        await self.session.commit()
        return _tool_view(tool, versions)

    async def _skill_view(self, item: SkillDefinition) -> SkillView:
        versions = list(
            (
                await self.session.scalars(
                    select(SkillVersion)
                    .where(SkillVersion.skill_id == item.id)
                    .order_by(SkillVersion.version_no)
                )
            ).all()
        )
        return SkillView(
            skill_id=item.skill_no,
            skill_code=item.skill_code,
            display_name=item.display_name,
            status=item.skill_status,
            latest_version=versions[-1].version_no if versions else None,
            published_version=next(
                (v.version_no for v in reversed(versions) if v.version_status == "published"), None
            ),
        )

    async def _tool_versions(self, tool_id: int) -> list[ToolVersion]:
        return list(
            (
                await self.session.scalars(
                    select(ToolVersion)
                    .where(ToolVersion.tool_id == tool_id)
                    .order_by(ToolVersion.version_no)
                )
            ).all()
        )


def _tool_view(item: ToolDefinition, versions: list[ToolVersion] | None = None) -> ToolView:
    versions = versions or []
    effective = next(
        (version for version in reversed(versions) if version.version_status == "published"),
        versions[-1] if versions else None,
    )
    return ToolView(
        tool_code=item.tool_code,
        server_code=item.server_code,
        risk_level=item.risk_level,
        input_schema=effective.input_schema if effective else None,
        output_schema=effective.output_schema if effective else None,
        status=item.tool_status,
        latest_version=versions[-1].version_no if versions else None,
        published_version=next(
            (v.version_no for v in reversed(versions) if v.version_status == "published"), None
        ),
        versions=[
            ToolVersionSummary(
                version_no=version.version_no,
                status=version.version_status,
                input_schema=version.input_schema,
                output_schema=version.output_schema,
                evaluation_report=version.evaluation_report,
                published_at=version.published_at,
            )
            for version in reversed(versions)
        ],
    )


def _kill_switch_view(item: RuntimeKillSwitch) -> KillSwitchView:
    return KillSwitchView(
        switch_id=item.switch_no,
        target_type=item.target_type,
        target_code=item.target_code,
        is_active=item.is_active,
        reason=item.reason,
        version=item.version,
    )


def _not_found() -> ApplicationError:
    return ApplicationError(
        status=404, code="RESOURCE_NOT_FOUND", title="Resource not found", detail="AI 资源不存在。"
    )


def _conflict(code: str) -> ApplicationError:
    return ApplicationError(
        status=409, code=code, title="AI resource conflict", detail="AI 资源状态冲突。"
    )
