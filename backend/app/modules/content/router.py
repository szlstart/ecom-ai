from __future__ import annotations

import base64
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.dependencies import DatabaseSession
from app.api.schemas import Envelope, StrictRequest
from app.core.exceptions import ApplicationError
from app.modules.identity.repository import IdentityRepository

router = APIRouter(prefix="/content", tags=["content"])


class LegalDocument(StrictRequest):
    document_type: Literal["terms_of_service", "privacy_policy"]
    document_version: str
    title: str
    locale: str
    region_code: str
    safe_content: str
    content_hash: str
    effective_at: str


@router.get(
    "/legal-documents/{document_type}",
    response_model=Envelope[LegalDocument],
    operation_id="LegalDocument_GetPublished",
)
async def get_legal_document(
    document_type: Literal["terms_of_service", "privacy_policy"],
    session: DatabaseSession,
    version: Annotated[str, Query(min_length=1, max_length=40)],
    locale: Annotated[str, Query(max_length=16)] = "zh-CN",
    region_code: Annotated[str, Query(max_length=16)] = "CN",
) -> Envelope[LegalDocument]:
    result = await IdentityRepository(session).legal_version(document_type, version)
    if result is None:
        raise ApplicationError(
            status=404,
            code="LEGAL_DOCUMENT_NOT_FOUND",
            title="Legal document not found",
            detail="未找到指定版本的协议文档。",
        )
    entry, content_version = result
    if content_version.locale != locale or content_version.region_code != region_code:
        raise ApplicationError(
            status=404,
            code="LEGAL_DOCUMENT_NOT_FOUND",
            title="Legal document not found",
            detail="未找到指定语言和地区的协议文档。",
        )
    content_hash = base64.urlsafe_b64encode(content_version.content_hash).rstrip(b"=").decode()
    return Envelope(
        data=LegalDocument(
            document_type=document_type,
            document_version=content_version.document_version,
            title=entry.title,
            locale=content_version.locale,
            region_code=content_version.region_code,
            safe_content=content_version.safe_content,
            content_hash=content_hash,
            effective_at=content_version.effective_at.isoformat() + "Z",
        )
    )
