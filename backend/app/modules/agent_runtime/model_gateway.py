from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from app.modules.agent_runtime.handoff_intent import is_explicit_handoff_request

StoreIntent = Literal[
    "general_chat",
    "product_qa",
    "sku_compare",
    "inventory_lookup",
    "policy_qa",
    "order_explain",
    "product_recommend",
    "human_handoff",
]


@dataclass(frozen=True)
class StoreAgentPlan:
    intent: StoreIntent
    search_text: str | None = None


class ModelGatewayError(RuntimeError):
    pass


class StoreModelGateway(Protocol):
    async def plan(self, user_text: str) -> StoreAgentPlan: ...


class DeterministicStoreModelGateway:
    """Development-safe planner; production providers must return the same closed schema."""

    async def plan(self, user_text: str) -> StoreAgentPlan:
        text = _normalize(user_text)
        if not text:
            return StoreAgentPlan("general_chat")
        if is_explicit_handoff_request(user_text):
            return StoreAgentPlan("human_handoff")
        if _contains(text, "推荐", "适合", "预算", "选购"):
            return StoreAgentPlan("product_recommend", search_text=user_text[:120])
        if _contains(text, "对比", "比较", "区别", "差别"):
            return StoreAgentPlan("sku_compare")
        if _contains(text, "库存", "有货", "缺货", "现货", "补货"):
            return StoreAgentPlan("inventory_lookup")
        if _contains(text, "政策", "运费", "退换", "保修", "发票", "客服时间"):
            return StoreAgentPlan("policy_qa")
        if _contains(text, "订单", "付款", "发货", "收货", "物流", "售后"):
            return StoreAgentPlan("order_explain")
        if _contains(text, "商品", "款式", "规格", "参数", "材质", "功能", "怎么用"):
            return StoreAgentPlan("product_qa")
        return StoreAgentPlan("general_chat")


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _contains(value: str, *terms: str) -> bool:
    return any(term in value for term in terms)
