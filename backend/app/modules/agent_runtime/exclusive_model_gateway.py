from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from app.modules.agent_runtime.handoff_intent import is_explicit_handoff_request

ExclusiveIntent = Literal[
    "general_chat",
    "policy_qa",
    "product_search",
    "product_compare",
    "personalized_recommendation",
    "order_lookup",
    "cart_lookup",
    "cart_add",
    "cart_update",
    "cart_remove",
    "cart_clear",
    "checkout_preview",
    "address_lookup",
    "wallet_lookup",
    "favorites_lookup",
    "favorite_update",
    "review_draft",
    "memory_lookup",
    "logistics_lookup",
    "refund_precheck",
    "refund_eligibility",
    "refund_progress",
    "human_handoff",
]


@dataclass(frozen=True)
class ExclusiveAgentPlan:
    intent: ExclusiveIntent
    search_text: str | None = None
    confidence: float = 1.0
    required_capabilities: tuple[str, ...] = ()
    missing_slots: tuple[str, ...] = ()
    continuation_of_previous_turn: bool = False
    needs_human: bool = False
    handoff_reason: str | None = None
    response_strategy: Literal["answer", "clarify", "handoff", "refuse"] = "answer"


@dataclass(frozen=True)
class ExclusiveSupervisorSubtask:
    subtask_key: str
    intent: ExclusiveIntent
    objective: str


@dataclass(frozen=True)
class ExclusiveSupervisorGoal:
    goal_key: str
    description: str
    assigned_task_key: str


@dataclass(frozen=True)
class ExclusiveSupervisorPlan:
    tasks: tuple[ExclusiveSupervisorSubtask, ...]
    confidence: float = 1.0
    goal_ledger: tuple[ExclusiveSupervisorGoal, ...] = ()
    coverage_complete: bool = True

    def __post_init__(self) -> None:
        if not self.goal_ledger:
            object.__setattr__(
                self,
                "goal_ledger",
                tuple(
                    ExclusiveSupervisorGoal(
                        goal_key=f"goal_{index}",
                        description=task.objective,
                        assigned_task_key=task.subtask_key,
                    )
                    for index, task in enumerate(self.tasks, start=1)
                ),
            )


class ExclusiveModelGateway(Protocol):
    async def plan(self, user_text: str) -> ExclusiveAgentPlan: ...

    async def plan_tasks(self, user_text: str) -> ExclusiveSupervisorPlan: ...


