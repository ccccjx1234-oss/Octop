"""One gate for every agent tool call: may this user run this tool?

Rules are user-group grants on resource type ``tool`` keyed by tool name. A
tool with no grant behaves as before (everyone may call it), so restrictions
can be rolled out one tool at a time. Admins always pass. The check reads the
database on every call, so revoking access takes effect on the next call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from octop.i18n import tr
from octop.infra.agents.middleware.token_quota import configurable_user_id
from octop.infra.users.groups import RESOURCE_TOOL, GroupAccess
from octop.infra.utils.locale import resolve_user_locale


def tool_allowed(repos: Any, *, user_id: int | None, tool_name: str) -> bool:
    if user_id is None or not tool_name:
        return True
    row = repos.user_repo.get(user_id)
    if row is None:
        return False
    return GroupAccess(repos).allows(
        user_id=user_id,
        is_admin=str(getattr(row, "role", "")) == "admin",
        resource_type=RESOURCE_TOOL,
        resource_key=tool_name,
        is_owner=False,
    )


class ToolPermissionMiddleware(AgentMiddleware[Any, Any]):
    def __init__(self, *, repos: Any) -> None:
        super().__init__()
        self._repos = repos

    def _denied(self, request: ToolCallRequest) -> ToolMessage | None:
        call = request.tool_call
        name = str(call.get("name") or "")
        user_id = configurable_user_id()
        if tool_allowed(self._repos, user_id=user_id, tool_name=name):
            return None
        locale = resolve_user_locale(user_repo=self._repos.user_repo, user_id=user_id or 0)
        return ToolMessage(
            content=tr("quality.tool_denied", locale, tool_name=name),
            tool_call_id=str(call.get("id") or ""),
            name=name,
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        denied = self._denied(request)
        return denied if denied is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        denied = self._denied(request)
        return denied if denied is not None else await handler(request)


__all__ = ["ToolPermissionMiddleware", "tool_allowed"]
