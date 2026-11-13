"""Authenticated actor scope; never share mutable identity between requests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from typing import Any

_actor: ContextVar[int | None] = ContextVar("octop_isolation_actor", default=None)


@cache
def enabled() -> bool:
    """Process-start mode; a live env reload must not split the security policy."""
    value = os.environ.get("OCTOP_USER_ISOLATION", "0").strip().lower()
    if value not in {"0", "1", "false", "true"}:
        raise RuntimeError("OCTOP_USER_ISOLATION must be 0 or 1")
    return value in {"1", "true"}


def actor_id() -> int | None:
    return _actor.get()


@contextmanager
def actor_scope(user_id: int) -> Iterator[None]:
    if isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("positive authenticated user id required")
    token = _actor.set(user_id)
    try:
        yield
    finally:
        _actor.reset(token)


@contextmanager
def invocation_scope(manager: Any, agent_id: str, request: dict[str, Any]) -> Iterator[None]:
    from octop.infra.errors import ErrorCode, OctopError

    source = manager._user_runtimes.source_row(agent_id)
    uid = actor_id()
    declared = request.get("user")
    if uid is None:
        try:
            uid = int(declared) if declared is not None else None
        except (TypeError, ValueError):
            uid = None
        if uid is None and not source.is_shared:
            uid = source.user_id
    if uid is None or uid <= 0 or (declared is not None and str(declared) != str(uid)):
        raise OctopError(ErrorCode.FORBIDDEN, "invocation requires its authenticated actor")
    thread_id = str(request.get("thread_id") or "")
    if thread_id:
        thread = manager._repos.thread_repo.get(thread_id)
        if thread is not None and (thread.user_id != uid or thread.agent_id != source.agent_id):
            raise OctopError(ErrorCode.FORBIDDEN, "thread does not belong to this actor and expert")
    with actor_scope(uid):
        yield


def check_message_identity(row: Any, channel_type: str, user_id: int) -> None:
    from octop.infra.errors import ErrorCode, OctopError
    from octop.infra.gateway.threads import ThreadRegistry

    authenticated_channels = {
        ThreadRegistry.CHANNEL_DASHBOARD,
        ThreadRegistry.CHANNEL_CLI,
        ThreadRegistry.CHANNEL_MCP,
    }
    if user_id <= 0 or (
        row is not None and row.is_shared and channel_type not in authenticated_channels
    ):
        raise OctopError(
            ErrorCode.FORBIDDEN, "shared expert requires a bound platform user identity"
        )
