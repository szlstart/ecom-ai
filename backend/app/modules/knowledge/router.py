from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import select

from app.api.dependencies import DatabaseSession, PostgresSession, get_security_service
from app.api.schemas import Envelope
from app.core.config import get_settings
from app.core.security import SecurityService
from app.modules.identity.router import _no_store
from app.modules.knowledge.admin_service import KnowledgeAdminService
from app.modules.knowledge.document_service import KnowledgeDocumentService
from app.modules.knowledge.indexing import cancel_index_job, reconcile_index_job
from app.modules.knowledge.mcp_registry import MCP_SERVERS
from app.modules.knowledge.publication_service import AiPublicationService
from app.modules.knowledge.schemas import (
    AgentList,
    AgentSkillBindingCreate,
    AgentVersionCreate,
    AgentView,
    KillSwitchChange,
    KillSwitchList,
    KillSwitchView,
    KnowledgeDocumentCreate,
    KnowledgeDocumentList,
    KnowledgeDocumentView,
    KnowledgeIndexJobView,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
    McpServerList,
    McpServerView,
    SkillDefinitionCreate,
    SkillList,
    SkillToolBindingCreate,
    SkillVersionCreate,
    SkillView,
    ToolCreate,
    ToolList,
    ToolVersionCreate,
    ToolView,
    VersionBindingView,
)
from app.modules.knowledge.service import KnowledgeService
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission
from app.modules.rbac.schemas import ApprovalRequiredView
from app.modules.system.models import AdminBatchJob

router = APIRouter(prefix="/admin/knowledge", tags=["knowledge-administration"])

ai_router = APIRouter(prefix="/admin/ai", tags=["ai-governance"])


@router.get(
    "/documents",
    response_model=Envelope[KnowledgeDocumentList],
    operation_id="AdminKnowledgeDocument_List",
)
async def list_documents(
    response: Response,
    session: DatabaseSession,
    postgres: PostgresSession,
    access: Annotated[AdminAccess, require_admin_permission("knowledge:read")],
) -> Envelope[KnowledgeDocumentList]:
    result = await KnowledgeDocumentService(session, postgres).list(access)
    _no_store(response)
    return Envelope(data=KnowledgeDocumentList(items=result))


@router.post(
    "/documents",
    response_model=Envelope[KnowledgeDocumentView],
    operation_id="AdminKnowledgeDocument_Create",
)
async def create_document(
    payload: KnowledgeDocumentCreate,
    response: Response,
    session: DatabaseSession,
    postgres: PostgresSession,
    access: Annotated[AdminAccess, require_admin_permission("knowledge:manage")],
) -> Envelope[KnowledgeDocumentView]:
    result = await KnowledgeDocumentService(session, postgres).create(access, payload)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/documents/{document_id}/publications",
    response_model=Envelope[KnowledgeDocumentView],
    operation_id="AdminKnowledgeIndex_Create",
)
async def publish_document(
    document_id: str,
    response: Response,
    session: DatabaseSession,
    postgres: PostgresSession,
    access: Annotated[AdminAccess, require_admin_permission("knowledge:publish")],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
    ],
) -> Envelope[KnowledgeDocumentView]:
    result = await KnowledgeDocumentService(session, postgres).publish(
        access, document_id, idempotency_key
    )
    _no_store(response)
    return Envelope(data=result)


@router.delete(
    "/documents/{document_id}",
    response_model=Envelope[KnowledgeDocumentView],
    operation_id="AdminKnowledgeDocument_Delete",
)
async def withdraw_document(
    document_id: str,
    response: Response,
    session: DatabaseSession,
    postgres: PostgresSession,
    access: Annotated[AdminAccess, require_admin_permission("knowledge:manage")],
) -> Envelope[KnowledgeDocumentView]:
    result = await KnowledgeDocumentService(session, postgres).withdraw(access, document_id)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/index-jobs/{job_id}",
    response_model=Envelope[KnowledgeIndexJobView],
    operation_id="AdminKnowledgeIndex_Get",
)
async def get_index_job(
    job_id: str,
    response: Response,
    session: DatabaseSession,
    postgres: PostgresSession,
    access: Annotated[AdminAccess, require_admin_permission("knowledge:read")],
) -> Envelope[KnowledgeIndexJobView]:
    parent = await session.scalar(select(AdminBatchJob).where(AdminBatchJob.job_no == job_id))
    if parent is not None:
        access.require_scope(parent.scope_type, parent.scope_id)
    state = await reconcile_index_job(session, postgres, job_id)
    if state is None:
        from app.core.exceptions import ApplicationError

        raise ApplicationError(
            status=404,
            code="RESOURCE_NOT_FOUND",
            title="Resource not found",
            detail="未找到索引任务。",
        )
    _no_store(response)
    return Envelope(
        data=KnowledgeIndexJobView(
            job_id=state.job_no,
            command_job_id=state.command_job_no,
            status=state.status,
            progress=state.progress,
            error_code=state.error_code,
        )
    )