class DeterministicExclusiveModelGateway:
    async def plan(self, user_text: str) -> ExclusiveAgentPlan:
        text = _strip_negated_intent_phrases(re.sub(r"\s+", "", user_text).casefold())
        if is_explicit_handoff_request(user_text):
            return ExclusiveAgentPlan("human_handoff")
        if _asks_logistics_update_policy(user_text):
            return ExclusiveAgentPlan("policy_qa")
        if _asks_general_funds_policy(user_text):
            return ExclusiveAgentPlan("policy_qa")
        if (
            _contains(text, "签收", "收货")
            and _contains(text, "确认收货")
            and _contains(text, "自动完成", "自动确认", "多久", "什么时候", "几天")
        ):
            return ExclusiveAgentPlan("policy_qa")
        if _contains(text, "暂停营业", "店铺暂停", "暂停经营") and _contains(
            text, "结算", "下单", "购买", "购物车"
        ):
            return ExclusiveAgentPlan("policy_qa")
        if "搜索框" in text and _contains(text, "你和", "区别", "不同", "比搜索"):
            return ExclusiveAgentPlan("general_chat")
        if (
            ("订单" in text or _contains(text, "哪一笔", "哪笔", "哪几笔"))
            and _contains(text, "哪些", "哪几笔", "哪一笔", "哪笔")
            and _contains(text, "可以取消", "确认收货", "可以评价", "申请售后", "能退款")
        ):
            return ExclusiveAgentPlan("order_lookup")
        if _requests_order_state_overview_request(text):
            return ExclusiveAgentPlan("order_lookup")
        # Constraint corrections are a continuation of catalogue work even when
        # the shopper omits words such as “商品” or “推荐”.  Treating “季节不限，
        # 其余条件不变” as general chat makes the model describe a future search
        # instead of actually performing it.
        if "不变" in text and _contains(
            text,
            "季节",
            "颜色",
            "预算",
            "价格",
            "品类",
            "尺码",
            "体重",
            "斤",
            "排序",
        ):
            return ExclusiveAgentPlan(
                "product_search",
                _search_text(user_text),
                continuation_of_previous_turn=True,
            )
        has_cart_context = _contains(
            text, "购物车", "购物袋", "购务车", "刚加的", "刚加入", "刚放进"
        )
        if _requests_cart_add_verification(text):
            return ExclusiveAgentPlan("cart_lookup")
        if _requests_checkout_preview(text):
            return ExclusiveAgentPlan("checkout_preview")
        if _requests_favorite_update(text):
            return ExclusiveAgentPlan("favorite_update")
        if _requests_review_draft(text):
            return ExclusiveAgentPlan("review_draft")
        if _asks_order_spend_summary_request(text):
            return ExclusiveAgentPlan("order_lookup")
        if _requests_search_then_cart_add(text):
            return ExclusiveAgentPlan("product_search", _search_text(user_text))
        cart_hypothetical = has_cart_context and _contains(
            text, "如果", "假设", "假如", "试算", "只计算", "先别修改", "不要修改"
        )
        if cart_hypothetical:
            return ExclusiveAgentPlan("cart_lookup")
        if has_cart_context and _contains(
            text,
            "清空",
            "都删",
            "全部删除",
            "全删",
            "删光",
            "内容全部移除",
            "所有商品移除",
            "全部商品移除",
        ):
            return ExclusiveAgentPlan("cart_clear")
        if has_cart_context and _contains(
            text,
            "移出购物车",
            "从购物车移出",
            "从购物车移除",
            "从购物车拿掉",
            "从购物车删除",
            "从购物车删",
            "删掉",
            "删除这件",
            "移除",
            "拿掉",
        ):
            return ExclusiveAgentPlan("cart_remove")
        if has_cart_context and _contains(
            text,
            "数量改成",
            "数量改为",
            "改成",
            "改为",
            "改回",
            "调成",
            "调整为",
            "恢复成",
            "恢复为",
            "恢复到",
        ):
            return ExclusiveAgentPlan("cart_update")
        if (
            _contains(text, "加入购物车", "加到购物车", "放进购物车", "放到购物车")
            and not _requests_cart_add_verification(text)
        ):
            return ExclusiveAgentPlan("cart_add")
        if _asks_multi_order_logistics_comparison(text):
            return ExclusiveAgentPlan("logistics_lookup")
        # “第一个/第二个” is not inherently a product reference.  Once an
        # order list has been shown, shoppers naturally say “第二笔多少钱” or
        # “第一笔到哪了”.  Route transaction language before the product-card
        # ordinal shortcut so the executor can resolve the recent order card.
        if _contains(text, "第一笔", "第二笔", "第三笔", "第四笔", "第五笔"):
            if _contains(
                text,
                "物流",
                "快递",
                "包裹",
                "签收",
                "位置",
                "到哪",
                "送达",
                "没到",
                "到没到",
                "还没到",
                "什么时候到",
                "什么时候能到",
                "多久到",
                "几天到",
                "预计到",
            ):
                return ExclusiveAgentPlan("logistics_lookup")
            if _contains(text, "退款", "退货", "售后"):
                if _contains(
                    text,
                    "退款资格",
                    "售后资格",
                    "资格预检",
                    "能否退款",
                    "可以退款",
                    "是否能退款",
                    "是否可以退款",
                    "能退款吗",
                    "能退吗",
                    "可退款吗",
                    "可以退吗",
                    "能不能退款",
                    "只检查",
                    "不要提交",
                    "别提交",
                    "先不提交",
                ):
                    return ExclusiveAgentPlan("refund_precheck")
                return ExclusiveAgentPlan("refund_eligibility")
            return ExclusiveAgentPlan("order_lookup")
        if _mentions_multiple_product_ordinals(text):
            return ExclusiveAgentPlan("product_compare")
        if (
            "库存" in text
            and _contains(text, "这三件", "这三个", "这几件", "这几个", "刚才这三件", "刚才这些")
            and _contains(text, "库存最少", "库存最低", "库存最多", "库存最高")
        ):
            return ExclusiveAgentPlan("product_compare")
        if "库存" in text and _contains(
            text,
            "库存最少",
            "库存最低",
            "库存最多",
            "库存最高",
            "哪一款",
            "哪款",
        ):
            return ExclusiveAgentPlan(
                "product_search",
                confidence=1.0,
                continuation_of_previous_turn=True,
            )
        if _contains(
            text,
            "第一个",
            "第二个",
            "第三个",
            "第四个",
            "第五个",
            "第1个",
            "第2个",
            "第3个",
            "第4个",
            "第5个",
            "刚才推荐",
        ):
            if _contains(
                text,
                "物流",
                "快递",
                "包裹",
                "签收",
                "运输中",
                "送达",
                "到哪",
            ):
                return ExclusiveAgentPlan("logistics_lookup")
            if _contains(text, "订单", "付款", "收货", "售后", "退款"):
                return ExclusiveAgentPlan("order_lookup")
            if _contains(text, "对比", "比较", "区别", "差别"):
                return ExclusiveAgentPlan("product_compare")
            return ExclusiveAgentPlan("product_search", _search_text(user_text))
        if _contains(
            text,
            "便宜的那个",
            "更便宜的",
            "最便宜的",
            "价格低的那个",
            "贵的那个",
            "更贵的",
            "最贵的",
            "价格高的那个",
        ):
            return ExclusiveAgentPlan("product_search", _search_text(user_text))
        if _contains(text, "这两件", "这两个", "这几件", "这几款") and _contains(
            text, "哪个", "哪件", "哪款", "对比", "比较", "区别", "适合"
        ):
            return ExclusiveAgentPlan("product_compare")
        if _contains(text, "对比", "比较", "区别", "差别"):
            return ExclusiveAgentPlan("product_compare")
        if _contains(
            text,
            "最近一单",
            "最近下的那一单",
            "最近下的订单",
            "最近一笔",
            "最新一单",
            "刚下的订单",
        ):
            return ExclusiveAgentPlan("order_lookup")
        if _contains(text, "搜索", "查找", "搜一下", "找找") and not _contains(
            text, "订单", "物流", "快递", "售后进度", "退款进度"
        ):
            return ExclusiveAgentPlan("product_search", _search_text(user_text))
        # A shopper can name a concrete product directly without saying
        # “搜索” or “商品”, for example “请介绍三端联动验收笔记本”.  Treat that
        # as a catalogue read instead of falling through to the generic welcome
        # reply.  Requests about the assistant itself or another business
        # domain are deliberately excluded here and continue through their
        # dedicated routes below.
        if (
            re.search(r"(?:请|帮我|给我)?(?:介绍(?:一下)?|讲讲|了解一下).{2,}", text)
            and not _contains(
                text,
                "你自己",
                "你的能力",
                "服务范围",
                "平台规则",
                "订单",
                "物流",
                "快递",
                "售后",
                "退款",
            )
        ):
            return ExclusiveAgentPlan("product_search", _search_text(user_text))
        if "售后" in text and _contains(text, "到哪", "哪一步", "进度", "状态", "处理"):
            return ExclusiveAgentPlan("refund_progress")
        if _requests_memory_lookup(text) and not _contains(
            text, "推荐", "适合我", "按我的偏好选", "找商品"
        ):
            return ExclusiveAgentPlan("memory_lookup")
        if (
            _contains(text, "推荐", "适合我")
            or ("偏好" in text and _contains(text, "商品", "选购", "买什么", "找"))
        ) and not _contains(text, "订单", "物流", "快递", "售后", "退款方式", "退款进度"):
            return ExclusiveAgentPlan("personalized_recommendation", _search_text(user_text))
        if _contains(text, "我买的", "我买过的", "买过的", "我在") and _contains(
            text,
            "发货",
            "签收",
            "收货",
            "评价",
            "订单状态",
        ):
            # “我在某店买的铅笔发货了吗” is an order query, not a public
            # catalogue search for pencils.
            return ExclusiveAgentPlan("order_lookup")
        # A shopper may follow a read-only refund precheck with a combined
        # question such as “为什么最多是 19.10 元，还在运输中要先确认收货吗”.
        # “退款” is often omitted because the active order card already carries
        # that context. Keep this on the read-only precheck path so both parts
        # are answered from the same live order projection.
        if _contains(
            text,
            "为什么最多",
            "最多是",
            "最多只能退",
            "最多能退",
            "申请上限",
            "可退上限",
        ) and _contains(text, "确认收货", "运输中", "创建申请", "提交申请"):
            return ExclusiveAgentPlan("refund_precheck")
        if "自动确认收货" in text and _contains(
            text, "多久", "几天", "什么时候", "规则", "政策", "时效"
        ):
            return ExclusiveAgentPlan("policy_qa")
        if (
            "退款" in text
            and _contains(
                text,
                "多久到账",
                "多久到",
                "多久能退",
                "要多久",
                "多长时间",
                "时效",
                "时间",
                "几天",
                "规则",
                "保证",
                "承诺",
                "一定",
                "所有退款",
                "固定",
                "通常",
                "一般",
                "到账吗",
            )
            and not _contains(text, "我的退款", "这笔退款", "当前退款", "退款进度")
        ):
            return ExclusiveAgentPlan("policy_qa")
        if _contains(text, "物流", "快递", "包裹") and _contains(
            text,
            "每5秒",
            "每五秒",
            "每隔5秒",
            "每隔五秒",
            "固定几秒",
            "自动更新",
            "自动推进",
            "更新机制",
            "更新规则",
            "多久更新一次",
        ):
            return ExclusiveAgentPlan("policy_qa")
        if _contains(
            text,
            "退款进度",
            "售后进度",
            "售后中的",
            "售后到哪一步",
            "售后处理到哪",
            "售后怎么样了",
            "退款到哪",
            "退款状态",
            "为什么还没退款",
            "为什么还没有退款",
            "为什么没退款",
            "为何还没退款",
            "还没退款",
            "还没有退款",
            "退款还没到账",
            "退款还没有到账",
            "我的退款",
            "这笔退款",
            "当前退款",
            "进行中的退款",
            "正在退款",
            "退款中的",
        ):
            return ExclusiveAgentPlan("refund_progress")
        if _requests_refund_draft(text):
            return ExclusiveAgentPlan("refund_eligibility")
        if "退款" in text and _contains(
            text,
            "退款草稿",
            "申请草稿",
            "准备仅退款",
            "准备退款",
            "生成退款",
            "创建退款",
        ):
            # “准备草稿但不要提交” explicitly asks for the reversible draft
            # and confirmation card. “不要提交” must not reduce it to a mere
            # eligibility check.
            return ExclusiveAgentPlan("refund_eligibility")
        if _contains(
            text,
            "退款资格",
            "售后资格",
            "资格预检",
            "资格检查",
            "能否退款",
            "可以退款",
            "是否能退款",
            "是否可以退款",
            "是否具备退款",
            "能退款吗",
            "能退吗",
            "可退款吗",
            "可以退吗",
            "有没有退款资格",
            "最多能退",
            "能退多少钱",
            "可退金额",
            "能不能退款",
            "只检查退款",
        ):
            return ExclusiveAgentPlan("refund_precheck")
        if "退款" in text and _contains(text, "只检查", "不要提交", "别提交", "先不提交"):
            return ExclusiveAgentPlan("refund_precheck")
        if _contains(
            text,
            "申请退款",
            "我要退款",
            "帮我退款",
            "给我退款",
            "退货退款",
            "仅退款",
            "发起售后",
            "退款",
            "退货",
        ):
            return ExclusiveAgentPlan("refund_eligibility")
        if _contains(
            text,
            "物流",
            "快递",
            "包裹",
            "到哪",
            "送达",
            "没到",
            "到没到",
            "还没到",
            "什么时候到",
            "什么时候能到",
            "多久到",
            "几天到",
            "预计到",
        ):
            return ExclusiveAgentPlan("logistics_lookup")
        if _contains(text, "购物车", "购物袋", "购务车") and _contains(
            text, "清空", "都删", "全部删除", "全删", "删光"
        ):
            return ExclusiveAgentPlan("cart_clear")
        if _contains(text, "购物车", "购物袋", "购务车"):
            return ExclusiveAgentPlan("cart_lookup")
        if _contains(
            text, "收货地址", "地址簿", "默认地址", "我的地址"
        ) or _requests_profile_lookup(text):
            return ExclusiveAgentPlan("address_lookup")
        if "充值" in text and not _describes_wallet_transaction_history(text):
            return ExclusiveAgentPlan("policy_qa")
        if _asks_payment_method_question(text):
            return ExclusiveAgentPlan("order_lookup")
        if _requests_wallet_lookup(text):
            return ExclusiveAgentPlan("wallet_lookup")
        if _contains(text, "收藏", "关注的店铺", "喜欢的商品"):
            return ExclusiveAgentPlan("favorites_lookup")
        if _contains(
            text,
            "订单",
            "付款",
            "收货",
            "购买记录",
            "买过什么",
            "买过哪些",
            "买了什么",
            "购买过什么",
            "下过什么单",
        ):
            return ExclusiveAgentPlan("order_lookup")
        if _contains(text, "库存", "有货", "缺货", "款式", "规格", "尺码", "码数"):
            return ExclusiveAgentPlan("product_search", _search_text(user_text))
        if re.search(r"\d+(?:\.\d{1,2})?元?(?:以内|以下|以上|起)", text) and _contains(
            text,
            "文具",
            "办公用品",
            "女装",
            "男装",
            "童装",
            "女鞋",
            "男鞋",
            "衣服",
            "裤子",
            "上衣",
        ):
            return ExclusiveAgentPlan("product_search", _search_text(user_text))
        if _contains(text, "商品", "搜索", "找", "买", "价格", "对比"):
            return ExclusiveAgentPlan("product_search", _search_text(user_text))
        if _contains(text, "规则", "政策", "平台", "运费", "退换", "保修", "发票"):
            return ExclusiveAgentPlan("policy_qa")
        return ExclusiveAgentPlan("general_chat")

    async def plan_tasks(self, user_text: str) -> ExclusiveSupervisorPlan:
        compact = re.sub(r"\s+", "", user_text).casefold()
        catalog_groups = _requested_catalog_groups(user_text)
        if len(catalog_groups) >= 2:
            return ExclusiveSupervisorPlan(
                tuple(
                    ExclusiveSupervisorSubtask(
                        subtask_key=f"task_{index}",
                        intent="personalized_recommendation",
                        objective=objective,
                    )
                    for index, objective in enumerate(catalog_groups[:4], start=1)
                )
            )
        if _asks_logistics_update_policy(user_text):
            return ExclusiveSupervisorPlan(
                (
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_1",
                        intent="policy_qa",
                        objective=user_text[:200],
                    ),
                )
            )
        if _asks_order_ordinal_logistics(compact):
            return ExclusiveSupervisorPlan(
                (
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_1",
                        intent="logistics_lookup",
                        objective=user_text[:200],
                    ),
                )
            )
        if _requests_refund_draft(compact):
            return ExclusiveSupervisorPlan(
                (
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_1",
                        intent="refund_eligibility",
                        objective=user_text[:200],
                    ),
                )
            )
        if _requests_checkout_preview(compact) and _requests_wallet_lookup(compact):
            return ExclusiveSupervisorPlan(
                (
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_1",
                        intent="checkout_preview",
                        objective=user_text[:200],
                    ),
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_2",
                        intent="wallet_lookup",
                        objective="查询当前用户的账户余额",
                    ),
                )
            )
        if _requests_checkout_preview(compact):
            return ExclusiveSupervisorPlan(
                (
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_1",
                        intent="checkout_preview",
                        objective=user_text[:200],
                    ),
                )
            )
        if _requests_favorite_update(compact):
            return ExclusiveSupervisorPlan(
                (
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_1",
                        intent="favorite_update",
                        objective=user_text[:200],
                    ),
                )
            )
        if _requests_review_draft(compact):
            return ExclusiveSupervisorPlan(
                (
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_1",
                        intent="review_draft",
                        objective=user_text[:200],
                    ),
                )
            )
        if _asks_general_funds_policy(user_text):
            # “余额能否提现 / 退款退到哪里” describes platform
            # rules, not the shopper's current wallet or a particular order.  A
            # lexical split on “余额/订单/退款” used to fan this out into
            # unrelated account and order cards.  Keep it as one RAG-grounded
            # policy task unless the user explicitly points at their own record.
            return ExclusiveSupervisorPlan(
                (
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_1",
                        intent="policy_qa",
                        objective=user_text[:200],
                    ),
                )
            )
        if _requests_search_then_cart_add(user_text):
            return ExclusiveSupervisorPlan(
                (
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_1",
                        intent="product_search",
                        objective=user_text[:200],
                    ),
                )
            )
        if _asks_order_logistics_status_difference(user_text):
            return ExclusiveSupervisorPlan(
                (
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_1",
                        intent="logistics_lookup",
                        objective=user_text[:200],
                    ),
                )
            )
        if _asks_multi_order_logistics_comparison(user_text):
            return ExclusiveSupervisorPlan(
                (
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_1",
                        intent="logistics_lookup",
                        objective=user_text[:200],
                    ),
                )
            )
        if _asks_single_order_logistics_question(user_text):
            return ExclusiveSupervisorPlan(
                (
                    ExclusiveSupervisorSubtask(
                        subtask_key="task_1",
                        intent="logistics_lookup",
                        objective=user_text[:200],
                    ),
                )
            )
        clauses = [
            part.strip()
            for part in re.split(
                r"(?:，|、|。|;|并且|同时|另外|然后|再帮我|以及|和我的|与我的|跟我的)",
                user_text,
            )
            if part.strip()
        ]
        tasks: list[ExclusiveSupervisorSubtask] = []
        for index, clause in enumerate(clauses[:6]):
            plan = await self.plan(clause)
            if plan.intent == "general_chat" or any(item.intent == plan.intent for item in tasks):
                continue
            tasks.append(
                ExclusiveSupervisorSubtask(
                    subtask_key=f"task_{index + 1}",
                    intent=plan.intent,
                    objective=clause[:200],
                )
            )
        # A provider planner may collapse a sentence that names several business
        # domains into only the last one it sees.  Treat explicit domain phrases
        # as a coverage contract: the Supervisor is still free to decide how to
        # solve each task, but it must not silently drop an order list, logistics
        # lookup, refund progress, cart, or address request stated by the user.
        coverage = _compound_intent_coverage(user_text)
        by_intent = {task.intent: task for task in tasks}
        for intent, objective in coverage:
            existing = by_intent.get(intent)
            replacement = ExclusiveSupervisorSubtask(
                subtask_key=existing.subtask_key if existing is not None else "",
                intent=intent,
                objective=objective,
            )
            if existing is not None:
                tasks[tasks.index(existing)] = replacement
                by_intent[intent] = replacement
                continue
            replacement = ExclusiveSupervisorSubtask(
                subtask_key=f"task_{len(tasks) + 1}",
                intent=intent,
                objective=objective,
            )
            tasks.append(replacement)
            by_intent[intent] = replacement
        if "cart_add" in by_intent and re.search(r"第(?:[一二三四五]|[1-5])个", user_text):
            tasks = [task for task in tasks if task.intent != "product_search"]
            by_intent = {task.intent: task for task in tasks}
        if any(task.intent in {"cart_update", "cart_remove"} for task in tasks):
            # The mutation result already returns the fresh cart projection.
            # A separately parsed cart read is redundant and, before the final
            # key normalization below, could inherit the same subtask key.
            tasks = [task for task in tasks if task.intent != "cart_lookup"]
            by_intent = {task.intent: task for task in tasks}
        if len(coverage) >= 2:
            coverage_order = {intent: index for index, (intent, _objective) in enumerate(coverage)}
            tasks.sort(key=lambda task: coverage_order.get(task.intent, len(coverage) + 1))
            tasks = [
                ExclusiveSupervisorSubtask(
                    subtask_key=f"task_{index}",
                    intent=task.intent,
                    objective=task.objective,
                )
                for index, task in enumerate(tasks, start=1)
            ]
        catalog_intents = {
            "product_search",
            "product_compare",
            "personalized_recommendation",
        }
        scoped_record_intents = {
            "cart_lookup",
            "cart_add",
            "cart_update",
            "cart_remove",
            "cart_clear",
            "favorites_lookup",
            "favorite_update",
            "order_lookup",
            "address_lookup",
            "wallet_lookup",
        }
        if any(task.intent in scoped_record_intents for task in tasks):
            # “核对购物车，只告诉我商品、款式、数量和总额” and
            # “核对收藏列表，确认商品已经恢复” refer to fields/items inside
            # an already-scoped business record. Punctuation must not turn the
            # bare word “商品/款式” into a second global catalogue search.
            tasks = [
                task
                for task in tasks
                if not (
                    task.intent == "product_search"
                    and not _contains(
                        task.objective,
                        "搜索",
                        "查找",
                        "推荐",
                        "找商品",
                        "全平台",
                        "加入购物车",
                    )
                    and _contains(
                        task.objective,
                        "商品",
                        "款式",
                        "规格",
                        "数量",
                        "总额",
                        "金额",
                        "价格",
                    )
                )
            ]
        mergeable_catalog_intents = {"product_search", "personalized_recommendation"}
        mergeable_catalog_tasks = [
            task for task in tasks if task.intent in mergeable_catalog_intents
        ]
        if len(mergeable_catalog_tasks) >= 2:
            # In a compound account request, punctuation can split “推荐两件20元
            # 以内文具，按价格升序” into two catalogue tasks. They are one set
            # of search constraints and must reach one specialist together;
            # otherwise the result count/sort constraints are silently lost.
            merged = ExclusiveSupervisorSubtask(
                subtask_key=mergeable_catalog_tasks[0].subtask_key,
                intent=(
                    "personalized_recommendation"
                    if any(
                        task.intent == "personalized_recommendation"
                        for task in mergeable_catalog_tasks
                    )
                    else "product_search"
                ),
                objective="，".join(task.objective for task in mergeable_catalog_tasks)[:200],
            )
            first_index = tasks.index(mergeable_catalog_tasks[0])
            tasks = [task for task in tasks if task not in mergeable_catalog_tasks]
            tasks.insert(first_index, merged)
            tasks = [
                ExclusiveSupervisorSubtask(
                    subtask_key=f"task_{index}",
                    intent=task.intent,
                    objective=task.objective,
                )
                for index, task in enumerate(tasks, start=1)
            ]
        if any(task.intent == "personalized_recommendation" for task in tasks):
            # The personalized catalogue specialist performs the authorized
            # memory recall itself before searching. A second parallel memory
            # task would race with catalogue search and could display a memory
            # card while returning products that never used that preference.
            tasks = [task for task in tasks if task.intent != "memory_lookup"]
        if len(tasks) >= 2 and all(task.intent in catalog_intents for task in tasks):
            # Commas often separate catalogue constraints rather than independent
            # jobs, for example “适合考试、20 元以内，按价格升序”. Keep those
            # constraints in one specialist request so limits and sorting are not
            # lost when two catalogue results are reduced together.
            overall = await self.plan(user_text)
            if overall.intent in catalog_intents:
                return ExclusiveSupervisorPlan(
                    (
                        ExclusiveSupervisorSubtask(
                            subtask_key="task_1",
                            intent=overall.intent,
                            objective=user_text[:200],
                        ),
                    )
                )
        tasks = [
            ExclusiveSupervisorSubtask(
                subtask_key=f"task_{index}",
                intent=task.intent,
                objective=task.objective,
            )
            for index, task in enumerate(tasks[:6], start=1)
        ]
        return ExclusiveSupervisorPlan(tuple(tasks))


