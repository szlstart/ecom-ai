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
        if _contains(
            text,
            "尺码",
            "码数",
            "最大码",
            "最小码",
            "多少码",
            "几码",
            "多大码",
            "最大号",
            "最小号",
            "颜色",
            "重量",
            "尺寸",
            "成分",
            "面料",
            "版型",
            "型号",
            "适用",
            "兼容",
            "洗涤",
        ):
            return StoreAgentPlan("product_qa")
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
        if _contains(
            text,
            "商品",
            "衣服",
            "鞋",
            "款式",
            "规格",
            "参数",
            "材质",
            "功能",
            "怎么用",
            "介绍",
        ):
            return StoreAgentPlan("product_qa")
        return StoreAgentPlan("general_chat")


def refine_store_plan_for_context(
    plan: StoreAgentPlan,
    user_text: str,
    *,
    has_product_context: bool,
    has_order_context: bool = False,
) -> StoreAgentPlan:
    """Repair an obviously under-classified plan using trusted page context.

    The planner does not receive database identifiers. When the server already binds a
    current product, natural follow-ups such as ``这个最大码多大`` are product questions
    even if a provider mistakes them for general chat. Greetings, thanks and capability
    questions deliberately remain small talk.
    """

    if not (has_product_context or has_order_context):
        return plan
    text = _normalize(user_text)
    if not text or _is_general_chat(text):
        return plan
    if plan.intent == "general_chat" and _looks_like_substantive_request(text, user_text):
        return StoreAgentPlan("product_qa")
    if (
        plan.intent == "product_recommend"
        and _contains(
            text,
            "这个",
            "这件",
            "这款",
            "这支",
            "这本",
            "这盒",
            "这套",
            "这双",
            "这台",
            "该商品",
            "当前商品",
            "订单里的",
            "刚买的",
        )
        and not _contains(text, "推荐别的", "还有什么", "类似商品", "换一个", "其他商品")
    ):
        return StoreAgentPlan("product_qa")
    return plan


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _contains(value: str, *terms: str) -> bool:
    return any(term in value for term in terms)


def _is_general_chat(value: str) -> bool:
    if value in {
        "你好",
        "您好",
        "hello",
        "hi",
        "在吗",
        "谢谢",
        "感谢",
        "再见",
        "你是谁",
        "你能做什么",
        "你可以做什么",
    }:
        return True
    return any(
        value.startswith(prefix)
        for prefix in ("谢谢你", "感谢你", "辛苦了", "你好呀", "您好呀")
    )


def _looks_like_substantive_request(normalized: str, original: str) -> bool:
    if "?" in original or "\uff1f" in original:
        return True
    return _contains(
        normalized,
        "这个",
        "这件",
        "这款",
        "该商品",
        "当前商品",
        "什么",
        "多少",
        "多大",
        "哪个",
        "哪些",
        "怎么",
        "如何",
        "是否",
        "能不能",
        "可不可以",
        "有没有",
        "介绍",
        "说说",
        "讲讲",
        "尺码",
        "规格",
        "参数",
        "材质",
        "颜色",
        "尺寸",
        "重量",
        "功能",
    )