@router.post(
    "/index-jobs/{job_id}/cancellations",
    response_model=Envelope[KnowledgeIndexJobView],
    operation_id="AdminKnowledgeIndex_Cancel",
)
async def cancel_job(
    job_id: str,
    response: Response,
    session: DatabaseSession,
    postgres: PostgresSession,
    access: Annotated[AdminAccess, require_admin_permission("knowledge:manage")],
) -> Envelope[KnowledgeIndexJobView]:
    parent = await session.scalar(select(AdminBatchJob).where(AdminBatchJob.job_no == job_id))
    if parent is not None:
        access.require_scope(parent.scope_type, parent.scope_id)
    try:
        state = await cancel_index_job(session, postgres, job_id)
    except LookupError as exc:
        from app.core.exceptions import ApplicationError

        raise ApplicationError(
            status=404,
            code="RESOURCE_NOT_FOUND",
            title="Resource not found",
            detail="未找到索引任务。",
        ) from exc
    _no_store(response)
    return Envelope(
        data=KnowledgeIndexJobView(
            job_id=state.job_no,
            command_job_id=state.command_job_no,
            status=state.status,
            progress=state.progress,
            error_code=state.error_code,
        )
    )


@router.post(
    "/searches",
    response_model=Envelope[KnowledgeSearchResult],
    operation_id="AdminKnowledge_Search",
)
async def search_knowledge(
    payload: KnowledgeSearchRequest,
    response: Response,
    session: DatabaseSession,
    postgres: PostgresSession,
    access: Annotated[AdminAccess, require_admin_permission("knowledge:read")],
) -> Envelope[KnowledgeSearchResult]:
    result = await KnowledgeService(session, postgres).search(access, payload)
    _no_store(response)
    return Envelope(data=result)


@ai_router.get("/agents", response_model=Envelope[AgentList], operation_id="AdminAgent_List")
async def list_agents(
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_agents:read")],
) -> Envelope[AgentList]:
    _ = access
    result = await KnowledgeAdminService(session).agents()
    _no_store(response)
    return Envelope(data=result)


@ai_router.post(
    "/agents/{agent_id}/versions",
    response_model=Envelope[AgentView],
    operation_id="AdminAgentVersion_Create",
)
async def create_agent_version(
    agent_id: str,
    payload: AgentVersionCreate,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_agents:manage")],
) -> Envelope[AgentView]:
    result = await KnowledgeAdminService(session).create_agent_version(access, agent_id, payload)
    _no_store(response)
    return Envelope(data=result)


@ai_router.post(
    "/agents/{agent_id}/versions/{version_no}/skill-bindings",
    response_model=Envelope[VersionBindingView],
    operation_id="AdminAgentSkillBinding_Create",
)
async def bind_agent_skill(
    agent_id: str,
    version_no: int,
    payload: AgentSkillBindingCreate,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_agents:manage")],
) -> Envelope[VersionBindingView]:
    result = await KnowledgeAdminService(session).bind_skill(
        access, agent_id, version_no, payload
    )
    _no_store(response)
    return Envelope(data=result)


@ai_router.post(
    "/agents/{agent_id}/versions/{version_no}/publications",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[ApprovalRequiredView],
    operation_id="AdminAgentVersion_Publish",
)
async def publish_agent_version(
    agent_id: str,
    version_no: int,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_agents:publish")],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
    ],
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> Envelope[ApprovalRequiredView]:
    result = await AiPublicationService(session, get_settings(), security).request_agent(
        access, agent_id, version_no, idempotency_key
    )
    _no_store(response)
    return Envelope(data=result)


@ai_router.get(
    "/mcp-servers",
    response_model=Envelope[McpServerList],
    operation_id="AdminMcpServer_List",
)
async def list_mcp_servers(
    response: Response,
    access: Annotated[AdminAccess, require_admin_permission("ai_policies:read")],
) -> Envelope[McpServerList]:
    _ = access
    _no_store(response)
    return Envelope(
        data=McpServerList(
            items=[
                McpServerView(
                    server_code=item.server_code,
                    tools=sorted(item.tools),
                    timeout_seconds=item.default_timeout_seconds,
                )
                for item in MCP_SERVERS.values()
            ]
        )
    )


@ai_router.get(
    "/kill-switches",
    response_model=Envelope[KillSwitchList],
    operation_id="AdminAiKillSwitch_List",
)
async def list_kill_switches(
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_policies:read")],
) -> Envelope[KillSwitchList]:
    _ = access
    result = await KnowledgeAdminService(session).kill_switches()
    _no_store(response)
    return Envelope(data=result)


