from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from app.modules.agent_runtime.handoff_intent import is_explicit_handoff_request

StoreIntent = Literal[
    "general_chat",
    "product_qa",
    "product_compare",
    "sku_compare",
    "inventory_lookup",
    "policy_qa",
    "order_explain",
    "after_sale_progress",
    "product_recommend",
    "cart_add",
    "human_handoff",
]


@dataclass(frozen=True)
class StoreAgentPlan:
    intent: StoreIntent
    search_text: str | None = None
    confidence: float = 1.0
    required_capabilities: tuple[str, ...] = ()
    missing_slots: tuple[str, ...] = ()
    continuation_of_previous_turn: bool = False
    needs_human: bool = False
    handoff_reason: str | None = None
    response_strategy: Literal["answer", "clarify", "handoff", "refuse"] = "answer"


@dataclass(frozen=True)
class StoreSupervisorSubtask:
    subtask_key: str
    intent: StoreIntent
    objective: str


@dataclass(frozen=True)
class StoreSupervisorGoal:
    goal_key: str
    description: str
    assigned_task_key: str


@dataclass(frozen=True)
class StoreSupervisorPlan:
    tasks: tuple[StoreSupervisorSubtask, ...]
    confidence: float = 1.0
    goal_ledger: tuple[StoreSupervisorGoal, ...] = ()
    coverage_complete: bool = True

    def __post_init__(self) -> None:
        if not self.goal_ledger:
            object.__setattr__(
                self,
                "goal_ledger",
                tuple(
                    StoreSupervisorGoal(
                        goal_key=f"goal_{index}",
                        description=task.objective,
                        assigned_task_key=task.subtask_key,
                    )
                    for index, task in enumerate(self.tasks, start=1)
                ),
            )


class ModelGatewayError(RuntimeError):
    pass


def requests_other_user_data(user_text: str) -> bool:
    """Detect explicit attempts to read another shopper's private data."""

    text = _normalize(user_text)
    subject = _contains(
        text,
        "其他顾客",
        "别的顾客",
        "其他用户",
        "别的用户",
        "其他买家",
        "别的买家",
        "其他买主",
        "别的买主",
        "另一个用户",
        "某个用户",
        "别人的",
        "他人的",
    )
    private_data = _contains(
        text,
        "订单",
        "买过",
        "买了",
        "购买记录",
        "物流",
        "地址",
        "余额",
        "购物车",
        "收藏",
        "账号",
    )
    self_only = _contains(
        text,
        "只告诉我自己的",
        "只看我自己的",
        "只查看我自己的",
        "只查我自己的",
        "只看本人",
        "只查本人",
        "仅限本人",
    )
    negates_other_access = re.search(
        r"(?:其他顾客|别的顾客|其他用户|别的用户|其他买家|别的买家|"
        r"另一个用户|某个用户|别人的|他人的)[^，。\uff1b;]{0,18}"
        r"(?:不能|不可以|不要|不用)(?:查看|查询|查|看)",
        text,
    ) is not None or re.search(
        r"(?:不能|不可以|不要|不用)(?:查看|查询|查|看)[^，。\uff1b;]{0,10}"
        r"(?:其他顾客|别的顾客|其他用户|别的用户|其他买家|别的买家|别人|他人)",
        text,
    ) is not None
    if subject and self_only and negates_other_access:
        subject = False
    explicit_named_user = _contains(
        text, "查看用户", "查询用户", "查用户", "查看账号", "查询账号"
    ) and not _contains(text, "当前用户", "当前账号", "本人", "我自己", "我的")
    named_owner = re.search(
        r"(?P<name>[\u4e00-\u9fff]{2,4})的"
        r"(?:订单|购买记录|物流|收货地址|余额|购物车|收藏)",
        text,
    )
    named_other = bool(
        named_owner
        and not negates_other_access
        and "我" not in named_owner.group("name")
        and not any(
            marker in named_owner.group("name")
            for marker in (
                "最近",
                "当前",
                "刚才",
                "这个",
                "这笔",
                "全部",
                "所有",
                "待评价",
                "评价",
                "可以",
                "可评价",
                "售后",
                "申请售后",
                "能申请",
                "还能",
                "待发货",
                "待付款",
                "运输中",
                "已完成",
                "已取消",
                "相关",
                "对应",
                "余额支付",
                "支付",
                "付款",
                "退款",
                # “第二笔订单的物流”中的“笔订单”描述的是当前用户
                # 已选中的业务资源，并不是另一个人的姓名。命名主体的
                # 宽松正则会截取这三个字，因此必须在安全边界内明确排除。
                "订单",
            )
        )
        and named_owner.group("name")
        not in {
            "我的",
            "自己",
            "本人",
            "当前用户",
            "当前账号",
            "商品",
            "店铺",
            "商城",
            "平台",
        }
    )
    return (subject or explicit_named_user or named_other) and private_data


