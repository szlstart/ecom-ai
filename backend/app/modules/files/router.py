from __future__ import annotations

from fastapi import APIRouter, Response, status
from fastapi.responses import RedirectResponse

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.files.auth import FileActorDependency, OptionalFileActorDependency
from app.modules.files.dependencies import FileServiceDependency
from app.modules.files.schemas import (
    FileMetadataView,
    FileUploadCompleteRequest,
    FileUploadPolicyView,
    FileUploadSessionCreateRequest,
    FileUploadSessionView,
)
from app.modules.identity.router import _no_store

router = APIRouter(tags=["files"])


@router.get(
    "/file-upload-policies/{purpose}",
    response_model=Envelope[FileUploadPolicyView],
    operation_id="FileUploadPolicy_Get",
)
async def get_upload_policy(
    purpose: str,
    response: Response,
    service: FileServiceDependency,
) -> Envelope[FileUploadPolicyView]:
    _no_store(response)
    return Envelope(data=service.policy(purpose))


@router.post(
    "/file-upload-sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[FileUploadSessionView],
    operation_id="FileUploadSession_Create",
)
async def create_upload_session(
    payload: FileUploadSessionCreateRequest,
    response: Response,
    service: FileServiceDependency,
    actor: FileActorDependency,
    idempotency_key: IdempotencyKey,
) -> Envelope[FileUploadSessionView]:
    _no_store(response)
    return Envelope(data=await service.create_upload(actor, payload, idempotency_key))


@router.get(
    "/file-upload-sessions/{upload_id}",
    response_model=Envelope[FileUploadSessionView],
    operation_id="FileUploadSession_Get",
)
async def get_upload_session(
    upload_id: str,
    response: Response,
    service: FileServiceDependency,
    actor: FileActorDependency,
) -> Envelope[FileUploadSessionView]:
    _no_store(response)
    return Envelope(data=await service.upload_status(actor, upload_id))


@router.post(
    "/file-upload-sessions/{upload_id}/complete",
    response_model=Envelope[FileUploadSessionView],
    operation_id="FileUploadSession_Complete",
)
async def complete_upload_session(
    upload_id: str,
    payload: FileUploadCompleteRequest,
    response: Response,
    service: FileServiceDependency,
    actor: FileActorDependency,
    idempotency_key: IdempotencyKey,
) -> Envelope[FileUploadSessionView]:
    _no_store(response)
    return Envelope(data=await service.complete_upload(actor, upload_id, payload, idempotency_key))


@router.delete(
    "/file-upload-sessions/{upload_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="FileUploadSession_Abort",
)
async def abort_upload_session(
    upload_id: str,
    service: FileServiceDependency,
    actor: FileActorDependency,
) -> Response:
    await service.abort_upload(actor, upload_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/files/{file_id}/metadata",
    response_model=Envelope[FileMetadataView],
    operation_id="File_GetMetadata",
)
async def get_file_metadata(
    file_id: str,
    response: Response,
    service: FileServiceDependency,
    actor: OptionalFileActorDependency,
) -> Envelope[FileMetadataView]:
    _no_store(response)
    return Envelope(data=await service.file_metadata(actor, file_id))


@router.get(
    "/files/{file_id}",
    response_class=RedirectResponse,
    operation_id="File_Get",
)
async def get_file(
    file_id: str,
    service: FileServiceDependency,
    actor: OptionalFileActorDependency,
) -> RedirectResponse:
    redirect = RedirectResponse(await service.file_url(actor, file_id), status_code=307)
    redirect.headers["Cache-Control"] = "private, no-store"
    redirect.headers["X-Content-Type-Options"] = "nosniff"
    return redirect
