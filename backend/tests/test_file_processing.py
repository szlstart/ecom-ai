import base64
import hashlib
import io
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.integrations.object_storage import ObjectMetadata, ObjectStorage
from app.modules.files.models import FileObject
from app.modules.files.ocr import OcrResult, _parse_tesseract_tsv
from app.modules.files.policies import upload_policy
from app.modules.files.processor import FileProcessingSource, FileProcessor
from app.modules.files.repository import FileRepository
from app.modules.files.scanner import detect_private_content_type, process_public_image


def _png(width: int = 1600, height: int = 900) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (31, 111, 235)).save(output, format="PNG")
    return output.getvalue()


def test_public_image_is_decoded_and_reencoded_as_safe_webp_variants() -> None:
    result = process_public_image(_png(), max_pixels=2_000_000, widths=(128, 512))

    assert result.detected_content_type == "image/png"
    assert (result.width, result.height) == (1600, 900)
    assert [item.variant for item in result.variants] == ["w128", "w512"]
    assert all(item.content_type == "image/webp" for item in result.variants)
    assert all(item.width <= int(item.variant.removeprefix("w")) for item in result.variants)
    for item in result.variants:
        with Image.open(io.BytesIO(item.payload)) as rendered:
            assert rendered.format == "WEBP"
            assert rendered.getexif() == {}


def test_public_image_rejects_pixel_bomb_before_derivation() -> None:
    with pytest.raises(ApplicationError) as captured:
        process_public_image(_png(200, 200), max_pixels=10_000)

    assert captured.value.code == "FILE_IMAGE_UNSAFE"


def test_public_image_rejects_broken_png_crc_without_retryable_exception() -> None:
    broken = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZScs"
        "AAAAASUVORK5CYII="
    )

    with pytest.raises(ApplicationError) as captured:
        process_public_image(broken, max_pixels=20_000_000)

    assert captured.value.code == "FILE_IMAGE_UNSAFE"
    assert captured.value.retryable is False


def test_private_file_type_detection_rejects_declared_pdf_with_wrong_magic() -> None:
    with pytest.raises(ApplicationError) as captured:
        detect_private_content_type(b"not-a-pdf", "application/pdf")

    assert captured.value.code == "FILE_TYPE_MISMATCH"


def test_retired_store_certification_upload_policy_is_not_exposed() -> None:
    with pytest.raises(ApplicationError) as captured:
        upload_policy("store_certification")

    assert captured.value.status == 404
    assert captured.value.code == "FILE_UPLOAD_POLICY_NOT_FOUND"


def test_tesseract_tsv_parser_rejects_low_confidence_noise() -> None:
    header = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\t"
        "width\theight\tconf\ttext"
    )
    rows = [
        "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t92\t面料成分",
        "5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t88\t棉95%",
        "5\t1\t2\t1\t1\t1\t0\t0\t1\t1\t12\tawxq",
        "5\t1\t3\t1\t1\t1\t0\t0\t1\t1\t80\tX",
    ]

    assert _parse_tesseract_tsv("\n".join([header, *rows]), 8000) == "面料成分 棉95%"