def requests_cross_store_search(user_text: str) -> bool:
    """Detect a request that exceeds a store Agent's catalog boundary."""

    text = _normalize(user_text)
    other_store = _contains(
        text,
        "其他店铺",
        "别的店铺",
        "其他商家",
        "别的商家",
        "全平台",
        "跨店",
    )
    catalog_request = _contains(
        text,
        "商品",
        "同款",
        "有没有",
        "查",
        "找",
        "推荐",
        "比较",
        "对比",
    )
    return other_store and catalog_request


class StoreModelGateway(Protocol):
    async def plan(self, user_text: str) -> StoreAgentPlan: ...

    async def plan_tasks(self, user_text: str) -> StoreSupervisorPlan: ...


class DeterministicStoreModelGateway:
    """Development-safe planner; production providers must return the same closed schema."""

    async def plan(self, user_text: str) -> StoreAgentPlan:
        text = _normalize(user_text)
        if not text:
            return StoreAgentPlan("general_chat")
        if is_explicit_handoff_request(user_text):
            return StoreAgentPlan("human_handoff")
        if is_store_cart_add_request(text):
            return StoreAgentPlan("cart_add")
        if _contains(
            text,
            "第一笔",
            "第二笔",
            "第三笔",
            "第四笔",
            "第五笔",
            "第1笔",
            "第2笔",
            "第3笔",
            "第4笔",
            "第5笔",
        ):
            return StoreAgentPlan("order_explain")
        if _contains(
            text,
            "第一个",
            "第二个",
            "第三个",
            "第四个",
            "第五个",
            "刚才推荐",
        ):
            if _contains(text, "对比", "比较", "区别", "差别"):
                return StoreAgentPlan("product_compare")
            return StoreAgentPlan("product_qa")
        # “付款后几天发”“默认什么快递”描述的是购买前的商品履约承诺，
        # 不是在查询某一笔已经存在的订单或包裹。这里必须先于“付款/物流”
        # 的订单关键词判断，否则没有下过单的顾客会错误收到“没有可见订单”。
        if is_product_fulfillment_question(user_text):
            return StoreAgentPlan("product_qa")
        if _contains(
            text,
            "最大规格",
            "最大包装",
            "最大款式",
            "最多几支",
            "最多几件",
            "最多几个",
            "最多几本",
        ):
            return StoreAgentPlan("sku_compare")
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
            "体重",
            "身高",
            "腰围",
            "重量",
            "尺寸",
            "成分",
            "面料",
            "版型",
            "型号",
            "适用",
            "兼容",
            "洗涤",
            "适合什么",
            "适合哪",
            "使用场景",
            "介绍",
            "评价",
            "评分",
            "口碑",
        ):
            return StoreAgentPlan("product_qa")
        if _contains(text, "推荐", "适合", "预算", "选购"):
            return StoreAgentPlan("product_recommend", search_text=_store_search_text(user_text))
        if _contains(text, "对比", "比较", "区别", "差别"):
            return StoreAgentPlan("sku_compare")
        if _contains(text, "库存", "有货", "缺货", "现货", "补货"):
            return StoreAgentPlan("inventory_lookup")
        if _contains(
            text,
            "政策",
            "运费",
            "包邮",
            "邮寄",
            "配送方式",
            "发货地",
            "退换",
            "保修",
            "发票",
            "客服时间",
        ):
            return StoreAgentPlan("policy_qa")
        if _contains(
            text,
            "售后进度",
            "退款进度",
            "售后申请到哪",
            "退款申请到哪",
            "退款处理结果",
            "售后处理结果",
        ):
            return StoreAgentPlan("after_sale_progress")
        if _contains(
            text,
            "订单",
            "付款",
            "发货",
            "收货",
            "物流",
            "快递",
            "包裹",
            "运单",
            "到哪",
            "签收",
            "派送",
            "揽收",
            "售后",
            "买过",
            "买了什么",
            "购买记录",
        ):
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

    async def plan_tasks(self, user_text: str) -> StoreSupervisorPlan:
        primary = await self.plan(user_text)
        text = _normalize(user_text)
        intents: list[StoreIntent] = [primary.intent]

        def add(intent: StoreIntent) -> None:
            if intent not in intents:
                intents.append(intent)

        # Keep explicit handoff as one task, but do not let it erase other
        # concrete goals in the same turn (for example "先查库存，再转人工").
        # A greeting remains a true single-goal turn.
        if primary.intent != "general_chat":
            if _contains(text, "库存", "有货", "缺货", "现货", "补货"):
                add("inventory_lookup")
            if _contains(
                text,
                "政策",
                "运费",
                "包邮",
                "配送方式",
                "发货地",
                "退换",
                "保修",
                "发票",
                "客服时间",
            ):
                add("policy_qa")
            if _contains(
                text,
                "售后进度",
                "退款进度",
                "售后申请到哪",
                "退款申请到哪",
                "退款处理结果",
                "售后处理结果",
            ):
                add("after_sale_progress")
            if _contains(
                text,
                "我的订单",
                "我的快递",
                "这笔订单",
                "这单",
                "买过",
                "购买记录",
                "订单到哪",
            ):
                add("order_explain")
            if _contains(text, "推荐", "适合", "预算", "选购"):
                add("product_recommend")
            if is_store_cart_add_request(text):
                add("cart_add")
            if is_product_fulfillment_question(user_text) or _contains(
                text,
                "介绍",
                "参数",
                "材质",
                "成分",
                "尺码",
                "码数",
                "尺寸",
                "怎么用",
                "评价",
                "评分",
                "口碑",
            ):
                add("product_qa")
        if "product_recommend" in intents and "inventory_lookup" in intents:
            # “推荐三件现货商品”中的现货是推荐结果的筛选条件，不是要查询
            # 当前页面商品库存的第二个任务。推荐工具本身会用实时可售库存过滤。
            explicit_current_inventory = _contains(
                text,
                "当前商品库存",
                "这个商品库存",
                "这件商品库存",
                "这款商品库存",
                "另外查库存",
                "同时查库存",
            ) or bool(re.search(r"如果.{0,40}(?:缺货|没货|无货)", text))
            if not explicit_current_inventory:
                intents.remove("inventory_lookup")
        if "sku_compare" in intents and "inventory_lookup" in intents:
            # SKU 对比工具已经从业务主库返回每个款式的价格与实时可售量，
            # 不要再委派一个重复库存任务造成两段近似回答。
            intents.remove("inventory_lookup")
        tasks = tuple(
            StoreSupervisorSubtask(
                subtask_key=f"task_{index}",
                intent=intent,
                objective=_deterministic_store_task_objective(intent, user_text),
            )
            for index, intent in enumerate(intents[:4], start=1)
        )
        return StoreSupervisorPlan(
            tasks,
            confidence=primary.confidence,
            goal_ledger=tuple(
                StoreSupervisorGoal(
                    goal_key=f"goal_{index}",
                    description=task.objective,
                    assigned_task_key=task.subtask_key,
                )
                for index, task in enumerate(tasks, start=1)
            ),
        )


