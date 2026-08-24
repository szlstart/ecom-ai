from datetime import datetime
from typing import Literal

from pydantic import Field

from app.api.schemas import StrictRequest

ContentType = Literal["banner", "announcement", "help_article", "footer", "about"]
SourceFormat = Literal["plain_text", "structured", "html"]


class ContentCreate(StrictRequest):
    content_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    content_type: ContentType
    title: str = Field(min_length=1, max_length=256)
    locale: str = Field(default="zh-CN", max_length=16)
    region_code: str = Field(default="CN", max_length=16)
    source_format: SourceFormat
    source_content: str = Field(min_length=1, max_length=100_000)


class ContentUpdate(StrictRequest):
    title: str = Field(min_length=1, max_length=256)
    locale: str = Field(default="zh-CN", max_length=16)
    region_code: str = Field(default="CN", max_length=16)
    source_format: SourceFormat
    source_content: str = Field(min_length=1, max_length=100_000)


class ContentVersionView(StrictRequest):
    content_version_id: str
    version: str
    locale: str
    region_code: str
    format: str
    blocks: list[dict[str, object]] | None
    html: str | None
    text: str
    status: str
    effective_at: datetime
    expires_at: datetime | None


class ContentView(StrictRequest):
    content_id: str
    content_key: str
    content_type: str
    title: str
    status: str
    version: int
    versions: list[ContentVersionView]


class ContentList(StrictRequest):
    items: list[ContentView]


class PublishedContent(StrictRequest):
    content_id: str
    content_key: str
    content_type: str
    title: str
    version: ContentVersionView


class PublishedContentList(StrictRequest):
    items: list[PublishedContent]
