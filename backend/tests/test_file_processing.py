import io

import pytest
from PIL import Image

from app.core.exceptions import ApplicationError
from app.modules.files.policies import upload_policy
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


def test_private_file_type_detection_rejects_declared_pdf_with_wrong_magic() -> None:
    with pytest.raises(ApplicationError) as captured:
        detect_private_content_type(b"not-a-pdf", "application/pdf")

    assert captured.value.code == "FILE_TYPE_MISMATCH"


def test_retired_store_certification_upload_policy_is_not_exposed() -> None:
    with pytest.raises(ApplicationError) as captured:
        upload_policy("store_certification")

    assert captured.value.status == 404
    assert captured.value.code == "FILE_UPLOAD_POLICY_NOT_FOUND"
