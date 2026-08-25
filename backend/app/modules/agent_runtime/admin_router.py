from typing import Annotated

from fastapi import APIRouter, Header, Response

from app.api.dependencies import DatabaseSession
from app.api.schemas import Envelope
from app.modules.agent_runtime.admin_service import AdminAgentRuntimeService
from app.modules.agent_runtime.schemas import AdminAgentRunCancelRequest, AdminAgentRunView
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission

router = APIRouter(prefix="/admin/ai/runs", tags=["admin-agent-runtime"])


@router.get(
    "/{run_id}",
    response_model=Envelope[AdminAgentRunView],
    operation_id="AdminAgentRun_Get",
)
async def get_agent_run(
    run_id: str,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_observability:read")],
) -> Envelope[AdminAgentRunView]:
    result = await AdminAgentRuntimeService(session).get(access, run_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/{run_id}/cancellations",
    response_model=Envelope[AdminAgentRunView],
    operation_id="AdminAgentRun_Kill",
)
async def cancel_agent_run(
    run_id: str,
    payload: AdminAgentRunCancelRequest,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("ai_runtime:kill")],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminAgentRunView]:
    result = await AdminAgentRuntimeService(session).cancel(
        access,
        run_id,
        payload.reason,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)
