from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import new_prefixed_ulid
from app.modules.agent_runtime.models import AgentToolAudit
from app.modules.knowledge.contracts import ToolCall, ToolResult, ToolScope, authorize_tool

ToolHandler = Callable[[BaseModel, ToolScope], Awaitable[Mapping[str, Any]]]
KillSwitchChecker = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True)
class ToolAdapter:
    code: str
    arguments_model: type[BaseModel]
    handler: ToolHandler
    timeout_seconds: float = 5.0


class McpHost:
    """Fail-closed MCP dispatcher; identity and scope are injected by the runtime."""

    def __init__(
        self,
        adapters: list[ToolAdapter],
        kill_switch_checker: KillSwitchChecker | None = None,
    ) -> None:
        self._adapters = {item.code: item for item in adapters}
        self._kill_switch_checker = kill_switch_checker

    async def execute(
        self,
        session: AsyncSession,
        *,
        run_id: int,
        tool_code: str,
        untrusted_arguments: Mapping[str, Any],
        trusted_scope: ToolScope,
        allowed_tools: frozenset[str],
    ) -> ToolResult:
        started = time.monotonic()
        call = ToolCall(
            protocol_version="2025-11-25",
            tool_code=tool_code,
            arguments=dict(untrusted_arguments),
            scope=trusted_scope,
        )
        try:
            if self._kill_switch_checker and await self._kill_switch_checker(tool_code):
                raise PermissionError("tool is disabled by kill switch")
            authorize_tool(call, allowed_tools)
            adapter = self._adapters[tool_code]
            arguments = adapter.arguments_model.model_validate(untrusted_arguments)
            raw = await asyncio.wait_for(
                adapter.handler(arguments, trusted_scope), timeout=adapter.timeout_seconds
            )
            result = ToolResult("succeeded", _redact(dict(raw)))
        except (PermissionError, KeyError):
            result = ToolResult("denied", {}, "TOOL_NOT_ALLOWED")
        except ValidationError:
            result = ToolResult("failed", {}, "TOOL_ARGUMENTS_INVALID")
        except TimeoutError:
            # A timeout cannot prove that a remote command did not run.
            result = ToolResult("unknown", {}, "TOOL_TIMEOUT_UNKNOWN")
        except Exception:
            result = ToolResult("failed", {}, "TOOL_EXECUTION_FAILED")
        session.add(
            AgentToolAudit(
                audit_no=new_prefixed_ulid("taud_"),
                run_id=run_id,
                tool_code=tool_code,
                scope_snapshot={
                    "user_no": trusted_scope.user_no,
                    "conversation_no": trusted_scope.conversation_no,
                    "store_no": trusted_scope.store_no,
                    "context_no": trusted_scope.context_no,
                    "context_version": trusted_scope.context_version,
                    "approval_no": trusted_scope.approval_no,
                },
                arguments_hash=hashlib.sha256(
                    json.dumps(
                        untrusted_arguments,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode()
                ).digest(),
                outcome=result.status,
                error_code=result.error_code,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        )
        await session.commit()
        return result


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


_SECRET_KEYS = re.compile(
    r"(?:password|secret|token|authorization|ciphertext|tracking_number|phone|email)", re.I
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if _SECRET_KEYS.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
