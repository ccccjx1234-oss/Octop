"""Keep protected conversation content out of the agent-wide memory tools."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

_protected_memory: ContextVar[bool] = ContextVar("protected_memory", default=False)
_root_thread: ContextVar[str | None] = ContextVar("private_root_thread", default=None)


@contextmanager
def private_memory_scope(thread_repo: Any, thread_id: str | None) -> Iterator[None]:
    """Bind the server-owned root thread state, retaining any protected parent."""
    row = thread_repo.get(thread_id) if thread_id else None
    root_token = _root_thread.set(
        _root_thread.get() if _root_thread.get() is not None else thread_id or ""
    )
    token = _protected_memory.set(
        _protected_memory.get() or row is None or bool(row.private_knowledge_protected)
    )
    try:
        yield
    finally:
        _protected_memory.reset(token)
        _root_thread.reset(root_token)


def invocation_root_thread() -> str | None:
    """Return the server-bound root, including an unknown root as an empty string."""
    return _root_thread.get()


def invocation_is_protected() -> bool:
    """Include protection inherited from every enclosing invocation."""
    return _protected_memory.get()


@contextmanager
def _inbox_memory_scope(thread_repo: Any, thread_id: str | None) -> Iterator[None]:
    """Start a new root only at the server-owned inbox consumption boundary."""
    root_token = _root_thread.set(None)
    protected_token = _protected_memory.set(False)
    try:
        with private_memory_scope(thread_repo, thread_id):
            yield
    finally:
        _protected_memory.reset(protected_token)
        _root_thread.reset(root_token)


def guard_memory_service(service: Any) -> None:
    """Guard the service already captured by harness tools and middleware."""
    if service is None or getattr(service, "_octop_private_guard", False):
        return
    for name in ("save", "capture_turn", "extract"):
        original = getattr(service, name)

        def guarded(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            if _protected_memory.get():
                raise PermissionError("Memory writes are unavailable for this conversation.")
            return _original(*args, **kwargs)

        setattr(service, name, wraps(original)(guarded))
    service._octop_private_guard = True


class PrivateMemoryGuardMiddleware(AgentMiddleware[Any, Any]):
    def __init__(self, thread_repo: Any) -> None:
        super().__init__()
        self._thread_repo = thread_repo

    def _blocked(self, request: ToolCallRequest) -> ToolMessage | None:
        call = request.tool_call
        if call.get("name") != "memory_save":
            return None
        config = request.runtime.config.get("configurable", {})
        thread_id = config.get("thread_id")
        row = self._thread_repo.get(thread_id) if isinstance(thread_id, str) else None
        if _protected_memory.get() or row is None or row.private_knowledge_protected:
            return ToolMessage(
                content="Memory writes are unavailable for this conversation.",
                tool_call_id=str(call.get("id") or ""),
                status="error",
            )
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        blocked = self._blocked(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        blocked = await asyncio.to_thread(self._blocked, request)
        return blocked if blocked is not None else await handler(request)
