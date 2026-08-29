import json

import pytest

from app.core.exceptions import ApplicationError
from app.modules.catalog.content_sanitizer import sanitize_content


def test_plain_text_and_structured_content_produce_safe_payloads() -> None:
    plain = sanitize_content("plain_text", "  安全的商品说明\n支持换行  ")
    assert plain.public_content_format == "structured_v1"
    assert plain.safe_text == "安全的商品说明 支持换行"
    assert plain.safe_blocks == [{"type": "paragraph", "text": "安全的商品说明 支持换行"}]

    file_id = "file_01K3J5Y7W9ABCDEFGHJKMNPQRS"
    structured = sanitize_content(
        "structured",
        json.dumps(
            [
                {"type": "heading", "level": 2, "text": "商品特点"},
                {"type": "bullet_list", "items": ["耐用", "轻便"]},
                {"type": "image", "file_id": file_id, "alt": "商品正面图"},
            ],
            ensure_ascii=False,
        ),
    )
    assert structured.referenced_file_ids == (file_id,)
    assert structured.safe_html is None
    assert structured.safe_text == "商品特点 耐用 轻便 商品正面图"


def test_html_content_is_allowlisted_and_normalized() -> None:
    file_id = "file_01K3J5Y7W9ABCDEFGHJKMNPQRS"
    sanitized = sanitize_content(
        "html",
        (
            '<h2 title="unused">商品说明</h2>'
            "<p><strong>安全</strong>内容</p>"
            f'<img src="/api/v1/files/{file_id}" alt="正面图">'
            '<a href="/help/returns">退换说明</a>'
        ).replace(' title="unused"', ""),
    )
    assert sanitized.public_content_format == "safe_html_v1"
    assert sanitized.referenced_file_ids == (file_id,)
    assert 'rel="noopener noreferrer"' in (sanitized.safe_html or "")
    assert sanitized.safe_text == "商品说明 安全 内容 正面图 退换说明"


@pytest.mark.parametrize(
    "source",
    [
        "<script>alert(1)</script>",
        '<p onclick="alert(1)">说明</p>',
        '<a href="javascript:alert(1)">危险链接</a>',
        '<img src="https://evil.example/image.png" alt="外链图">',
        '<iframe src="/internal">内容</iframe>',
    ],
)
def test_html_content_rejects_executable_or_untrusted_markup(source: str) -> None:
    with pytest.raises(ApplicationError) as rejected:
        sanitize_content("html", source)
    assert rejected.value.code == "PRODUCT_CONTENT_UNSAFE"
