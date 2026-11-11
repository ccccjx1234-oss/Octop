"""Request-scoped Youli delegation identity for business tools."""

from __future__ import annotations

from contextvars import ContextVar

_YOULI_SID: ContextVar[str | None] = ContextVar("octop_youli_sid", default=None)
_THREAD_SIDS: dict[str, str] = {}


def set_current_youli_sid(sid: str | None) -> None:
    """Bind the delegated session id to the current async request context."""
    _YOULI_SID.set(sid)


def current_youli_sid() -> str | None:
    """Return the sid for the current request, or ``None`` for local accounts."""
    return _YOULI_SID.get()


def bind_thread_sid(thread_id: str, sid: str | None) -> None:
    """Remember the delegated sid for this dashboard thread."""
    if thread_id and sid:
        _THREAD_SIDS[thread_id] = sid


def sid_for_thread(thread_id: str | None) -> str | None:
    return _THREAD_SIDS.get(thread_id) if thread_id else None
