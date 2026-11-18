"""Employee-scoped MCN client construction and turn identity checks."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from octop.config import YouliConfig
from octop.infra.auth.youli.mcn_client import McnClient, McnClientError


def _turn_context() -> tuple[str, int | None]:
    try:
        from langgraph.config import get_config

        configurable = get_config().get("configurable") or {}
    except Exception:
        return "", None
    sid = str(configurable.get("youli_sid") or "").strip()
    raw_user = configurable.get("octop_user_id")
    if raw_user in (None, ""):
        return sid, None
    try:
        return sid, int(str(raw_user))
    except (TypeError, ValueError):
        return sid, None


def _owner_fallback_allowed(creds: dict[str, Any], sid: str, turn_user: int | None) -> bool:
    """Only the connector owner can use the current owner-session resolver."""
    owner = creds.get("_owner_user_id")
    if owner is None:
        return not sid
    return turn_user is not None and int(turn_user) == int(owner)


def build_mcn_client(
    creds: dict[str, Any],
    *,
    turn_context_fn: Callable[[], tuple[str, int | None]] = _turn_context,
) -> McnClient:
    """Build an employee-scoped ``McnClient`` without cross-employee fallback."""
    sid, turn_user = turn_context_fn()
    resolver = creds.get("_token_resolver")
    token = str(resolver() or "").strip() if callable(resolver) else ""
    if not token and _owner_fallback_allowed(creds, sid, turn_user):
        latest = creds.get("_latest_token_resolver")
        token = str(latest() or "").strip() if callable(latest) else ""
    base_url = (
        str(os.environ.get("OCTOP_YOULI_MCN_URL") or creds.get("base_url") or "")
        .strip()
        .rstrip("/")
    )
    ca_file = str(os.environ.get("OCTOP_YOULI_CA_FILE") or creds.get("ca_file") or "").strip()
    if not token:
        raise McnClientError("MCN 连接器未授权，请重新登录会火员工账号")
    client = McnClient(YouliConfig(mcn_base_url=base_url, ca_file=ca_file), token)
    if not client.enabled:
        raise McnClientError("MCN 业务接口未配置")
    return client
