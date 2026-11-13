"""Ownership of per-user expert content, separate from the expert definition."""

from __future__ import annotations

from typing import Any

from octop.api.common.agent import require_agent_owner_row, require_agent_row
from octop.infra.isolation.context import enabled


def require_content_owner_row(agent_id: str, *, user: Any, as_user: int | None, server: Any) -> Any:
    if not enabled():
        return require_agent_owner_row(agent_id, user=user, as_user=as_user, server=server)
    require_agent_row(agent_id, user=user, as_user=as_user, server=server)
    return server.app_runtime.agent_registry._user_runtimes.row(agent_id, user.id)