def _requested_catalog_groups(user_text: str) -> tuple[str, ...]:
    """Extract explicitly requested independent recommendation groups.

    A phrase such as ``适合考试的文具和便携笔记本，分成两组推荐`` is two
    result sets, not two constraints for one search.  This helper only activates
    when the user explicitly asks for groups, leaving ordinary ``A 和 B``
    catalogue constraints on the normal semantic planning path.
    """

    group_marker = re.search(
        r"(?:分成|分为|按)(?:两|二|2|三|3|四|4)组(?:分别)?(?:推荐|展示|列出|查找)?",
        user_text,
    )
    if group_marker is None:
        return ()
    punctuation = " ,.;\uFF0C\u3002\uFF1B"
    request = user_text[: group_marker.start()].strip(punctuation)
    request = re.sub(
        r"^(?:请|麻烦)?(?:帮我|给我|替我)?(?:找|查找|搜索|推荐|看看|列出)?",
        "",
        request,
    ).strip(punctuation)
    parts = [
        part.strip(punctuation)
        for part in re.split(r"(?:以及|并列的|和|与|、)", request)
        if part.strip(punctuation)
    ]
    if len(parts) < 2:
        return ()
    objectives = tuple(f"推荐{part}"[:200] for part in parts[:4] if len(part) >= 2)
    return objectives if len(objectives) >= 2 else ()