async def test_reprocessing_reactivates_existing_deleted_variant_with_same_file_id() -> None:
    session = MagicMock()
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    storage = MagicMock()
    storage.put = AsyncMock(
        return_value=ObjectMetadata(
            size=128,
            content_type="image/webp",
            etag="restored-etag",
            version_id="restored-version",
            metadata={},
        )
    )
    scanner = MagicMock()
    processor = FileProcessor(
        cast(AsyncSession, session), cast(ObjectStorage, storage), scanner
    )
    source = FileObject(
        id=10,
        file_no="file_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        bucket="private-image-sources",
        object_key="source/product.png",
        purpose="product_detail",
        owner_type="store",
        owner_no="sto_test",
        upload_session_id=None,
        parent_file_id=None,
        variant="original",
        processor_version=None,
        declared_mime_type="image/png",
        detected_mime_type="image/png",
        size_bytes=len(_png()),
        sha256=b"x" * 32,
        visibility="private",
        sensitivity_level="L0",
        scan_status="processing",
        file_status="scanning",
        reference_count=1,
        version=3,
    )
    deleted = FileObject(
        id=11,
        file_no="file_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        bucket="public-assets",
        object_key="removed/w320.webp",
        purpose="product_detail",
        owner_type="store",
        owner_no="sto_test",
        upload_session_id=None,
        parent_file_id=10,
        variant="w320",
        processor_version="image-v1",
        declared_mime_type="image/png",
        detected_mime_type="image/webp",
        size_bytes=0,
        sha256=b"y" * 32,
        visibility="public_derivative",
        sensitivity_level="L0",
        scan_status="safe",
        file_status="deleted",
        reference_count=1,
        version=2,
    )
    repository = MagicMock(spec=FileRepository)
    repository.file_by_id = AsyncMock(return_value=source)
    repository.variants = AsyncMock(return_value=[deleted])
    processor.repository = repository

    await processor._activate_public_image(
        FileProcessingSource(
            id=source.id,
            file_no=source.file_no,
            bucket=source.bucket,
            object_key=source.object_key,
            purpose=source.purpose,
            declared_mime_type=source.declared_mime_type,
            size_bytes=source.size_bytes,
            sha256=source.sha256,
        ),
        _png(),
        2_000_000,
    )

    assert deleted.file_no == "file_01ARZ3NDEKTSV4RRFFQ69G5FAW"
    assert deleted.file_status == "active"
    assert deleted.scan_status == "safe"
    assert deleted.object_key.endswith("/w320.webp")
    assert deleted.deleted_at is None
    assert deleted.provider_checksum == "restored-etag"
    session.commit.assert_awaited_once()


async def test_ocr_result_is_copied_to_source_and_all_public_variants() -> None:
    session = MagicMock()
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    storage = MagicMock()
    payload = _png()
    storage.read = AsyncMock(return_value=payload)
    scanner = MagicMock()
    ocr = MagicMock()
    ocr.extract = AsyncMock(
        return_value=OcrResult(
            text="面料成分: 棉 95%\n洗涤建议: 冷水手洗",
            engine="tesseract-5-tsv-v2",
            language="chi_sim+eng",
        )
    )
    source = FileObject(
        id=20,
        file_no="file_01ARZ3NDEKTSV4RRFFQ69G5FAX",
        bucket="private-image-sources",
        object_key="source/detail.png",
        purpose="product_detail",
        owner_type="store",
        owner_no="sto_test",
        upload_session_id=None,
        parent_file_id=None,
        variant="original",
        processor_version=None,
        declared_mime_type="image/png",
        detected_mime_type="image/png",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).digest(),
        visibility="private",
        sensitivity_level="L0",
        scan_status="safe",
        ocr_status="processing",
        file_status="active",
        reference_count=1,
        version=4,
    )
    derived = FileObject(
        id=21,
        file_no="file_01ARZ3NDEKTSV4RRFFQ69G5FAY",
        bucket="public-assets",
        object_key="detail/w960.webp",
        purpose="product_detail",
        owner_type="store",
        owner_no="sto_test",
        upload_session_id=None,
        parent_file_id=20,
        variant="w960",
        processor_version="image-v1",
        declared_mime_type="image/png",
        detected_mime_type="image/webp",
        size_bytes=100,
        sha256=b"z" * 32,
        visibility="public_derivative",
        sensitivity_level="L0",
        scan_status="safe",
        ocr_status="processing",
        file_status="active",
        reference_count=1,
        version=2,
    )
    repository = MagicMock(spec=FileRepository)
    repository.file_by_id = AsyncMock(side_effect=[source, source])
    repository.variants = AsyncMock(return_value=[derived])
    processor = FileProcessor(
        cast(AsyncSession, session), cast(ObjectStorage, storage), scanner, ocr
    )
    processor.repository = repository

    await processor._process_ocr_claimed(source.id)

    assert source.ocr_status == "completed"
    assert derived.ocr_status == "completed"
    assert derived.ocr_text == "面料成分: 棉 95%\n洗涤建议: 冷水手洗"
    assert derived.ocr_engine == "tesseract-5-tsv-v2"
    assert derived.ocr_error_code is None
    session.commit.assert_awaited_once()
