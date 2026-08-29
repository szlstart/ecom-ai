from __future__ import annotations

import base64
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.dependencies import DatabaseSession
from app.api.schemas import Envelope, StrictRequest
from app.core.exceptions import ApplicationError
from app.modules.content.schemas import PublishedContent, PublishedContentList
from app.modules.content.service import ContentService
from app.modules.identity.repository import IdentityRepository

router = APIRouter(prefix="/content", tags=["content"])


@router.get(
    "/banners",
    response_model=Envelope[PublishedContentList],
    operation_id="HomeBanner_ListPublished",
)
async def list_banners(session: DatabaseSession) -> Envelope[PublishedContentList]:
    return Envelope(data=await ContentService(session).published("banner"))


@router.get(
    "/announcements",
    response_model=Envelope[PublishedContentList],
    operation_id="PlatformAnnouncement_ListPublished",
)
async def list_announcements(session: DatabaseSession) -> Envelope[PublishedContentList]:
    return Envelope(data=await ContentService(session).published("announcement"))


@router.get(
    "/help-articles",
    response_model=Envelope[PublishedContentList],
    operation_id="HelpArticle_ListPublished",
)
async def list_help(session: DatabaseSession) -> Envelope[PublishedContentList]:
    return Envelope(data=await ContentService(session).published("help_article"))


@router.get(
    "/help-articles/{content_key}",
    response_model=Envelope[PublishedContent],
    operation_id="HelpArticle_GetPublished",
)
async def get_help(content_key: str, session: DatabaseSession) -> Envelope[PublishedContent]:
    return Envelope(
        data=await ContentService(session).published_key("help_article", content_key)
    )


@router.get(
    "/footer",
    response_model=Envelope[PublishedContentList],
    operation_id="FooterContent_GetPublished",
)
async def get_footer(session: DatabaseSession) -> Envelope[PublishedContentList]:
    return Envelope(data=await ContentService(session).published("footer"))


@router.get(
    "/about",
    response_model=Envelope[PublishedContentList],
    operation_id="AboutContent_GetPublished",
)
async def get_about(session: DatabaseSession) -> Envelope[PublishedContentList]:
    return Envelope(data=await ContentService(session).published("about"))


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
