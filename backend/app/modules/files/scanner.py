from __future__ import annotations

import asyncio
import hashlib
import io
import struct
from dataclasses import dataclass
from typing import Protocol

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import Settings
from app.core.exceptions import ApplicationError


class MalwareScanner(Protocol):
    async def scan(self, payload: bytes) -> None: ...


class ClamAvScanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def scan(self, payload: bytes) -> None:
        if not self.settings.file_scanner_enabled:
            raise ApplicationError(
                status=503,
                code="FILE_SCANNER_UNAVAILABLE",
                title="File scanner unavailable",
                detail="文件安全扫描服务尚未启用。",
                retryable=True,
            )

        async def operation() -> None:
            reader, writer = await asyncio.open_connection(
                self.settings.file_scanner_host,
                self.settings.file_scanner_port,
            )
            try:
                writer.write(b"zINSTREAM\0")
                for offset in range(0, len(payload), 64 * 1024):
                    chunk = payload[offset : offset + 64 * 1024]
                    writer.write(struct.pack("!I", len(chunk)))
                    writer.write(chunk)
                    await writer.drain()
                writer.write(struct.pack("!I", 0))
                await writer.drain()
                result = (await reader.readuntil(b"\0")).decode(errors="replace")
            finally:
                writer.close()
                await writer.wait_closed()
            if " FOUND" in result:
                raise ApplicationError(
                    status=422,
                    code="FILE_MALWARE_DETECTED",
                    title="Unsafe file rejected",
                    detail="文件未通过恶意内容检测。",
                )
            if not result.endswith("OK\0"):
                raise ApplicationError(
                    status=503,
                    code="FILE_SCANNER_ERROR",
                    title="File scanner error",
                    detail="文件安全扫描未能完成。",
                    retryable=True,
                )

        try:
            await asyncio.wait_for(operation(), timeout=self.settings.file_scanner_timeout_seconds)
        except ApplicationError:
            raise
        except (TimeoutError, OSError, asyncio.IncompleteReadError) as exc:
            raise ApplicationError(
                status=503,
                code="FILE_SCANNER_UNAVAILABLE",
                title="File scanner unavailable",
                detail="文件安全扫描服务暂时不可用。",
                retryable=True,
            ) from exc


@dataclass(frozen=True)
class ProcessedImage:
    variant: str
    payload: bytes
    content_type: str
    width: int
    height: int
    sha256: bytes


@dataclass(frozen=True)
class ImageProcessingResult:
    detected_content_type: str
    width: int
    height: int
    variants: list[ProcessedImage]


def process_public_image(
    payload: bytes, max_pixels: int, widths: tuple[int, ...] = (320, 960)
) -> ImageProcessingResult:
    try:
        with Image.open(io.BytesIO(payload)) as source:
            source.verify()
        with Image.open(io.BytesIO(payload)) as source:
            source_format = source.format
            if source_format not in {"JPEG", "PNG", "WEBP"}:
                raise _unsafe_image("不支持该图片编码格式。")
            if source.width * source.height > max_pixels:
                raise _unsafe_image("图片总像素超过允许上限。")
            image = ImageOps.exif_transpose(source)
            if getattr(image, "n_frames", 1) != 1:
                raise _unsafe_image("首版不接受动画图片。")
            original_width, original_height = image.size
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            detected = {
                "JPEG": "image/jpeg",
                "PNG": "image/png",
                "WEBP": "image/webp",
            }[source_format]
            return ImageProcessingResult(
                detected_content_type=detected,
                width=original_width,
                height=original_height,
                variants=[_encode_variant(image, width) for width in widths],
            )
    except ApplicationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise _unsafe_image("文件不是可安全解码的图片。") from exc


def detect_private_content_type(payload: bytes, declared: str) -> str:
    if declared in {"image/jpeg", "image/png", "image/webp"}:
        processed = process_public_image(payload, 40_000_000)
        return processed.detected_content_type
    if declared == "application/pdf" and payload.startswith(b"%PDF-"):
        return declared
    if declared == "text/csv" and b"\x00" not in payload[:8192]:
        return declared
    if (
        declared == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        and payload.startswith(b"PK\x03\x04")
    ):
        return declared
    raise ApplicationError(
        status=422,
        code="FILE_TYPE_MISMATCH",
        title="File type mismatch",
        detail="文件实际类型与声明用途不匹配。",
    )


def _encode_variant(image: Image.Image, target_width: int) -> ProcessedImage:
    rendered = image.copy()
    rendered.thumbnail((target_width, target_width), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    rendered.save(output, format="WEBP", quality=86, method=6, exif=b"")
    payload = output.getvalue()
    variant = f"w{target_width}"
    return ProcessedImage(
        variant=variant,
        payload=payload,
        content_type="image/webp",
        width=rendered.width,
        height=rendered.height,
        sha256=hashlib.sha256(payload).digest(),
    )


def _unsafe_image(detail: str) -> ApplicationError:
    return ApplicationError(
        status=422,
        code="FILE_IMAGE_UNSAFE",
        title="Unsafe image rejected",
        detail=detail,
    )