@ai_router.post(
    "/kill-switches/{target_type}/{target_code}/activations",
    response_model=Envelope[KillSwitchView],
    operation_id="AdminAiKillSwitch_Activate",
)
async def activate_kill_switch(
    target_type: str,
    target_code: str,
    payload: KillSwitchChange,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_runtime:kill")],
) -> Envelope[KillSwitchView]:
    result = await KnowledgeAdminService(session).change_kill_switch(
        access,
        target_type=target_type,
        target_code=target_code,
        active=True,
        reason=payload.reason,
    )
    _no_store(response)
    return Envelope(data=result)


@ai_router.post(
    "/kill-switches/{target_type}/{target_code}/deactivations",
    response_model=Envelope[KillSwitchView],
    operation_id="AdminAiKillSwitch_Deactivate",
)
async def deactivate_kill_switch(
    target_type: str,
    target_code: str,
    payload: KillSwitchChange,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_runtime:kill")],
) -> Envelope[KillSwitchView]:
    result = await KnowledgeAdminService(session).change_kill_switch(
        access,
        target_type=target_type,
        target_code=target_code,
        active=False,
        reason=payload.reason,
    )
    _no_store(response)
    return Envelope(data=result)


@ai_router.get("/skills", response_model=Envelope[SkillList], operation_id="AdminSkill_List")
async def list_skills(
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_skills:read")],
) -> Envelope[SkillList]:
    _ = access
    result = await KnowledgeAdminService(session).skills()
    _no_store(response)
    return Envelope(data=result)


@ai_router.post("/skills", response_model=Envelope[SkillView], operation_id="AdminSkill_Create")
async def create_skill(
    payload: SkillDefinitionCreate,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_skills:manage")],
) -> Envelope[SkillView]:
    result = await KnowledgeAdminService(session).create_skill(access, payload)
    _no_store(response)
    return Envelope(data=result)


@ai_router.post(
    "/skills/{skill_id}/versions",
    response_model=Envelope[SkillView],
    operation_id="AdminSkill_VersionCreate",
)
async def create_skill_version(
    skill_id: str,
    payload: SkillVersionCreate,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_skills:manage")],
) -> Envelope[SkillView]:
    result = await KnowledgeAdminService(session).create_version(access, skill_id, payload)
    _no_store(response)
    return Envelope(data=result)


@ai_router.post(
    "/skills/{skill_id}/versions/{version_no}/tool-bindings",
    response_model=Envelope[VersionBindingView],
    operation_id="AdminSkillToolBinding_Create",
)
async def bind_skill_tool(
    skill_id: str,
    version_no: int,
    payload: SkillToolBindingCreate,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_skills:manage")],
) -> Envelope[VersionBindingView]:
    result = await KnowledgeAdminService(session).bind_tool(
        access, skill_id, version_no, payload
    )
    _no_store(response)
    return Envelope(data=result)


@ai_router.post(
    "/skills/{skill_id}/versions/{version_no}/publications",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[ApprovalRequiredView],
    operation_id="AdminSkill_Publish",
)
async def publish_skill(
    skill_id: str,
    version_no: int,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_skills:publish")],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
    ],
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> Envelope[ApprovalRequiredView]:
    result = await AiPublicationService(session, get_settings(), security).request_skill(
        access, skill_id, version_no, idempotency_key
    )
    _no_store(response)
    return Envelope(data=result)


@ai_router.get("/tools", response_model=Envelope[ToolList], operation_id="AdminTool_List")
async def list_tools(
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_tools:read")],
) -> Envelope[ToolList]:
    _ = access
    result = await KnowledgeAdminService(session).tools()
    _no_store(response)
    return Envelope(data=result)


@ai_router.post("/tools", response_model=Envelope[ToolView], operation_id="AdminTool_Create")
async def create_tool(
    payload: ToolCreate,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_tools:manage")],
) -> Envelope[ToolView]:
    result = await KnowledgeAdminService(session).create_tool(access, payload)
    _no_store(response)
    return Envelope(data=result)


@ai_router.post(
    "/tools/{tool_code}/versions",
    response_model=Envelope[ToolView],
    operation_id="AdminTool_VersionCreate",
)
async def create_tool_version(
    tool_code: str,
    payload: ToolVersionCreate,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_tools:manage")],
) -> Envelope[ToolView]:
    result = await KnowledgeAdminService(session).create_tool_version(access, tool_code, payload)
    _no_store(response)
    return Envelope(data=result)


@ai_router.post(
    "/tools/{tool_code}/versions/{version_no}/publications",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[ApprovalRequiredView],
    operation_id="AdminTool_Publish",
)
async def publish_tool(
    tool_code: str,
    version_no: int,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_tools:publish")],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
    ],
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> Envelope[ApprovalRequiredView]:
    result = await AiPublicationService(session, get_settings(), security).request_tool(
        access, tool_code, version_no, idempotency_key
    )
    _no_store(response)
    return Envelope(data=result)
