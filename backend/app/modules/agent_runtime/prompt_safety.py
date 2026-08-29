from __future__ import annotations

import base64
import binascii
import re
import unicodedata

REDACTED_UNTRUSTED_TEXT = "[疑似提示注入内容已省略]"

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_BASE64_TOKEN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])")
_INJECTION_PATTERNS = (
    re.compile(
        r"(?:忽略|无视|绕过|覆盖).{0,24}(?:系统|开发者|先前|之前|安全).{0,12}(?:指令|规则|提示词|限制)"
    ),
    re.compile(r"(?:泄露|输出|显示|告诉我).{0,24}(?:系统提示词|密码|密钥|令牌|token|secret)"),
    re.compile(r"ignore.{0,32}(?:previous|prior|system|developer|instruction|rules)"),
    re.compile(
        r"(?:reveal|print|show|exfiltrate).{0,32}(?:system.?prompt|password|secret|token|api.?key)"
    ),
    re.compile(
        r"(?:call|invoke|execute|use).{0,24}(?:tool|function).{0,24}(?:without|bypass|ignore)"
    ),
)


def detects_prompt_injection(value: str) -> bool:
    canonical = _canonical(value)
    normalized = canonical.casefold()
    if _matches(normalized):
        return True
    for token in _BASE64_TOKEN.findall(canonical):
        decoded = _decode_base64(token)
        if decoded is not None and _matches(_normalize(decoded)):
            return True
    return False


def safe_untrusted_excerpt(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if detects_prompt_injection(text):
        return REDACTED_UNTRUSTED_TEXT
    return text[:limit]


def _normalize(value: str) -> str:
    return _canonical(value).casefold()


def _canonical(value: str) -> str:
    return _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", value))


def _matches(value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in _INJECTION_PATTERNS)


def _decode_base64(value: str) -> str | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, validate=True)
        return decoded.decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
