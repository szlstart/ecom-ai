from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.contracts import CONFIRMATION_REQUIRED_TOOLS, READ_ONLY_TOOLS
from app.modules.knowledge.models import RuntimeKillSwitch


@dataclass(frozen=True)
class McpServerDefinition:
    server_code: str
    tools: frozenset[str]
    default_timeout_seconds: float = 5.0


MCP_SERVERS = {
    item.server_code: item
    for item in (
        McpServerDefinition(
            "catalog-mcp",
            frozenset(code for code in READ_ONLY_TOOLS if code.startswith("catalog.")),
        ),
        McpServerDefinition(
            "order-mcp", frozenset(code for code in READ_ONLY_TOOLS if code.startswith("order."))
        ),
        McpServerDefinition(
            "logistics-mcp",
            frozenset(code for code in READ_ONLY_TOOLS if code.startswith("logistics.")),
        ),
        McpServerDefinition(
            "after-sale-mcp",
            frozenset(
                code
                for code in READ_ONLY_TOOLS | CONFIRMATION_REQUIRED_TOOLS
                if code.startswith("after_sale.")
            ),
        ),
        McpServerDefinition(
            "support-mcp",
            frozenset(
                code
                for code in READ_ONLY_TOOLS | CONFIRMATION_REQUIRED_TOOLS
                if code.startswith("support.")
            ),
        ),
        McpServerDefinition(
            "memory-mcp",
            frozenset(
                code
                for code in READ_ONLY_TOOLS | CONFIRMATION_REQUIRED_TOOLS
                if code.startswith("memory.")
            ),
        ),
    )
}


def server_for_tool(tool_code: str) -> McpServerDefinition:
    matches = [server for server in MCP_SERVERS.values() if tool_code in server.tools]
    if len(matches) != 1:
        raise LookupError("tool must belong to exactly one registered MCP server")
    return matches[0]


def database_kill_switch_checker(session: AsyncSession) -> Callable[[str], Awaitable[bool]]:
    async def is_disabled(tool_code: str) -> bool:
        server = server_for_tool(tool_code)
        return bool(
            await session.scalar(
                select(RuntimeKillSwitch.id).where(
                    RuntimeKillSwitch.is_active.is_(True),
                    (
                        (RuntimeKillSwitch.target_type == "tool")
                        & (RuntimeKillSwitch.target_code == tool_code)
                    )
                    | (
                        (RuntimeKillSwitch.target_type == "mcp_server")
                        & (RuntimeKillSwitch.target_code == server.server_code)
                    ),
                )
            )
        )

    return is_disabled
