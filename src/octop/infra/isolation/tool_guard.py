"""Fail closed for tools that have no user-scoped execution contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

_ALLOWED = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "execute",
        "write_todos",
        "current_time",
        "ask_user_question",
        "memory_search",
        "memory_get",
        "memory_save",
        "send_file_to_user",
    }
)


class IsolationToolGuard(AgentMiddleware[Any, Any]):
    def __init__(self, mcp_names: Callable[[], frozenset[str]] = frozenset):
        self.mcp_names = mcp_names

    def denied(self, request: ToolCallRequest) -> ToolMessage | None:
        if request.tool_call.get("name") in _ALLOWED | self.mcp_names():
            return None
        return ToolMessage(
            content="This tool has no user-isolated execution adapter.",
            tool_call_id=str(request.tool_call.get("id") or ""),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        denied = self.denied(request)
        return denied if denied is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        denied = self.denied(request)
        return denied if denied is not None else await handler(request)
