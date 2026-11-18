"""In-process authority for an authenticated dashboard turn."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass
class _Authority:
    user_id: int
    sources: frozenset[str]
    active: bool = True


_current: ContextVar[_Authority | None] = ContextVar("private_knowledge_authority", default=None)


def dashboard_authority(user_id: int, selected: list[str] | None) -> object:
    """Called only after dashboard transport authentication and turn validation."""
    sources = frozenset(x for x in selected or [] if x.startswith("private-feishu:"))
    return _Authority(user_id, sources)


def install(value: object) -> Token[_Authority | None]:
    return _current.set(value if type(value) is _Authority else None)


def reset(token: Token[_Authority | None]) -> None:
    authority = _current.get()
    if authority is not None:
        authority.active = False
    _current.reset(token)


def allows(user_id: int, source: str) -> bool:
    authority = _current.get()
    return (
        authority is not None
        and authority.active
        and authority.user_id == user_id
        and source in authority.sources
    )


def matches_actor(user_id: object) -> bool:
    """When a dashboard capability is installed, bind dispatch to its authenticated actor."""
    authority = _current.get()
    if authority is None:
        return True
    return authority.active and str(user_id) == str(authority.user_id)