def _asks_order_logistics_status_difference(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    compares = any(marker in compact for marker in ("为什么", "不一致", "矛盾", "以哪个为准"))
    return (
        compares
        and "订单" in compact
        and any(marker in compact for marker in ("物流", "快递", "签收", "运输中"))
    )


def _asks_logistics_update_policy(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    logistics = any(marker in compact for marker in ("物流", "快递", "包裹"))
    mechanism = any(
        marker in compact
        for marker in (
            "每5秒",
            "每五秒",
            "固定几秒",
            "自动更新",
            "自动推进",
            "谁更新物流节点",
            "谁来更新物流节点",
            "由谁更新物流节点",
            "更新机制",
            "更新规则",
        )
    )
    asks_live_position = any(
        marker in compact
        for marker in ("我的包裹到哪", "我的物流到哪", "这笔物流到哪", "查物流进度")
    )
    return logistics and mechanism and not asks_live_position


def _requests_search_then_cart_add(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    asks_add = any(
        marker in compact for marker in ("加入购物车", "加到购物车", "放进购物车", "放到购物车")
    )
    asks_search = any(marker in compact for marker in ("搜索", "查找", "帮我找", "找一", "推荐"))
    references_existing_result = any(
        marker in compact
        for marker in ("刚才搜索结果", "刚才的搜索结果", "搜索结果的", "刚才结果", "上面结果")
    )
    return asks_add and asks_search and not references_existing_result


def _requests_checkout_preview(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    has_checkout = _contains(compact, "结算", "结算预览", "去结算")
    has_cart = _contains(compact, "购物车", "购物袋", "购务车")
    payment_is_negated = _contains(
        compact,
        "不要付款",
        "不付款",
        "先不付款",
        "不要支付",
        "先不支付",
        "不支付",
    )
    requests_payment = _contains(compact, "付款", "支付") and not payment_is_negated
    return has_checkout and has_cart and not requests_payment


def _requests_favorite_update(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    if _contains(compact, "不要执行", "先不执行", "别执行", "只问问", "如果取消"):
        return False
    asks_listing = "收藏" in compact and _contains(
        compact,
        "收藏了哪些",
        "收藏有哪些",
        "收藏有什么",
        "收藏多少",
        "收藏几件",
        "收藏几家",
        "收藏列表",
        "看看收藏",
    )
    explicit_action = _contains(
        compact,
        "取消收藏",
        "移出收藏",
        "删除收藏",
        "帮我收藏",
        "加入收藏",
        "添加收藏",
        "取消关注",
        "帮我关注",
        "关注这家",
        "重新收藏",
        "恢复收藏",
        "再次收藏",
        "再收藏",
    )
    imperative_prefix = re.match(r"^(?:请|麻烦)?(?:收藏|关注)", compact) is not None
    return explicit_action or (imperative_prefix and not asks_listing)


def _requests_review_draft(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    review_subject = _contains(compact, "评价", "好评", "差评", "晒单")
    writing_request = _contains(
        compact,
        "评价草稿",
        "帮我写",
        "写一段",
        "拟一段",
        "整理成评价",
        "润色",
        "不要提交",
        "先不提交",
        "直接提交",
        "帮我提交",
    )
    return review_subject and writing_request


def _asks_order_spend_summary_request(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    return "订单" in compact and any(
        marker in compact
        for marker in (
            "累计实付",
            "总共实付",
            "一共实付",
            "累计消费",
            "一共花",
            "总共花",
            "花了多少钱",
            "已退款多少",
            "退款总额",
            "净支出",
        )
    )


def _requests_refund_draft(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    asks_draft = _contains(
        compact,
        "退款草稿",
        "申请草稿",
        "准备仅退款",
        "准备退款",
        "生成退款",
        "创建退款",
        "准备草稿",
        "生成草稿",
    )
    refund_context = _contains(
        compact,
        "退款",
        "售后",
        "能退",
        "可以退",
        "如果能退",
        "若能退",
    )
    return asks_draft and refund_context


def _asks_multi_order_logistics_comparison(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    return (
        any(marker in compact for marker in ("物流", "快递", "包裹", "运输中"))
        and any(marker in compact for marker in ("所有", "全部", "每笔", "分别"))
        and any(
            marker in compact
            for marker in ("比较", "对比", "最早", "最晚", "预计送达", "什么时候到")
        )
    )


def _asks_order_ordinal_logistics(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    has_ordinal = any(
        marker in compact
        for marker in (
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
        )
    )
    return has_ordinal and any(
        marker in compact
        for marker in ("物流", "快递", "包裹", "位置", "到哪", "送达", "什么时候到")
    )


def _asks_single_order_logistics_question(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    return (
        "订单" in compact
        and any(marker in compact for marker in ("物流", "快递", "包裹", "到哪"))
        and not any(marker in compact for marker in ("所有", "全部", "每笔", "分别", "几笔"))
        and not any(
            marker in compact
            for marker in ("偏好", "余额", "购物车", "收货地址", "收藏", "建议", "结合")
        )
        and not _asks_order_logistics_status_difference(user_text)
    )


def _asks_general_funds_policy(user_text: str) -> bool:
    compact = re.sub(r"\s+", "", user_text).casefold()
    policy_subject = "提现" in compact or (
        "退款" in compact
        and any(
            marker in compact
            for marker in ("原支付渠道", "原路", "退到余额", "退款去向", "退到哪里")
        )
    )
    personal_record = any(
        marker in compact
        for marker in (
            "我的订单",
            "我的退款",
            "这笔订单",
            "这笔退款",
            "这个订单",
            "刚才的订单",
            "订单号",
            "退款进度",
        )
    )
    return policy_subject and not personal_record


def _compound_intent_coverage(
    user_text: str,
) -> tuple[tuple[ExclusiveIntent, str], ...]:
    """Return explicit domain asks that a compound plan is not allowed to lose."""

    user_text = _strip_negated_intent_phrases(user_text)
    text = re.sub(r"\s+", "", user_text).casefold()
    result: list[tuple[ExclusiveIntent, str]] = []

    if "订单" in text and _contains(
        text,
        "购物车",
        "购物袋",
        "购务车",
        "收货地址",
        "默认地址",
        "余额",
        "钱包",
        "收藏",
        "偏好",
    ):
        result.append(("order_lookup", user_text[:200]))

    cart_hypothetical = _contains(
        text, "购物车", "购物袋", "购务车", "刚加的", "刚加入", "刚放进"
    ) and _contains(text, "如果", "假设", "假如", "试算", "只计算", "先别修改", "不要修改")
    if cart_hypothetical:
        result.append(("cart_lookup", user_text[:200]))
    elif (
        _contains(text, "加入购物车", "加到购物车", "放进购物车", "放到购物车")
        and not _requests_cart_add_verification(text)
    ):
        result.append(("cart_add", user_text[:200]))
    elif _contains(
        text, "购物车", "购物袋", "购务车", "刚加的", "刚加入", "刚放进"
    ) and _contains(
        text, "数量改成", "数量改为", "改成", "改为", "改回", "调成", "调整为"
    ):
        result.append(("cart_update", user_text[:200]))
    elif (
        _contains(text, "购物车", "购物袋", "购务车")
        and not _contains(text, "内容全部移除", "所有商品移除", "全部商品移除")
        and _contains(
        text,
        "移出购物车",
        "从购物车移出",
        "从购物车移除",
        "从购物车拿掉",
        "从购物车删除",
        "从购物车删",
        "删掉",
        "删除这件",
        "移除",
        "拿掉",
        )
    ):
        result.append(("cart_remove", user_text[:200]))
    elif _contains(text, "购物车", "购物袋", "购务车"):
        intent: ExclusiveIntent = (
            "cart_clear"
            if _contains(
                text,
                "清空",
                "都删",
                "全部删除",
                "全删",
                "删光",
                "内容全部移除",
                "所有商品移除",
                "全部商品移除",
            )
            else "cart_lookup"
        )
        result.append((intent, user_text[:200]))
    if (
        _contains(text, "收货地址", "地址簿", "默认地址", "我的地址")
        or ("地址" in text and "邮编" in text)
        or _requests_profile_lookup(text)
    ):
        result.append(("address_lookup", user_text[:200]))
    asks_balance_value = _contains(
        text,
        "余额多少",
        "余额还有",
        "当前余额",
        "可用余额",
        "钱还剩多少",
        "还剩多少钱",
        "剩多少钱",
        "还有多少钱",
        "我还有多少",
        "我还剩多少",
        "剩余多少钱",
        "可用多少钱",
    )
    if _requests_wallet_lookup(text) and (
        not _asks_payment_method_question(text) or asks_balance_value
    ):
        result.append(("wallet_lookup", "查询当前用户的账户余额"))
    if _contains(text, "收藏", "关注的店铺", "喜欢的商品"):
        result.append(("favorites_lookup", "查询当前用户收藏的商品和店铺"))
    if _requests_memory_lookup(text) and not _contains(
        text, "推荐", "适合我", "按我的偏好选", "找商品"
    ):
        result.append(("memory_lookup", "查询当前用户已确认的购物偏好"))

    # General policy questions can be one part of a larger request.  Keep them
    # as an independent RAG-backed task instead of allowing the planner to
    # answer the policy clause while silently dropping orders or account data.
    refund_policy = (
        "退款" in text
        and _contains(
            text,
            "多久到账",
            "多久到",
            "多久能退",
            "多长时间",
            "时效",
            "几天",
            "通常",
            "一般",
        )
        and not _contains(text, "我的退款", "这笔退款", "当前退款", "退款进度")
    )
    receipt_policy = "自动确认收货" in text and _contains(
        text, "多久", "几天", "什么时候", "规则", "政策", "时效"
    )
    logistics_policy_question = _contains(text, "物流", "快递", "包裹") and _contains(
        text,
        "每5秒",
        "每五秒",
        "每隔5秒",
        "每隔五秒",
        "自动更新",
        "自动推进",
        "更新机制",
        "更新规则",
        "多久更新一次",
    )
    if refund_policy or receipt_policy or logistics_policy_question:
        if refund_policy:
            objective = "退款一般多久到账以及固定到账时效规则"
        elif receipt_policy:
            objective = "物流签收后几天会自动确认收货"
        else:
            objective = "模拟物流是否按固定秒数自动更新"
        result.append(("policy_qa", objective))

    logistics_query = _contains(
        text, "运输中的包裹", "物流到哪", "包裹到哪", "快递到哪", "物流现状"
    ) or (
        _contains(text, "物流", "快递", "包裹") and _contains(text, "到哪", "进度", "轨迹", "送达")
    )
    logistics_policy = _contains(
        text,
        "每5秒",
        "每五秒",
        "每隔5秒",
        "每隔五秒",
        "自动更新",
        "自动推进",
        "更新机制",
        "更新规则",
    )
    if logistics_query and not logistics_policy:
        objective = "查询当前用户运输中订单的物流进度" if "运输中" in text else user_text[:200]
        result.append(("logistics_lookup", objective))

    refund_precheck = _contains(text, "售后", "退款", "退货") and _contains(
        text,
        "能不能",
        "能否",
        "是否",
        "资格",
        "只检查",
        "不要提交",
        "别提交",
    )
    if refund_precheck:
        result.append(("refund_precheck", user_text[:200]))

    if _contains(
        text,
        "退款进度",
        "售后进度",
        "售后中的",
        "售后到哪一步",
        "售后处理到哪",
        "退款到哪",
        "进行中的退款",
        "正在退款",
        "退款中的",
        "为什么还没退款",
        "为什么还没有退款",
        "为什么没退款",
        "为何还没退款",
        "还没退款",
        "还没有退款",
        "退款还没到账",
        "退款还没有到账",
    ) or ((_contains(text, "售后", "退款")) and "进度" in text):
        result.append(("refund_progress", "查询当前用户售后中的申请进度"))

    asks_order_cards = "订单" in text and _contains(
        text,
        "展示",
        "卡片",
        "列出",
        "有哪些",
        "所有订单",
        "全部订单",
        "按状态",
        "分别",
        "待付款",
        "待支付",
        "待发货",
        "运输中",
        "待评价",
        "售后中",
        "已完成",
    )
    if asks_order_cards:
        result.append(("order_lookup", user_text[:200]))

    protected_transaction_action = _contains(
        text,
        "帮我付款",
        "替我付款",
        "代我付款",
        "帮我支付",
        "替我支付",
        "代我支付",
        "帮我确认收货",
        "替我确认收货",
        "帮我取消订单",
        "替我取消订单",
    )
    cart_payment_request = _contains(text, "购物车", "购物袋", "购务车") and _contains(
        text, "帮我付款", "替我付款", "代我付款", "帮我支付", "替我支付", "代我支付"
    )
    if (
        protected_transaction_action
        and result
        and not cart_payment_request
        and not any(intent == "order_lookup" for intent, _objective in result)
    ):
        # The write remains blocked, but a companion read (for example balance)
        # should still complete and the relevant order card gives the shopper a
        # safe manual action entry point.
        result.append(("order_lookup", "查询当前用户最近一笔相关订单"))

    priority = {
        "order_lookup": 0,
        "cart_lookup": 1,
        "cart_add": 1,
        "cart_update": 1,
        "cart_remove": 1,
        "cart_clear": 1,
        "address_lookup": 2,
        "wallet_lookup": 2,
        "favorites_lookup": 2,
        "memory_lookup": 2,
        "policy_qa": 3,
        "logistics_lookup": 4,
        "refund_progress": 5,
        "refund_precheck": 5,
    }
    return tuple(sorted(dict(result).items(), key=lambda item: priority[item[0]]))


def _requests_cart_add_verification(text: str) -> bool:
    """Recognize questions about a past add so they can never repeat the write."""

    normalized = re.sub(r"\s+", "", text).casefold()
    return any(
        marker in normalized
        for marker in ("加入购物车", "加到购物车", "放进购物车", "放到购物车")
    ) and any(
        marker in normalized
        for marker in ("核对", "确认一下", "是不是", "是否", "有没有", "对不对", "正确吗")
    )


def _requests_wallet_lookup(text: str) -> bool:
    """Recognize natural balance questions without requiring the word account.

    Buyers commonly ask ``钱还剩多少`` after another task.  Requiring
    ``账户``/``账号`` silently dropped that half of a compound request.
    Restrict the shorthand to remaining/available-money phrases so ordinary
    product price questions are not routed to the wallet specialist.
    """

    return _contains(text, "余额", "钱包", "账户余额", "可用余额") or _contains(
        text,
        "钱还剩多少",
        "还剩多少钱",
        "剩多少钱",
        "还有多少钱",
        "我还有多少",
        "我还剩多少",
        "剩余多少钱",
        "可用多少钱",
    )


def _describes_wallet_transaction_history(text: str) -> bool:
    """Do not mistake a classification of existing ledger rows for a recharge request."""

    normalized = re.sub(r"\s+", "", text).casefold()
    return "充值" in normalized and any(
        marker in normalized
        for marker in (
            "哪笔",
            "每笔",
            "流水",
            "变动",
            "记录",
            "历史",
            "明细",
            "消费",
            "支出",
            "收入",
        )
    )


def _requests_order_state_overview_request(text: str) -> bool:
    """Keep a multi-state order overview from being captured by one state keyword."""

    normalized = re.sub(r"\s+", "", text).casefold()
    if "订单" not in normalized:
        return False
    groups = (
        ("待付款", "待支付", "未付款"),
        ("待发货", "备货"),
        ("运输中", "已发货", "物流中"),
        ("已完成", "已收货"),
        ("待评价", "未评价"),
        ("售后中", "退款中", "售后订单"),
    )
    requested_group_count = sum(
        any(marker in normalized for marker in markers) for markers in groups
    )
    return requested_group_count >= 2 or (
        requested_group_count >= 1
        and any(marker in normalized for marker in ("按状态", "每种状态", "所有状态"))
    )


def _requests_profile_lookup(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text).casefold()
    explicit_profile = any(
        marker in normalized
        for marker in ("我的用户名", "我的邮箱", "账号资料", "账户资料", "个人资料")
    )
    account_subject = any(
        marker in normalized
        for marker in ("当前账号", "当前账户", "我的账号", "我的账户")
    )
    profile_field = any(
        marker in normalized
        for marker in ("用户名", "邮箱", "邮箱提示", "当前邮箱")
    )
    paired_identity_fields = "用户名" in normalized and "邮箱" in normalized
    return explicit_profile or (account_subject and profile_field) or paired_identity_fields


def _asks_payment_method_question(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text).casefold()
    return any(marker in normalized for marker in ("支付", "付款")) and any(
        marker in normalized
        for marker in ("支付方式", "付款方式", "支付渠道", "怎么付", "如何付", "用余额", "实际扣")
    )


def _mentions_multiple_product_ordinals(text: str) -> bool:
    """Treat two referenced product cards as a comparison even in natural wording."""

    references = re.findall(r"第(?:[一二三四五]|[1-5])个", text)
    return len(references) >= 2


def _requests_memory_lookup(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text).casefold()
    return _contains(
        normalized,
        "你记得我",
        "记得我的",
        "记住我的",
        "记住的偏好",
        "记住的购物偏好",
        "记住了什么",
        "保存的偏好",
        "保存的购物偏好",
        "我的长期记忆",
        "我的购物偏好",
        "我喜欢什么",
        "我的偏好",
    ) or ("偏好" in normalized and _contains(normalized, "记得", "记忆", "保存"))


def _strip_negated_intent_phrases(text: str) -> str:
    """Do not turn an explicitly rejected action into the selected intent.

    The remaining sentence is still routed normally, so ``不是要退款，只想问
    自动确认收货`` becomes an order/policy question while ``不是申请退款，想问
    退款多久到账`` remains a refund-policy question because the later occurrence
    is not inside the negated phrase.
    """

    value = text
    for pattern in (
        r"(?:我)?(?:不是|并非)(?:想|要|准备)?(?:申请|办理|发起)?退款",
        r"(?:我)?不(?:想|要|需要)(?:申请|办理|发起)?退款",
        r"(?:先)?不要(?:申请|办理|发起)?退款",
        r"(?:我)?(?:不是|并非)(?:想|要)?退货",
        r"(?:我)?不(?:想|要|需要)退货",
        r"(?:其他顾客|别的顾客|其他用户|别的用户|其他买家|别的买家|"
        r"另一个用户|某个用户|别人的|他人的)(?:订单|购买记录|物流|地址|余额|"
        r"购物车|收藏|账号)?(?:我知道)?(?:不能|不可以|不要|不用)(?:查看|查询|查|看)",
        r"(?:不能|不可以|不要|不用)(?:查看|查询|查|看)(?:其他顾客|别的顾客|"
        r"其他用户|别的用户|其他买家|别的买家|别人|他人)(?:的)?"
        r"(?:订单|购买记录|物流|地址|余额|购物车|收藏|账号)?",
    ):
        value = re.sub(pattern, "", value)
    return value


EXCLUSIVE_CAPABILITIES: dict[ExclusiveIntent, tuple[str, ...]] = {
    "general_chat": (),
    "policy_qa": ("rag.policy.search",),
    "product_search": ("catalog.search_products",),
    "product_compare": ("catalog.compare_products",),
    "personalized_recommendation": (
        "catalog.search_products",
        "memory.list_mine",
    ),
    "order_lookup": ("order.list_user_orders", "order.get_user_order_detail"),
    "cart_lookup": ("cart.get_mine",),
    "cart_add": ("catalog.compare_products", "cart.add_item"),
    "cart_update": ("cart.get_mine", "cart.update_quantity"),
    "cart_remove": ("cart.get_mine", "cart.remove_item"),
    "cart_clear": ("cart.get_mine", "cart.clear.commit"),
    "checkout_preview": ("checkout.create_session",),
    "address_lookup": ("address.list_mine", "account.profile.get_mine"),
    "wallet_lookup": ("account.wallet.get_mine",),
    "favorites_lookup": ("account.favorites.list_mine",),
    "favorite_update": (
        "account.favorites.list_mine",
        "favorite.add_product",
        "favorite.remove_product",
        "favorite.add_store",
        "favorite.remove_store",
    ),
    "review_draft": ("order.list_user_orders",),
    "memory_lookup": ("memory.list_mine",),
    "logistics_lookup": ("logistics.get_user_order_shipments",),
    "refund_precheck": ("after_sale.check_refund_eligibility",),
    "refund_eligibility": ("after_sale.build_refund_draft",),
    "refund_progress": (
        "after_sale.list_user_refunds",
        "after_sale.get_user_refund_detail",
    ),
    "human_handoff": ("support.create_platform_ticket",),
}


def complete_exclusive_plan(plan: ExclusiveAgentPlan) -> ExclusiveAgentPlan:
    capabilities = plan.required_capabilities or EXCLUSIVE_CAPABILITIES[plan.intent]
    is_handoff = plan.intent == "human_handoff"
    return ExclusiveAgentPlan(
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


def _contains(value: str, *terms: str) -> bool:
    return any(term in value for term in terms)


def _search_text(value: str) -> str | None:
    value = re.sub(
        r"(?:[，,\uff1b;]\s*)?(?:并且?|同时)(?:请)?(?:说明|告诉我|展示|列出).*$",
        " ",
        value,
    )
    cleaned = re.sub(
        r"(?:麻烦|请|帮我|给我|我想|想要|看看|一下|全平台|商品|搜索|查找|找找|找|推荐|对比|比较|介绍|讲讲|了解|价格)",
        " ",
        value,
    )
    cleaned = re.sub(r"[\u3001\u3002\uff0c\uff01\uff1f\uff1a\uff1b,:;!?]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:120] or None
