from __future__ import annotations

from dataclasses import dataclass

from app.core.exceptions import ApplicationError


@dataclass(frozen=True)
class UploadPolicy:
    purpose: str
    version: str
    allowed_mime_types: tuple[str, ...]
    allowed_extensions: tuple[str, ...]
    max_size_bytes: int
    max_count: int
    max_pixels: int | None
    owner_type: str
    permissions: tuple[str, ...]
    processor: str


MIB = 1024 * 1024
POLICIES = {
    "product": UploadPolicy(
        purpose="product",
        version="product-image-v1",
        allowed_mime_types=("image/jpeg", "image/png", "image/webp"),
        allowed_extensions=("jpg", "jpeg", "png", "webp"),
        max_size_bytes=10 * MIB,
        max_count=20,
        max_pixels=40_000_000,
        owner_type="store",
        permissions=("products:create", "products:update"),
        processor="public_image",
    ),
    "store_logo": UploadPolicy(
        purpose="store_logo",
        version="store-logo-v1",
        allowed_mime_types=("image/jpeg", "image/png", "image/webp"),
        allowed_extensions=("jpg", "jpeg", "png", "webp"),
        max_size_bytes=5 * MIB,
        max_count=1,
        max_pixels=20_000_000,
        owner_type="store",
        permissions=("stores:manage",),
        processor="public_image",
    ),
    "review_image": UploadPolicy(
        purpose="review_image",
        version="review-image-v1",
        allowed_mime_types=("image/jpeg", "image/png", "image/webp"),
        allowed_extensions=("jpg", "jpeg", "png", "webp"),
        max_size_bytes=10 * MIB,
        max_count=6,
        max_pixels=40_000_000,
        owner_type="user",
        permissions=(),
        processor="public_image",
    ),
    "brand_logo": UploadPolicy(
        purpose="brand_logo",
        version="brand-logo-v1",
        allowed_mime_types=("image/jpeg", "image/png", "image/webp"),
        allowed_extensions=("jpg", "jpeg", "png", "webp"),
        max_size_bytes=5 * MIB,
        max_count=1,
        max_pixels=20_000_000,
        owner_type="platform",
        permissions=("catalog_taxonomy:manage",),
        processor="public_image",
    ),
    "category_icon": UploadPolicy(
        purpose="category_icon",
        version="category-icon-v1",
        allowed_mime_types=("image/jpeg", "image/png", "image/webp"),
        allowed_extensions=("jpg", "jpeg", "png", "webp"),
        max_size_bytes=5 * MIB,
        max_count=1,
        max_pixels=20_000_000,
        owner_type="platform",
        permissions=("catalog_taxonomy:manage",),
        processor="public_image",
    ),
    "admin_import": UploadPolicy(
        purpose="admin_import",
        version="admin-import-v1",
        allowed_mime_types=(
            "text/csv",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        allowed_extensions=("csv", "xlsx"),
        max_size_bytes=100 * MIB,
        max_count=1,
        max_pixels=None,
        owner_type="store",
        permissions=("products:create", "inventories:adjust"),
        processor="private_document",
    ),
}


def upload_policy(purpose: str) -> UploadPolicy:
    try:
        return POLICIES[purpose]
    except KeyError as exc:
        raise ApplicationError(
            status=404,
            code="FILE_UPLOAD_POLICY_NOT_FOUND",
            title="Upload policy not found",
            detail="当前文件用途没有可用的上传策略。",
        ) from exc