def _deterministic_store_task_objective(intent: StoreIntent, user_text: str) -> str:
    """Give fallback specialists a domain-scoped objective instead of the whole request."""

    if intent == "product_recommend":
        first_goal = re.split(r"(?:\uFF1B|;|另外|同时|并且告诉|再告诉)", user_text, maxsplit=1)[0]
        return (_store_search_text(first_goal) or first_goal.strip())[:200]
    if intent == "product_qa" and is_product_fulfillment_question(user_text):
        normalized = _normalize(user_text)
        requested: list[str] = []
        if _contains(normalized, "几天发", "多久发", "什么时候发", "发货时效", "付款后"):
            requested.append("付款后的发货时效")
        if _contains(normalized, "快递", "物流发", "默认物流"):
            requested.append("默认快递")
        return ("查询当前商品的" + "和".join(requested or ["履约说明"]))[:200]
    labels: dict[StoreIntent, str] = {
        "inventory_lookup": "查询当前商品或明确款式的实时可售库存",
        "policy_qa": "查询本店当前公开生效的服务政策",
        "order_explain": "查询当前用户在本店的订单、物流与售后状态",
        "after_sale_progress": "查询当前用户在本店已提交售后申请的实时进度",
        "product_compare": "对比用户明确引用的本店商品",
        "sku_compare": user_text[:200],
        "human_handoff": "为当前会话转接本店人工客服",
        "general_chat": "回应当前用户消息",
        "product_qa": "回答当前商品的公开资料问题",
        "cart_add": "将用户明确选择的本店商品款式加入本人购物车",
    }
    return labels[intent]


