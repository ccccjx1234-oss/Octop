"""Liveness probe (no auth)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from octop.api.deps import get_server, require_admin

router = APIRouter()


@router.get("", summary="Health check")
async def health(server: Any = Depends(get_server)) -> dict[str, Any]:
    """Liveness probe: database reachability, user count, and loaded agents. No auth required."""
    bound = server.database_bound
    users = server.user_manager.count() if server.user_manager is not None else 0
    agents = (
        len(server.app_runtime.agent_registry.list_rows()) if server.app_runtime is not None else 0
    )
    channels: list[dict[str, Any]] = []
    runtime = getattr(server, "app_runtime", None)
    gateway = getattr(runtime, "gateway", None) if runtime is not None else None
    report = getattr(gateway, "channel_health", None)
    if callable(report):
        channels = report()
    result: dict[str, Any] = {
        "ok": True,
        "started_at": server._started_at,
        "db": bound,
        "users_loaded": users,
        "agents_running": agents,
        "channels": channels,
    }
    policy = getattr(server, "isolation_policy", None)
    if policy is not None:
        result["isolation"] = {"state": policy.state, "ready": policy.state == "ready"}
    return result


class IsolationDiagnostics(BaseModel):
    enabled: bool
    mode: str | None = None
    state: str
    error: str | None = None
    repairs: int = 0
    login_available: bool


@router.get(
    "/isolation",
    summary="Administrator user isolation diagnostics",
    description="Requires admin authentication. Includes startup failures and repair count; "
    "does not retry startup or change the isolation mode.",
    response_model=IsolationDiagnostics,
    dependencies=[Depends(require_admin())],
)
async def isolation_diagnostics(server: Any = Depends(get_server)) -> IsolationDiagnostics:
    policy = getattr(server, "isolation_policy", None)
    return IsolationDiagnostics(
        enabled=policy is not None,
        mode=policy.mode if policy is not None else None,
        state=policy.state if policy is not None else "disabled",
        error=policy.error if policy is not None else None,
        repairs=policy.repairs.count if policy is not None else 0,
        login_available=server.user_manager is not None,
    )
