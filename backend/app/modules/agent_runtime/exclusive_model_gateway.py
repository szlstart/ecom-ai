from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from app.modules.agent_runtime.handoff_intent import is_explicit_handoff_request

ExclusiveIntent = Literal[
    "general_chat",
    "policy_qa",
    "product_search",
    "personalized_recommendation",
    "order_lookup",
    "logistics_lookup",
    "refund_eligibility",
    "refund_progress",
    "human_handoff",
]


@dataclass(frozen=True)
class ExclusiveAgentPlan:
    intent: ExclusiveIntent
    search_text: str | None = None


class ExclusiveModelGateway(Protocol):
    async def plan(self, user_text: str) -> ExclusiveAgentPlan: ...


class DeterministicExclusiveModelGateway:
    async def plan(self, user_text: str) -> ExclusiveAgentPlan:
        text = re.sub(r"\s+", "", user_text).casefold()
        if is_explicit_handoff_request(user_text):
            return ExclusiveAgentPlan("human_handoff")
        if _contains(text, "退款进度", "售后进度", "退款到哪", "退款状态"):
            return ExclusiveAgentPlan("refund_progress")
        if _contains(text, "申请退款", "我要退款", "退货退款", "仅退款", "发起售后"):
            return ExclusiveAgentPlan("refund_eligibility")
        if _contains(text, "物流", "快递", "包裹", "到哪", "送达"):
            return ExclusiveAgentPlan("logistics_lookup")
        if _contains(text, "订单", "付款", "收货", "购买记录"):
            return ExclusiveAgentPlan("order_lookup")
        if _contains(text, "推荐", "适合我", "偏好"):
            return ExclusiveAgentPlan("personalized_recommendation", _search_text(user_text))
        if _contains(text, "商品", "搜索", "找", "买", "价格", "对比"):
            return ExclusiveAgentPlan("product_search", _search_text(user_text))
        if _contains(text, "规则", "政策", "平台", "运费", "退换", "保修", "发票"):
            return ExclusiveAgentPlan("policy_qa")
        return ExclusiveAgentPlan("general_chat")


def _contains(value: str, *terms: str) -> bool:
    return any(term in value for term in terms)


def _search_text(value: str) -> str | None:
    cleaned = re.sub(
        r"(?:麻烦|请|帮我|给我|我想|想要|看看|一下|全平台|商品|搜索|查找|找找|找|推荐|对比|比较|价格)",
        " ",
        value,
    )
    cleaned = re.sub(r"[\u3001\u3002\uff0c\uff01\uff1f\uff1a\uff1b,:;!?]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:120] or None