STORE_CAPABILITIES: dict[StoreIntent, tuple[str, ...]] = {
    "general_chat": (),
    "product_qa": ("catalog.get_product",),
    "product_compare": ("catalog.compare_products",),
    "sku_compare": ("catalog.compare_skus",),
    "inventory_lookup": ("catalog.get_inventory_availability",),
    "policy_qa": ("catalog.get_store_policy", "rag.store_policy.search"),
    "order_explain": (
        "order.list_user_store_orders",
        "order.get_store_order_summary",
        "logistics.get_store_order_shipments",
    ),
    "after_sale_progress": ("after_sale.list_user_store_refunds",),
    "product_recommend": (
        "catalog.search_store_products",
        "catalog.compare_products",
    ),
    "cart_add": ("catalog.get_product", "cart.add_item"),
    "human_handoff": ("support.create_store_ticket",),
}


def complete_store_plan(plan: StoreAgentPlan) -> StoreAgentPlan:
    """Apply deterministic server defaults to every planner implementation."""

    capabilities = plan.required_capabilities or STORE_CAPABILITIES[plan.intent]
    is_handoff = plan.intent == "human_handoff"
    return StoreAgentPlan(
        intent=plan.intent,
        search_text=plan.search_text,
        confidence=min(max(plan.confidence, 0.0), 1.0),
        required_capabilities=tuple(dict.fromkeys(capabilities)),
        missing_slots=tuple(dict.fromkeys(plan.missing_slots)),
        continuation_of_previous_turn=plan.continuation_of_previous_turn,
        needs_human=is_handoff,
        handoff_reason=plan.handoff_reason if is_handoff else None,
        response_strategy=(
            "handoff"
            if is_handoff
            else "clarify"
            if plan.missing_slots and plan.confidence < 0.65
            else "answer"
            if plan.response_strategy in {"handoff", "refuse"}
            else plan.response_strategy
        ),
    )


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
        return complete_store_plan(plan)
    text = _normalize(user_text)
    if not text:
        return complete_store_plan(plan)
    if has_product_context and is_product_fulfillment_question(user_text):
        return complete_store_plan(
            StoreAgentPlan(
                "product_qa",
                confidence=max(plan.confidence, 0.95),
                continuation_of_previous_turn=True,
            )
        )
    if plan.intent == "general_chat" and _is_affirmative_follow_up(text):
        if has_product_context:
            return complete_store_plan(
                StoreAgentPlan(
                    "product_qa",
                    confidence=max(plan.confidence, 0.9),
                    continuation_of_previous_turn=True,
                )
            )
        if has_order_context:
            return complete_store_plan(
                StoreAgentPlan(
                    "order_explain",
                    confidence=max(plan.confidence, 0.9),
                    continuation_of_previous_turn=True,
                )
            )
    if _is_general_chat(text):
        return complete_store_plan(plan)
    if plan.intent == "general_chat" and _looks_like_substantive_request(text, user_text):
        return complete_store_plan(StoreAgentPlan("product_qa", confidence=plan.confidence))
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
            "这把",
            "这条",
            "它",
            "该商品",
            "当前商品",
            "订单里的",
            "刚买的",
            "适合吗",
            "适不适合",
        )
        and not _contains(text, "推荐别的", "还有什么", "类似商品", "换一个", "其他商品")
    ):
        return complete_store_plan(StoreAgentPlan("product_qa", confidence=plan.confidence))
    if (
        has_product_context
        and plan.intent == "product_recommend"
        and _contains(
            text,
            "体重",
            "身高",
            "腰围",
            "胸围",
            "臀围",
            "长度",
            "重量",
            "材质",
            "面料",
            "尺寸",
            "尺码",
            "码数",
        )
        and not _contains(text, "推荐", "还有什么", "类似商品", "换一个", "其他商品")
    ):
        return complete_store_plan(
            StoreAgentPlan(
                "product_qa",
                confidence=max(plan.confidence, 0.9),
                continuation_of_previous_turn=True,
            )
        )
    return complete_store_plan(plan)


