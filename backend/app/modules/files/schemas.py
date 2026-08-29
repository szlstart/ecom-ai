from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.api.schemas import StrictRequest


class FileUploadPolicyView(StrictRequest):
    purpose: str
    policy_version: str
    allowed_mime_types: list[str]
    allowed_extensions: list[str]
    max_size_bytes: int
    max_count: int
    max_pixels: int | None


class FileUploadSessionCreateRequest(StrictRequest):
    purpose: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1, le=100 * 1024 * 1024)
    content_type: str = Field(min_length=3, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    business_context_id: str | None = Field(default=None, max_length=64)

    @field_validator("filename")
    @classmethod
    def reject_path_filename(cls, value: str) -> str:
        if "/" in value or "\\" in value or "\x00" in value:
            raise ValueError("filename must not contain path separators")
        return value


class FileUploadInstruction(StrictRequest):
    method: str
    url: str
    headers: dict[str, str]
    expires_at: datetime


class FileVariantView(StrictRequest):
    file_id: str
    variant: str
    status: str
    scan_status: str
    content_type: str
    size_bytes: int
    width: int | None
    height: int | None
    url: str | None


class FileUploadSessionView(StrictRequest):
    upload_id: str
    purpose: str
    owner_type: str
    owner_id: str
    upload_status: str
    expires_at: datetime
    upload: FileUploadInstruction | None = None
    source_file: FileVariantView | None = None
    bindable_file: FileVariantView | None = None
    variants: list[FileVariantView] = Field(default_factory=list)


class FileUploadCompleteRequest(StrictRequest):
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    provider_checksum: str | None = Field(default=None, max_length=128)


class FileMetadataView(FileVariantView):
    purpose: str
    owner_type: str
    owner_id: str
    visibility: str
