from fastapi import APIRouter, Response, status

from app.api.dependencies import IdempotencyKey, UserContext
from app.api.schemas import Envelope
from app.modules.agent_runtime.dependencies import AgentRuntimeServiceDependency
from app.modules.agent_runtime.schemas import (
    AgentConsentGrantRequest,
    AgentConsentList,
    AgentConsentView,
    AgentRunView,
)
from app.modules.identity.router import _no_store

router = APIRouter(tags=["agent-runtime"])


@router.get(
    "/users/me/agent-consents",
    response_model=Envelope[AgentConsentList],
    operation_id="AiConsent_ListMine",
)
async def list_consents(
    response: Response, context: UserContext, service: AgentRuntimeServiceDependency
) -> Envelope[AgentConsentList]:
    result = await service.list_consents(context.user)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/users/me/agent-consents",
    response_model=Envelope[AgentConsentView],
    status_code=status.HTTP_201_CREATED,
    operation_id="AiConsent_Grant",
)
async def grant_consent(
    payload: AgentConsentGrantRequest,
    response: Response,
    context: UserContext,
    service: AgentRuntimeServiceDependency,
    idempotency_key: IdempotencyKey,
) -> Envelope[AgentConsentView]:
    result = await service.grant_consent(context.user, payload, idempotency_key)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/users/me/agent-consents/{consent_id}",
    response_model=Envelope[AgentConsentView],
    operation_id="AiConsent_GetMine",
)
async def get_consent(
    consent_id: str,
    response: Response,
    context: UserContext,
    service: AgentRuntimeServiceDependency,
) -> Envelope[AgentConsentView]:
    result = next(
        (
            item
            for item in (await service.list_consents(context.user)).items
            if item.consent_id == consent_id
        ),
        None,
    )
    if result is None:
        from app.core.exceptions import ApplicationError

        raise ApplicationError(
            status=404, code="RESOURCE_NOT_FOUND", title="Resource not found", detail="授权不存在。"
        )
    _no_store(response)
    return Envelope(data=result)


def _consent_command(path: str, operation_id: str, command: str) -> None:
    async def endpoint(
        consent_id: str,
        response: Response,
        context: UserContext,
        service: AgentRuntimeServiceDependency,
    ) -> Envelope[AgentConsentView]:
        result = await service.consent_command(context.user, consent_id, command)
        _no_store(response)
        return Envelope(data=result)

    endpoint.__name__ = operation_id
    router.add_api_route(
        path,
        endpoint,
        methods=["POST"],
        response_model=Envelope[AgentConsentView],
        operation_id=operation_id,
    )


_consent_command("/users/me/agent-consents/{consent_id}/pauses", "AiConsent_Pause", "pause")
_consent_command("/users/me/agent-consents/{consent_id}/resumes", "AiConsent_Resume", "resume")
_consent_command("/users/me/agent-consents/{consent_id}/revocations", "AiConsent_Revoke", "revoke")


@router.get(
    "/agent-runs/{run_id}",
    response_model=Envelope[AgentRunView],
    operation_id="AgentRun_GetMine",
)
async def get_agent_run(
    run_id: str,
    response: Response,
    context: UserContext,
    service: AgentRuntimeServiceDependency,
) -> Envelope[AgentRunView]:
    result = await service.get(context.user, run_id)
    _no_store(response)
    return Envelope(data=result)
