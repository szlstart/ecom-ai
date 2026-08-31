import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.security import SecurityService
from app.modules.agent_runtime.exclusive_tools import ExclusiveToolGateway
from app.modules.agent_runtime.store_tools import StoreToolGateway
from app.modules.knowledge.skill_registry import RuntimeToolPolicy


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


def _context(policy: RuntimeToolPolicy, *, text: str = "查询商品") -> SimpleNamespace:
    return SimpleNamespace(
        allowed_tools=frozenset({policy.tool_code}),
        tool_policies={policy.tool_code: policy},
        trigger=SimpleNamespace(text_content=text),
        run=SimpleNamespace(id=1),
        trusted_scope={"user_no": "usr_test", "conversation_no": "conv_test"},
    )


@pytest.mark.asyncio
async def test_custom_gateway_enforces_skill_call_budget() -> None:
    policy = RuntimeToolPolicy("catalog.get_product", "none", 1, 1000)
    gateway = StoreToolGateway(_Session())  # type: ignore[arg-type]

    async def handler() -> dict[str, object]:
        return {"ok": True}

    first = await gateway.execute(_context(policy), policy.tool_code, {}, handler)  # type: ignore[arg-type]
    second = await gateway.execute(_context(policy), policy.tool_code, {}, handler)  # type: ignore[arg-type]
    assert first.status == "succeeded"
    assert second.status == "denied"
    assert second.error_code == "TOOL_CALL_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_custom_gateway_enforces_skill_timeout_and_confirmation() -> None:
    timeout_policy = RuntimeToolPolicy("catalog.get_product", "none", 1, 1)
    gateway = StoreToolGateway(_Session())  # type: ignore[arg-type]

    async def slow_handler() -> dict[str, object]:
        await asyncio.sleep(0.02)
        return {"ok": True}

    timed_out = await gateway.execute(
        _context(timeout_policy),  # type: ignore[arg-type]
        timeout_policy.tool_code,
        {},
        slow_handler,
    )
    assert timed_out.status == "unknown"
    assert timed_out.error_code == "TOOL_TIMEOUT_UNKNOWN"

    confirmation_policy = RuntimeToolPolicy(
        "support.create_store_ticket", "user_confirmation", 1, 1000
    )
    denied = await StoreToolGateway(_Session()).execute(  # type: ignore[arg-type]
        _context(confirmation_policy, text="商品什么时候发货"),  # type: ignore[arg-type]
        confirmation_policy.tool_code,
        {},
        slow_handler,
    )
    assert denied.status == "denied"
    assert denied.error_code == "TOOL_CONFIRMATION_REQUIRED"


@pytest.mark.asyncio
async def test_refund_submit_accepts_only_server_injected_matching_approval() -> None:
    settings = Settings(_env_file=None)
    policy = RuntimeToolPolicy(
        "after_sale.submit_refund_application", "user_confirmation", 1, 1000
    )
    gateway = ExclusiveToolGateway(
        _Session(),  # type: ignore[arg-type]
        settings,
        SecurityService(settings),
    )

    async def handler() -> dict[str, object]:
        return {"refund_id": "ref_test"}

    arguments: dict[str, object] = {"approval_id": "apr_server"}
    spoofed = await gateway.execute(
        _context(policy),  # type: ignore[arg-type]
        policy.tool_code,
        arguments,
        handler,
    )
    trusted = await gateway.execute(
        _context(policy),  # type: ignore[arg-type]
        policy.tool_code,
        arguments,
        handler,
        trusted_approval_no="apr_server",
    )

    assert spoofed.status == "denied"
    assert spoofed.error_code == "TOOL_CONFIRMATION_REQUIRED"
    assert trusted.status == "succeeded"