def is_product_fulfillment_question(value: str) -> bool:
    """Distinguish product promises from an existing order's live logistics.

    Product detail pages often describe dispatch lead time and the store's default
    courier.  Those questions must read the current product (including merchant-
    corrected OCR descriptions).  Actual parcel status remains an order intent.
    """

    text = _normalize(value)
    if not text:
        return False
    actual_order_markers = (
        "订单",
        "包裹",
        "运单",
        "物流单号",
        "快递单号",
        "物流进度",
        "物流轨迹",
        "到哪里",
        "到哪了",
        "签收",
        "派送",
        "揽收",
        "我买的",
        "我下单",
        "已经下单",
        "下单了",
        "已经付款",
        "已付款",
        "支付成功",
        "怎么还没",
        "还没发货",
        "没有发货",
        "发货了吗",
        "是否发货",
    )
    if _contains(text, *actual_order_markers):
        return False
    dispatch_markers = (
        "几天内发",
        "多久发",
        "多长时间发",
        "什么时候发货",
        "什么时候发出",
        "何时发货",
        "何时发出",
        "发货时效",
        "付款后几天",
        "付款后多久",
        "下单后几天",
        "下单后多久",
        "拍下后几天",
        "拍下后多久",
    )
    courier_markers = (
        "发什么快递",
        "什么快递发",
        "用什么快递",
        "走什么快递",
        "默认快递",
        "哪个快递",
        "哪家快递",
        "发哪家快递",
        "什么物流发",
        "用什么物流",
        "走什么物流",
        "默认物流",
        "哪个物流",
        "哪家物流",
    )
    return _contains(text, *dispatch_markers, *courier_markers)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def is_store_cart_add_request(value: str) -> bool:
    text = _normalize(value)
    return re.search(
        r"(?:加入|加到|放进|放到|加进)(?:我的|我自己的|本人|我)?购物车",
        text,
    ) is not None


def _contains(value: str, *terms: str) -> bool:
    return any(term in value for term in terms)


def _store_search_text(value: str) -> str | None:
    cleaned = re.sub(
        r"(?:麻烦|请|帮我|给我|我想|想要|看看|一下|你们店|本店|店里|商品|推荐|选购)",
        " ",
        value,
    )
    cleaned = re.sub(r"[\u3001\u3002\uff0c\uff01\uff1a\uff1b,:;!?]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:120] or None


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
        value.startswith(prefix) for prefix in ("谢谢你", "感谢你", "辛苦了", "你好呀", "您好呀")
    )


def _is_affirmative_follow_up(value: str) -> bool:
    return value in {
        "好",
        "好的",
        "好呀",
        "可以",
        "行",
        "行啊",
        "继续",
        "嗯",
        "嗯嗯",
        "ok",
        "okay",
    }


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
