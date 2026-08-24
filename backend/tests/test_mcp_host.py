import asyncio
from typing import Any, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.contracts import ToolScope
from app.modules.knowledge.mcp_host import McpHost, ToolAdapter


class ProductArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str


async def test_mcp_host_injects_scope_redacts_and_audits() -> None:
    captured: dict[str, Any] = {}

    async def handler(arguments: BaseModel, scope: ToolScope) -> dict[str, Any]:
        captured["scope"] = scope
        typed = cast(ProductArgs, arguments)
        return {
            "product_id": typed.product_id,
            "tracking_number": "FULL-NUMBER",
        }

    class Session:
        def add(self, value: Any) -> None:
            captured["audit"] = value

        async def commit(self) -> None:
            captured["committed"] = True

    trusted = ToolScope("usr_1", "conv_1", "sto_1", "ctx_1", 2)
    host = McpHost([ToolAdapter("catalog.product.get", ProductArgs, handler)])
    result = await host.execute(
        cast(AsyncSession, Session()),
        run_id=1,
        tool_code="catalog.product.get",
        untrusted_arguments={"product_id": "prd_1"},
        trusted_scope=trusted,
        allowed_tools=frozenset({"catalog.product.get"}),
    )
    assert result.status == "succeeded"
    assert result.safe_data["tracking_number"] == "***"
    assert captured["scope"] == trusted
    assert captured["audit"].outcome == "succeeded"


async def test_mcp_host_timeout_is_unknown_and_scope_cannot_be_smuggled() -> None:
    async def slow_handler(arguments: BaseModel, scope: ToolScope) -> dict[str, Any]:
        await asyncio.sleep(0.02)
        return {}

    class Session:
        def add(self, value: Any) -> None:
            pass

        async def commit(self) -> None:
            pass

    host = McpHost(
        [ToolAdapter("catalog.product.get", ProductArgs, slow_handler, timeout_seconds=0.001)]
    )
    result = await host.execute(
        cast(AsyncSession, Session()),
        run_id=1,
        tool_code="catalog.product.get",
        untrusted_arguments={"product_id": "prd_1", "user_no": "usr_attacker"},
        trusted_scope=ToolScope("usr_real", "conv_1", None, None, None),
        allowed_tools=frozenset({"catalog.product.get"}),
    )
    assert result.status == "failed"
    assert result.error_code == "TOOL_ARGUMENTS_INVALID"


async def test_mcp_host_denies_write_without_trusted_approval() -> None:
    called = False

    async def handler(arguments: BaseModel, scope: ToolScope) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    class Session:
        def add(self, value: Any) -> None:
            pass

        async def commit(self) -> None:
            pass

    host = McpHost([ToolAdapter("support.create_platform_ticket", ProductArgs, handler)])
    result = await host.execute(
        cast(AsyncSession, Session()),
        run_id=1,
        tool_code="support.create_platform_ticket",
        untrusted_arguments={"product_id": "prd_1"},
        trusted_scope=ToolScope("usr_1", "conv_1", None, None, None),
        allowed_tools=frozenset({"support.create_platform_ticket"}),
    )
    assert result.status == "denied"
    assert called is False
