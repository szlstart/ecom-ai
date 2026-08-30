from __future__ import annotations

import re
from collections.abc import Iterable

from app.modules.agent_runtime.prompt_safety import safe_untrusted_excerpt

_POLICY_TERMS = (
    "充值",
    "微信",
    "支付宝",
    "扣款",
    "余额",
    "支付",
    "退款",
    "退货",
    "售后",
    "物流",
    "发货",
    "签收",
    "运费",
    "包邮",
    "订单",
    "库存",
    "价格",
    "隐私",
    "账号",
    "密码",
    "人工",
    "客服",
)


def concise_policy_answer(
    query: str,
    sources: Iterable[tuple[object, object]],
    *,
    intro: str,
) -> str:
    """Create a short, grounded fallback without dumping complete RAG chunks."""

    query_terms = tuple(term for term in _POLICY_TERMS if term in query)
    candidates: list[tuple[int, int, str]] = []
    position = 0
    for raw_title, raw_content in sources:
        title = safe_untrusted_excerpt(raw_title, 160)
        content = safe_untrusted_excerpt(raw_content, 1200)
        for raw_part in re.split(r"(?<=[\u3002\uff01\uff1f\uff1b])|\n+", content):
            # Indexed Markdown is whitespace-normalized, so a heading and its first
            # bullet can share one fragment ("## 模拟充值 - 当前..."). Keep the
            # factual bullet instead of discarding the complete fragment as a heading.
            if " - " in raw_part:
                raw_part = raw_part.rsplit(" - ", 1)[-1]
            part = re.sub(
                r"^(?:[-*•>]\s*|\d+[.)、]\s*)", "", raw_part.strip()
            ).strip()
            if not part or part.startswith("#"):
                continue
            score = sum(6 for term in query_terms if term in part)
            score += sum(2 for term in query_terms if term in title)
            candidates.append((score, -position, safe_untrusted_excerpt(part, 260)))
            position += 1

    if not candidates:
        return intro
    candidates.sort(reverse=True)
    limit = 1 if any(marker in query for marker in ("一句话", "简短", "简要", "只回答")) else 2
    selected: list[str] = []
    for _, _, part in candidates:
        normalized = part.rstrip("\uff1b\u3002")
        if normalized and normalized not in selected:
            selected.append(normalized)
        if len(selected) >= limit:
            break
    if not selected:
        return intro
    prefix = intro.rstrip("\uff1a:\uff0c,\u3002")
    return f"{prefix}\uff1a" + "\uff1b".join(selected) + "\u3002"
