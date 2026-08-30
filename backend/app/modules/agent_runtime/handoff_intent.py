from __future__ import annotations

import re

_HANDOFF_REQUEST_PATTERNS = (
    re.compile(
        r"^(?:请|麻烦)?(?:直接)?(?:转|转接|联系|找|叫|安排)(?:一下|一个)?"
        r"(?:平台人工客服|人工客服|平台客服|客服人员|人工|真人)"
        r"(?:吧|谢谢|可以吗)?[\u3002\uff01!\uff1f?]*$"
    ),
    re.compile(
        r"(?:请|麻烦|帮我|给我|我要|我想|想要|需要|能否|可以|可不可以|请问能不能)"
        r"(?:直接)?(?:转|转接|联系|找|叫|安排)(?:一下|一个)?"
        r"(?:平台人工客服|人工客服|平台客服|客服人员|人工|真人)"
    ),
    re.compile(
        r"(?:请|麻烦|帮我|给我|我要|我想|想要|需要)"
        r"(?:一个|找)?(?:平台人工客服|人工客服|平台客服|客服人员|人工|真人)"
    ),
    re.compile(
        r"(?:请|麻烦|帮我|给我|我要|我想|想要|需要)"
        r"(?:发起|进行)?(?:人工)?投诉"
    ),
    re.compile(r"(?:humanagent|humanservice|realperson|liveagent|talktoahuman)"),
)

_EXACT_HANDOFF_REQUESTS = {
    "人工",
    "真人",
    "人工客服",
    "平台客服",
    "平台人工客服",
    "客服人员",
    "转人工",
    "转客服",
    "投诉",
}


def is_explicit_handoff_request(value: str) -> bool:
    """Return true only when the current message explicitly requests human service."""

    normalized = re.sub(r"\s+", "", value).casefold()
    if normalized in _EXACT_HANDOFF_REQUESTS:
        return True
    return any(pattern.search(normalized) is not None for pattern in _HANDOFF_REQUEST_PATTERNS)
