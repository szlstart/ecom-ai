from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import IdempotencyKey, UserContext
from app.api.schemas import Envelope
from app.modules.agent_runtime.dependencies import (
    AgentApprovalServiceDependency,
    AgentRuntimeServiceDependency,
    AiPrivacyServiceDependency,
)
from app.modules.agent_runtime.privacy_schemas import (
    AiCleanupTaskView,
    AiMemoryActivationRequest,
    AiMemoryDeleteRequest,
    AiMemoryList,
    AiMemoryRevisionRequest,
    AiMemoryView,
    AiPersonalizationDisableRequest,
)
from app.modules.agent_runtime.schemas import (
    AgentApprovalDecisionRequest,
    AgentApprovalView,
    AgentConsentGrantRequest,
    AgentConsentList,
    AgentConsentView,
    AgentRunView,
)
from app.modules.identity.router import _etag, _expected_version, _no_store

router = APIRouter(tags=["agent-runtime"])


@router.get(
    "/users/me/ai-memory-items",
    response_model=Envelope[AiMemoryList],
    operation_id="AiMemory_ListMine",
)
async def list_ai_memory_items(
    response: Response,
    context: UserContext,
    service: AiPrivacyServiceDependency,
    namespace: Annotated[str | None, Query(pattern=r"^(exclusive|store)$")] = None,
    store_id: Annotated[str | None, Query(max_length=64)] = None,
    memory_type: Annotated[str | None, Query(max_length=32)] = None,
    memory_status: Annotated[
        str | None,
        Query(pattern=r"^(candidate|active|superseded|revoked|expired|deleted)$"),
    ] = None,
) -> Envelope[AiMemoryList]:
    result = await service.list_memories(
        context.user,
        namespace=namespace,
        store_no=store_id,
        memory_type=memory_type,
        status=memory_status,
    )
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/users/me/ai-memory-items/{memory_id}/activations",
    response_model=Envelope[AiMemoryView],
    operation_id="AiMemory_Activate",
)
async def activate_ai_memory_item(
    memory_id: str,
    payload: AiMemoryActivationRequest,
    response: Response,
    context: UserContext,
    service: AiPrivacyServiceDependency,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AiMemoryView]:
    result = await service.activate_memory(
        context.user, memory_id, payload, _expected_version(if_match)
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/users/me/ai-memory-items/{memory_id}/revisions",
    response_model=Envelope[AiMemoryView],
    status_code=status.HTTP_201_CREATED,
    operation_id="AiMemory_Revise",
)
async def revise_ai_memory_item(
    memory_id: str,
    payload: AiMemoryRevisionRequest,
    response: Response,
    context: UserContext,
    service: AiPrivacyServiceDependency,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AiMemoryView]:
    result = await service.revise_memory(
        context.user, memory_id, payload, _expected_version(if_match)
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.delete(
    "/users/me/ai-memory-items/{memory_id}",
    response_model=Envelope[AiCleanupTaskView],
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="AiMemory_Delete",
)
async def delete_ai_memory_item(
    memory_id: str,
    payload: AiMemoryDeleteRequest,
    response: Response,
    context: UserContext,
    service: AiPrivacyServiceDependency,
    idempotency_key: IdempotencyKey,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AiCleanupTaskView]:
    result = await service.delete_memory(
        context.user, memory_id, payload, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/users/me/ai-personalization/disable-all",
    response_model=Envelope[AiCleanupTaskView],
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="AiPersonalization_DisableAll",
)
async def disable_all_ai_personalization(
    payload: AiPersonalizationDisableRequest,
    response: Response,
    context: UserContext,
    service: AiPrivacyServiceDependency,
    idempotency_key: IdempotencyKey,
) -> Envelope[AiCleanupTaskView]:
    result = await service.disable_all(context.user, payload, idempotency_key)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/users/me/ai-cleanup-tasks/{task_id}",
    response_model=Envelope[AiCleanupTaskView],
    operation_id="CleanupTask_GetMine",
)
async def get_ai_cleanup_task(
    task_id: str,
    response: Response,
    context: UserContext,
    service: AiPrivacyServiceDependency,
) -> Envelope[AiCleanupTaskView]:
    result = await service.get_cleanup(context.user, task_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/users/me/ai-cleanup-tasks/{task_id}/retries",
    response_model=Envelope[AiCleanupTaskView],
    operation_id="CleanupTask_RetryMine",
)
async def retry_ai_cleanup_task(
    task_id: str,
    response: Response,
    context: UserContext,
    service: AiPrivacyServiceDependency,
    idempotency_key: IdempotencyKey,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AiCleanupTaskView]:
    result = await service.retry_cleanup(
        context.user, task_id, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


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


@router.get(
    "/agent-tool-approvals/{approval_id}",
    response_model=Envelope[AgentApprovalView],
    operation_id="AgentToolApproval_GetMine",
)
async def get_agent_tool_approval(
    approval_id: str,
    response: Response,
    context: UserContext,
    service: AgentApprovalServiceDependency,
) -> Envelope[AgentApprovalView]:
    result = await service.get(context.user, approval_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/agent-tool-approvals/{approval_id}/decisions",
    response_model=Envelope[AgentApprovalView],
    operation_id="AgentToolApproval_DecideMine",
)
async def decide_agent_tool_approval(
    approval_id: str,
    payload: AgentApprovalDecisionRequest,
    response: Response,
    context: UserContext,
    service: AgentApprovalServiceDependency,
    idempotency_key: IdempotencyKey,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AgentApprovalView]:
    result = await service.decide(
        context.user,
        approval_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)
