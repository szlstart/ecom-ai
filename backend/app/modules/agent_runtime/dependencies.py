from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession, PostgresSession, get_security_service
from app.core.config import get_settings
from app.core.security import SecurityService
from app.modules.agent_runtime.approval_service import AgentApprovalService
from app.modules.agent_runtime.privacy_service import AiPrivacyService
from app.modules.agent_runtime.service import AgentRuntimeService
from app.modules.knowledge.embedding import embedding_provider


def get_agent_runtime_service(session: DatabaseSession) -> AgentRuntimeService:
    return AgentRuntimeService(session)


AgentRuntimeServiceDependency = Annotated[AgentRuntimeService, Depends(get_agent_runtime_service)]


def get_agent_approval_service(
    session: DatabaseSession,
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> AgentApprovalService:
    return AgentApprovalService(session, get_settings(), security)


AgentApprovalServiceDependency = Annotated[
    AgentApprovalService, Depends(get_agent_approval_service)
]


def get_ai_privacy_service(
    session: DatabaseSession,
    postgres: PostgresSession,
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> AiPrivacyService:
    return AiPrivacyService(session, postgres, security, embedding_provider(get_settings()))


AiPrivacyServiceDependency = Annotated[AiPrivacyService, Depends(get_ai_privacy_service)]
