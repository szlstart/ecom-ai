from typing import Annotated

from fastapi import APIRouter, Header, Response

from app.api.dependencies import DatabaseSession
from app.api.schemas import Envelope
from app.core.exceptions import ApplicationError
from app.modules.content.schemas import ContentCreate, ContentList, ContentUpdate, ContentView
from app.modules.content.service import ContentService
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission

router = APIRouter(prefix="/admin/content", tags=["admin-content"])


@router.get("", response_model=Envelope[ContentList], operation_id="AdminContent_List")
async def list_content(
    session: DatabaseSession,
    _access: Annotated[AdminAccess, require_admin_permission("content:read")],
) -> Envelope[ContentList]:
    return Envelope(data=await ContentService(session).list())


@router.post(
    "", response_model=Envelope[ContentView], status_code=201, operation_id="AdminContent_Create"
)
async def create_content(
    payload: ContentCreate,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("content:manage")],
) -> Envelope[ContentView]:
    return Envelope(data=await ContentService(session).create(access, payload))


@router.get("/{content_id}", response_model=Envelope[ContentView], operation_id="AdminContent_Get")
async def get_content(
    content_id: str,
    response: Response,
    session: DatabaseSession,
    _access: Annotated[AdminAccess, require_admin_permission("content:read")],
) -> Envelope[ContentView]:
    result = await ContentService(session).get(content_id)
    response.headers["ETag"] = f'"{result.version}"'
    return Envelope(data=result)


@router.put(
    "/{content_id}", response_model=Envelope[ContentView], operation_id="AdminContent_Update"
)
async def update_content(
    content_id: str,
    payload: ContentUpdate,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("content:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[ContentView]:
    result = await ContentService(session).update(access, content_id, payload, _etag(if_match))
    response.headers["ETag"] = f'"{result.version}"'
    return Envelope(data=result)


@router.post(
    "/{content_id}/versions/{version}/publish",
    response_model=Envelope[ContentView],
    operation_id="AdminContent_Publish",
)
async def publish_content(
    content_id: str,
    version: str,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("content:publish")],
) -> Envelope[ContentView]:
    return Envelope(data=await ContentService(session).publish(access, content_id, version))


@router.post(
    "/{content_id}/withdraw",
    response_model=Envelope[ContentView],
    operation_id="AdminContent_Withdraw",
)
async def withdraw_content(
    content_id: str,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("content:publish")],
) -> Envelope[ContentView]:
    return Envelope(data=await ContentService(session).withdraw(access, content_id))


def _etag(value: str | None) -> int:
    if value is None:
        raise ApplicationError(
            status=428,
            code="PRECONDITION_REQUIRED",
            title="If-Match required",
            detail="更新内容必须携带 If-Match。",
        )
    try:
        return int(value.strip('"'))
    except ValueError as exc:
        raise ApplicationError(
            status=400,
            code="IF_MATCH_INVALID",
            title="Invalid If-Match",
            detail="If-Match 格式无效。",
        ) from exc
