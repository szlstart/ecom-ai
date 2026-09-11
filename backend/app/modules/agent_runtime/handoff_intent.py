from __future__ import annotations

import re

_HANDOFF_REQUEST_PATTERNS = (
    re.compile(
        r"^(?:请|麻烦)?(?:直接)?(?:转|转接|联系|找|叫|安排)(?:一下|一个)?"
        r"(?:平台人工客服|人工客服|平台客服|客服人员|人工|真人)"
        r"(?:吧|谢谢|可以吗)?[\u3002\uff01!\uff1f?]*$"
    ),
    re.compile(
        r"(?:(?<![申求])请|麻烦|帮我|给我|我要|我想|想要|需要|能否|可以|可不可以|请问能不能)"
        r"(?:直接)?(?:转到|转给|转接|转|联系|找|叫|安排)(?:一下|一个)?"
        r"(?:平台人工客服|人工客服|平台客服|客服人员|人工|真人)"
    ),
    re.compile(
        r"(?:(?<![申求])请|麻烦|帮我|给我|我要|我想|想要|需要)"
        r"(?:一个|找)?(?:平台人工客服|人工客服|平台客服|客服人员|人工|真人)"
    ),
    re.compile(
        r"(?:(?<![申求])请|麻烦|帮我|给我|我要|我想|想要|需要)"
        r"(?:发起|进行)?(?:人工)?投诉"
    ),
    re.compile(
        r"(?:^|[。\uff01!\uff1f?\uff0c,])"
        r"(?:我(?:要|想|需要)?|请|麻烦|帮我|给我)?(?:再次|重新)?"
        r"(?:请求|申请)(?:接入|转接|联系|安排)?(?:一下|一个)?"
        r"(?:平台人工客服|平台人工|平台客服|人工客服|真人客服)"
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

_HANDOFF_NEGATION_PATTERN = re.compile(
    r"(?:不(?:要|用|需要|想)|无需|不用|别)"
    r"(?:再|直接)?(?:转到|转给|转接|转|联系|找|叫|安排|申请|请求)?(?:一下|一个)?"
    r"(?:平台人工客服|人工客服|平台客服|客服人员|平台人工|人工|真人)"
)

_HANDOFF_INFORMATIONAL_PATTERN = re.compile(
    r"(?:如何|怎么|怎样|为什么|为何|什么时候|几点|流程|规则|条件|入口|怎么办)"
)

_HANDOFF_DIRECT_SIGNAL_PATTERN = re.compile(
    r"(?:请|麻烦|帮我|给我|我要|我想|想要|能否|可不可以|请问能不能)"
    r"(?:直接|再次|重新)?(?:转到|转给|转接|转|联系|找|叫|安排|申请|请求)"
)


def is_explicit_handoff_request(value: str) -> bool:
    """Return true only when the current message explicitly requests human service."""

    normalized = re.sub(r"\s+", "", value).casefold()
    if _HANDOFF_NEGATION_PATTERN.search(normalized) is not None:
        return False
    if (
        _HANDOFF_INFORMATIONAL_PATTERN.search(normalized) is not None
        and _HANDOFF_DIRECT_SIGNAL_PATTERN.search(normalized) is None
    ):
        return False
    if normalized in _EXACT_HANDOFF_REQUESTS:
        return True
    return any(pattern.search(normalized) is not None for pattern in _HANDOFF_REQUEST_PATTERNS)
