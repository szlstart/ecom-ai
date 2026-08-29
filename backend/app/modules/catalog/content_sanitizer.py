from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal, cast

from app.core.exceptions import ApplicationError

MAX_SOURCE_BYTES = 100_000
_FILE_URL = re.compile(r"^/api/v1/files/(file_[0-9A-HJKMNP-TV-Z]{26})$")
_ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "em",
    "ul",
    "ol",
    "li",
    "h2",
    "h3",
    "blockquote",
    "a",
    "img",
}
_VOID_TAGS = {"br", "img"}


@dataclass(frozen=True)
class SanitizedContent:
    public_content_format: Literal["structured_v1", "safe_html_v1"]
    safe_blocks: list[dict[str, object]] | None
    safe_html: str | None
    safe_text: str
    referenced_file_ids: tuple[str, ...]


def sanitize_content(source_format: str, source_content: str) -> SanitizedContent:
    if len(source_content.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise _invalid("内容超过允许的 100KB 上限。")
    if source_format == "plain_text":
        text = _normalized_text(source_content)
        if not text:
            raise _invalid("内容不能为空。")
        return SanitizedContent(
            public_content_format="structured_v1",
            safe_blocks=[{"type": "paragraph", "text": text}],
            safe_html=None,
            safe_text=text,
            referenced_file_ids=(),
        )
    if source_format == "structured":
        return _sanitize_structured(source_content)
    if source_format == "html":
        parser = _StrictHtmlSanitizer()
        try:
            parser.feed(source_content)
            parser.close()
        except (ValueError, AssertionError) as exc:
            raise _invalid(str(exc)) from exc
        safe_text = _normalized_text(" ".join(parser.text_parts))
        if not safe_text:
            raise _invalid("内容必须包含可阅读文本。")
        return SanitizedContent(
            public_content_format="safe_html_v1",
            safe_blocks=None,
            safe_html="".join(parser.output),
            safe_text=safe_text,
            referenced_file_ids=tuple(dict.fromkeys(parser.file_ids)),
        )
    raise _invalid("source_format 仅支持 plain_text、structured 或 html。")


def _sanitize_structured(source_content: str) -> SanitizedContent:
    try:
        raw = json.loads(source_content)
    except json.JSONDecodeError as exc:
        raise _invalid("结构化内容不是有效 JSON。") from exc
    if not isinstance(raw, list) or not 1 <= len(raw) <= 200:
        raise _invalid("结构化内容必须包含 1 到 200 个 Block。")
    blocks: list[dict[str, object]] = []
    text_parts: list[str] = []
    file_ids: list[str] = []
    for candidate in raw:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("type"), str):
            raise _invalid("每个 Block 必须包含字符串 type。")
        block_type = candidate["type"]
        if block_type in {"paragraph", "heading"}:
            allowed = {"type", "text"} | ({"level"} if block_type == "heading" else set())
            if set(candidate) - allowed:
                raise _invalid("结构化文本 Block 包含未知字段。")
            text = candidate.get("text")
            if not isinstance(text, str) or not _normalized_text(text):
                raise _invalid("文本 Block 的 text 不能为空。")
            clean: dict[str, object] = {"type": block_type, "text": _normalized_text(text)}
            if block_type == "heading":
                if candidate.get("level") not in {2, 3}:
                    raise _invalid("标题 level 仅允许 2 或 3。")
                clean["level"] = candidate["level"]
            blocks.append(clean)
            text_parts.append(cast(str, clean["text"]))
        elif block_type == "bullet_list":
            if set(candidate) - {"type", "items"}:
                raise _invalid("列表 Block 包含未知字段。")
            items = candidate.get("items")
            if (
                not isinstance(items, list)
                or not 1 <= len(items) <= 100
                or not all(isinstance(item, str) and _normalized_text(item) for item in items)
            ):
                raise _invalid("列表 items 必须是 1 到 100 个非空字符串。")
            normalized_items = [_normalized_text(cast(str, item)) for item in items]
            blocks.append({"type": block_type, "items": normalized_items})
            text_parts.extend(normalized_items)
        elif block_type == "image":
            if set(candidate) - {"type", "file_id", "alt"}:
                raise _invalid("图片 Block 包含未知字段。")
            file_id = candidate.get("file_id")
            alt = candidate.get("alt")
            if (
                not isinstance(file_id, str)
                or _FILE_URL.fullmatch(f"/api/v1/files/{file_id}") is None
            ):
                raise _invalid("图片 Block 必须引用有效 file_id。")
            if not isinstance(alt, str) or not 1 <= len(alt.strip()) <= 255:
                raise _invalid("图片 Block 必须包含 1 到 255 字符的 alt。")
            blocks.append({"type": block_type, "file_id": file_id, "alt": alt.strip()})
            file_ids.append(file_id)
            text_parts.append(alt.strip())
        else:
            raise _invalid(f"不支持的结构化 Block: {block_type}。")
    return SanitizedContent(
        public_content_format="structured_v1",
        safe_blocks=blocks,
        safe_html=None,
        safe_text=_normalized_text(" ".join(text_parts)),
        referenced_file_ids=tuple(dict.fromkeys(file_ids)),
    )


class _StrictHtmlSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.text_parts: list[str] = []
        self.file_ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _ALLOWED_TAGS:
            raise ValueError(f"不允许使用 HTML 标签 {tag}。")
        safe_attrs = self._attributes(tag, attrs)
        suffix = "".join(
            f' {name}="{html.escape(value, quote=True)}"' for name, value in safe_attrs
        )
        self.output.append(f"<{tag}{suffix}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            raise ValueError(f"不允许使用 HTML 结束标签 {tag}。")
        self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.output.append(html.escape(data, quote=False))
        if data.strip():
            self.text_parts.append(data)

    def handle_entityref(self, name: str) -> None:
        raise AssertionError("entity references must be converted")

    def handle_charref(self, name: str) -> None:
        raise AssertionError("character references must be converted")

    def _attributes(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
        allowed = {"a": {"href", "title"}, "img": {"src", "alt", "title"}}.get(tag, set())
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name, value in attrs:
            if name in seen or name not in allowed or value is None:
                raise ValueError(f"标签 {tag} 包含不允许或重复的属性 {name}。")
            seen.add(name)
            if tag == "a" and name == "href":
                if not value.startswith("/") or value.startswith("//"):
                    raise ValueError("链接只允许本站相对路径。")
            if tag == "img" and name == "src":
                match = _FILE_URL.fullmatch(value)
                if match is None:
                    raise ValueError("图片只允许引用受控 file_id。")
                self.file_ids.append(match.group(1))
            if tag == "img" and name == "alt":
                if not 1 <= len(value.strip()) <= 255:
                    raise ValueError("图片必须包含 1 到 255 字符的 alt。")
                self.text_parts.append(value.strip())
            result.append((name, value))
        if tag == "a":
            result.append(("rel", "noopener noreferrer"))
        if tag == "img" and not {"src", "alt"}.issubset(seen):
            raise ValueError("图片必须同时包含 src 与 alt。")
        return result


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


def _invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        status=422,
        code="PRODUCT_CONTENT_UNSAFE",
        title="Product content rejected",
        detail=detail,
    )
