from typing import Annotated

from fastapi import APIRouter, Header, Query, Response

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.events.dependencies import DeadLetterServiceDependency
from app.modules.events.schemas import (
    DeadLetterIgnoreRequest,
    DeadLetterList,
    DeadLetterReplayPreview,
    DeadLetterReplayRequest,
    DeadLetterView,
)
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission
from app.modules.rbac.schemas import ApprovalRequiredView

router = APIRouter(prefix="/admin/dead-letter-events", tags=["event-administration"])


@router.get("", response_model=Envelope[DeadLetterList], operation_id="AdminDeadLetter_List")
async def list_dead_letters(
    response: Response,
    service: DeadLetterServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("events:read")],
    dead_status: Annotated[str | None, Query(alias="status", max_length=16)] = None,
    event_type: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[DeadLetterList]:
    result = await service.list(access, status=dead_status, event_type=event_type, limit=limit)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/{dead_letter_id}",
    response_model=Envelope[DeadLetterView],
    operation_id="AdminDeadLetter_Get",
)
async def get_dead_letter(
    dead_letter_id: str,
    response: Response,
    service: DeadLetterServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("events:read")],
) -> Envelope[DeadLetterView]:
    result = await service.detail(access, dead_letter_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/{dead_letter_id}/replay-previews",
    response_model=Envelope[DeadLetterReplayPreview],
    operation_id="AdminDeadLetter_Preview",
)
async def preview_dead_letter_replay(
    dead_letter_id: str,
    response: Response,
    service: DeadLetterServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("events:operate")],
) -> Envelope[DeadLetterReplayPreview]:
    result = await service.preview(_access, dead_letter_id)
    response.headers["ETag"] = _etag(result.dead_letter.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/{dead_letter_id}/replays",
    response_model=Envelope[ApprovalRequiredView],
    status_code=202,
    operation_id="AdminDeadLetter_Replay",
)
async def replay_dead_letter(
    dead_letter_id: str,
    payload: DeadLetterReplayRequest,
    response: Response,
    service: DeadLetterServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("events:operate")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[ApprovalRequiredView]:
    result = await service.request_replay(
        access,
        dead_letter_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/{dead_letter_id}/ignore",
    response_model=Envelope[DeadLetterView],
    operation_id="AdminDeadLetter_Ignore",
)
async def ignore_dead_letter(
    dead_letter_id: str,
    payload: DeadLetterIgnoreRequest,
    response: Response,
    service: DeadLetterServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("events:operate")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[DeadLetterView]:
    result = await service.ignore(
        access,
        dead_letter_id,
        payload,
        _expected_version(if_match),
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)
