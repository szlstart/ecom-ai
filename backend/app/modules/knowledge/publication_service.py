from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.security import SecurityService
from app.modules.agent_runtime.delegation import (
    MultiAgentEvaluationReport,
    MultiAgentReleaseGate,
    MultiAgentRoutingPolicy,
)
from app.modules.agent_runtime.models import AgentDefinition, AgentVersion
from app.modules.knowledge.models import (
    AgentSkillBinding,
    SkillDefinition,
    SkillToolBinding,
    SkillVersion,
    ToolDefinition,
    ToolVersion,
)
from app.modules.rbac.approval_service import AdminApprovalRequestService, ApprovalRequestSpec
from app.modules.rbac.dependencies import AdminAccess
from app.modules.rbac.schemas import ApprovalRequiredView


class AiPublicationService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        security: SecurityService,
    ) -> None:
        self.session = session
        self.settings = settings
        self.security = security

    async def request_agent(
        self,
        access: AdminAccess,
        agent_no: str,
        version_no: int,
        idempotency_key: str,
    ) -> ApprovalRequiredView:
        row = (
            await self.session.execute(
                select(AgentDefinition, AgentVersion)
                .join(AgentVersion, AgentVersion.agent_id == AgentDefinition.id)
                .where(
                    AgentDefinition.agent_no == agent_no,
                    AgentVersion.version_no == version_no,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None or row[1].version_status != "draft":
            raise _not_publishable()
        agent, version = row
        evaluation = version.policy_config.get("evaluation_report")
        if not isinstance(evaluation, dict) or not bool(evaluation.get("passed")):
            raise _evaluation_required()
        if not multi_agent_policy_is_publishable(version.policy_config):
            raise _evaluation_required()
        skill_versions = list(
            (
                await self.session.scalars(
                    select(SkillVersion)
                    .join(
                        AgentSkillBinding,
                        AgentSkillBinding.skill_version_id == SkillVersion.id,
                    )
                    .where(
                        AgentSkillBinding.agent_version_id == version.id,
                        AgentSkillBinding.binding_status == "active",
                        SkillVersion.version_status == "published",
                    )
                )
            ).all()
        )
        bound_tools: set[str] = set()
        for skill_version in skill_versions:
            policies = await self._tool_policies(skill_version.id)
            denied = {
                tool.tool_code
                for binding, tool, _ in policies
                if binding.permission_effect == "deny"
            }
            bound_tools.update(
                tool.tool_code
                for binding, tool, _ in policies
                if binding.permission_effect == "allow" and tool.tool_code not in denied
            )
        if not set(version.tool_allowlist) <= bound_tools:
            raise _dependency_invalid()
        return await self._request(
            access,
            action_code="ai.agent.publish.v1",
            target_type="ai_agent_version",
            target_no=f"{agent.agent_no}:v{version.version_no}",
            command_payload={"agent_id": agent.agent_no, "version_no": version.version_no},
            display_snapshot={
                "agent_code": agent.agent_code,
                "version_no": version.version_no,
                "model_profile": version.model_profile,
                "tool_count": len(version.tool_allowlist),
                "impact": "切换新会话 Run 使用的 Agent Version，进行中 Run 保持原版本",
                "evaluation_passed": True,
            },
            resource_versions={"definition": agent.version, "agent_version": version.version},
            idempotency_key=idempotency_key,
        )

    async def request_skill(
        self,
        access: AdminAccess,
        skill_no: str,
        version_no: int,
        idempotency_key: str,
    ) -> ApprovalRequiredView:
        row = (
            await self.session.execute(
                select(SkillDefinition, SkillVersion)
                .join(SkillVersion, SkillVersion.skill_id == SkillDefinition.id)
                .where(
                    SkillDefinition.skill_no == skill_no,
                    SkillVersion.version_no == version_no,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None or row[1].version_status != "draft":
            raise _not_publishable()
        skill, version = row
        if not bool(version.evaluation_report.get("passed")):
            raise _evaluation_required()
        if not _schemas_are_safe(version.input_schema, version.output_schema):
            raise _dependency_invalid()
        policies = await self._tool_policies(version.id)
        if any(
            tool_version.version_status != "published"
            or tool.tool_status != "active"
            or (
                binding.permission_effect == "allow"
                and tool.risk_level != "read"
                and binding.confirmation_policy == "none"
            )
            for binding, tool, tool_version in policies
        ):
            raise _dependency_invalid()
        return await self._request(
            access,
            action_code="ai.skill.publish.v1",
            target_type="ai_skill_version",
            target_no=f"{skill.skill_no}:v{version.version_no}",
            command_payload={"skill_id": skill.skill_no, "version_no": version.version_no},
            display_snapshot={
                "skill_code": skill.skill_code,
                "version_no": version.version_no,
                "impact": "切换新 Run 使用的 Skill 发布版本，历史版本保持不可变",
                "evaluation_passed": True,
            },
            resource_versions={"definition": skill.version, "skill_version": version.version},
            idempotency_key=idempotency_key,
        )

    async def request_tool(
        self,
        access: AdminAccess,
        tool_code: str,
        version_no: int,
        idempotency_key: str,
    ) -> ApprovalRequiredView:
        row = (
            await self.session.execute(
                select(ToolDefinition, ToolVersion)
                .join(ToolVersion, ToolVersion.tool_id == ToolDefinition.id)
                .where(
                    ToolDefinition.tool_code == tool_code,
                    ToolVersion.version_no == version_no,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None or row[1].version_status != "draft":
            raise _not_publishable()
        tool, version = row
        if not bool(version.evaluation_report.get("passed")):
            raise _evaluation_required()
        if not _schemas_are_safe(version.input_schema, version.output_schema):
            raise _dependency_invalid()
        return await self._request(
            access,
            action_code="ai.tool.publish.v1",
            target_type="ai_tool_version",
            target_no=f"{tool.tool_code}:v{version.version_no}",
            command_payload={"tool_code": tool.tool_code, "version_no": version.version_no},
            display_snapshot={
                "tool_code": tool.tool_code,
                "server_code": tool.server_code,
                "risk_level": tool.risk_level,
                "version_no": version.version_no,
                "impact": "切换新 Run 可绑定的 MCP Tool Contract，历史版本保持不可变",
                "evaluation_passed": True,
            },
            resource_versions={"definition": tool.version, "tool_version": version.version},
            idempotency_key=idempotency_key,
        )

    async def request_tool_rollback(
        self,
        access: AdminAccess,
        tool_code: str,
        target_version_no: int,
        idempotency_key: str,
    ) -> ApprovalRequiredView:
        rows = list(
            (
                await self.session.execute(
                    select(ToolDefinition, ToolVersion)
                    .join(ToolVersion, ToolVersion.tool_id == ToolDefinition.id)
                    .where(ToolDefinition.tool_code == tool_code)
                    .order_by(ToolVersion.version_no)
                    .with_for_update()
                )
            ).all()
        )
        if not rows:
            raise _not_publishable()
        tool = rows[0][0]
        versions = [row[1] for row in rows]
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
            or not _schemas_are_safe(target.input_schema, target.output_schema)
        ):
            raise _not_publishable()
        return await self._request(
            access,
            action_code="ai.tool.rollback.v1",
            target_type="ai_tool_version",
            target_no=f"{tool.tool_code}:v{target.version_no}",
            command_payload={
                "tool_code": tool.tool_code,
                "target_version_no": target.version_no,
                "current_version_no": current.version_no,
            },
            display_snapshot={
                "tool_code": tool.tool_code,
                "server_code": tool.server_code,
                "risk_level": tool.risk_level,
                "from_version_no": current.version_no,
                "to_version_no": target.version_no,
                "impact": "原子切换新 Run 使用的 Tool Contract，历史版本正文保持不可变",
                "evaluation_passed": True,
            },
            resource_versions={
                "definition": tool.version,
                "target_tool_version": target.version,
                "current_tool_version": current.version,
            },
            idempotency_key=idempotency_key,
        )

    async def _request(
        self,
        access: AdminAccess,
        *,
        action_code: str,
        target_type: str,
        target_no: str,
        command_payload: dict[str, object],
        display_snapshot: dict[str, object],
        resource_versions: dict[str, object],
        idempotency_key: str,
    ) -> ApprovalRequiredView:
        return await AdminApprovalRequestService(self.session, self.security).create(
            access,
            ApprovalRequestSpec(
                approval_type="ai_release",
                action_code=action_code,
                target_type=target_type,
                target_no=target_no,
                scope_type="platform",
                scope_id=0,
                command_payload=command_payload,
                display_snapshot=display_snapshot,
                resource_versions=resource_versions,
                policy_snapshot={
                    "policy": "ai_release_dual_control_v1",
                    "required_approval_count": 2,
                    "initiator_cannot_approve": True,
                    "initiator_assurance_level": access.context.session.assurance_level,
                    "initiator_authenticated_at": (
                        access.context.session.authenticated_at.isoformat()
                    ),
                },
                required_approval_count=2,
                reason="发布已通过评估的 AI 不可变版本",
            ),
            idempotency_key=idempotency_key,
            ttl_minutes=self.settings.admin_approval_ttl_minutes,
        )

    async def _tool_policies(
        self, skill_version_id: int
    ) -> list[tuple[SkillToolBinding, ToolDefinition, ToolVersion]]:
        rows = (
            await self.session.execute(
                select(SkillToolBinding, ToolDefinition, ToolVersion)
                .join(ToolVersion, ToolVersion.id == SkillToolBinding.tool_version_id)
                .join(ToolDefinition, ToolDefinition.id == ToolVersion.tool_id)
                .where(SkillToolBinding.skill_version_id == skill_version_id)
            )
        ).all()
        return [(binding, tool, tool_version) for binding, tool, tool_version in rows]


def _not_publishable() -> ApplicationError:
    return ApplicationError(
        status=409,
        code="AI_VERSION_NOT_PUBLISHABLE",
        title="AI version conflict",
        detail="AI 版本不存在或当前不可发布。",
    )


def _evaluation_required() -> ApplicationError:
    return ApplicationError(
        status=409,
        code="AI_EVALUATION_REQUIRED",
        title="AI evaluation required",
        detail="安全与质量评估尚未通过，不能发起发布审批。",
    )


def _dependency_invalid() -> ApplicationError:
    return ApplicationError(
        status=409,
        code="AI_VERSION_DEPENDENCY_INVALID",
        title="AI dependency invalid",
        detail="固定版本绑定、Tool 状态、确认策略或 Allowlist 不满足发布要求。",
    )


def _schemas_are_safe(
    input_schema: dict[str, object], output_schema: dict[str, object]
) -> bool:
    return (
        input_schema.get("type") == "object"
        and input_schema.get("additionalProperties") is False
        and output_schema.get("type") == "object"
    )


def multi_agent_policy_is_publishable(policy_config: dict[str, object]) -> bool:
    raw_policy = policy_config.get("multi_agent")
    if not isinstance(raw_policy, dict) or raw_policy.get("enabled") is not True:
        return True
    routing = MultiAgentRoutingPolicy.from_agent_version_policy(policy_config)
    evaluation = policy_config.get("evaluation_report")
    raw_report = evaluation.get("multi_agent") if isinstance(evaluation, dict) else None
    if not routing.enabled or not isinstance(evaluation, dict) or not isinstance(raw_report, dict):
        return False
    if evaluation.get("report_id") != raw_policy.get("evaluation_report_id"):
        return False
    approved_intents = raw_report.get("approved_intents")
    if (
        not isinstance(approved_intents, list)
        or frozenset(item for item in approved_intents if isinstance(item, str))
        != routing.approved_intents
    ):
        return False
    integer_fields = (
        "sample_size",
        "baseline_successes",
        "candidate_successes",
        "candidate_safety_violations",
        "candidate_p95_latency_ms",
        "approved_p95_latency_ms",
    )
    if any(not _is_integer(raw_report.get(name)) for name in integer_fields):
        return False
    cost_fields = ("candidate_average_cost", "approved_average_cost")
    if any(not _is_number(raw_report.get(name)) for name in cost_fields):
        return False
    golden_set_version = raw_report.get("golden_set_version")
    if not isinstance(golden_set_version, str):
        return False
    report = MultiAgentEvaluationReport(
        golden_set_version=golden_set_version,
        sample_size=int(raw_report["sample_size"]),
        baseline_successes=int(raw_report["baseline_successes"]),
        candidate_successes=int(raw_report["candidate_successes"]),
        candidate_safety_violations=int(raw_report["candidate_safety_violations"]),
        candidate_p95_latency_ms=int(raw_report["candidate_p95_latency_ms"]),
        approved_p95_latency_ms=int(raw_report["approved_p95_latency_ms"]),
        candidate_average_cost=float(raw_report["candidate_average_cost"]),
        approved_average_cost=float(raw_report["approved_average_cost"]),
    )
    return MultiAgentReleaseGate().evaluate(report).approved


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
