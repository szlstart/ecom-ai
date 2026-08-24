from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession
from app.modules.agent_runtime.service import AgentRuntimeService


def get_agent_runtime_service(session: DatabaseSession) -> AgentRuntimeService:
    return AgentRuntimeService(session)


AgentRuntimeServiceDependency = Annotated[AgentRuntimeService, Depends(get_agent_runtime_service)]
