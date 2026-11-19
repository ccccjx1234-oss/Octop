"""In-process registry of Dashboard WebSocket connections."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from octop.infra.gateway.threads import thread_visible_to

if TYPE_CHECKING:
    from octop.infra.db.repos.threads import ThreadRow

logger = logging.getLogger(__name__)

SendFn = Callable[[dict[str, Any]], Awaitable[None]]


def stamp_thread_id(frame: dict[str, Any], thread_id: str) -> dict[str, Any]:
    """Return *frame* with ``thread_id`` set so clients can drop cross-talk."""
    tid = thread_id.strip()
    if not tid or frame.get("thread_id") == tid:
        return frame
    stamped = dict(frame)
    stamped["thread_id"] = tid
    return stamped


class WebSocketHub:
    """Maps connection ids to async send callbacks for Dashboard sockets.

    Tracks per-thread subscriptions (chat) and optional per-user bindings
    (dashboard-wide toasts). Reconnecting clients resume live chunks via
    in-memory "turn active" flags. Each connection is bound to at most one
    thread at a time.
    """

    def __init__(self, *, get_thread: Callable[[str], ThreadRow | None] | None = None) -> None:
        self._get_thread = get_thread
        self._connections: dict[str, SendFn] = {}
        self._thread_subscribers: dict[str, set[str]] = {}
        self._conn_thread: dict[str, str] = {}
        self._user_conns: dict[int, set[str]] = {}
        self._conn_user: dict[str, int] = {}
        self._conn_agent: dict[str, str] = {}
        self._sid_conns: dict[str, set[str]] = {}
        self._conn_sid: dict[str, str] = {}
        self._active_turns: set[str] = set()

    def register(
        self,
        connection_id: str,
        send_fn: SendFn,
        *,
        user_id: int | None = None,
        sid: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self._unbind_user(connection_id)
        self._unbind_sid(connection_id)
        self._connections[connection_id] = send_fn
        self._conn_agent.pop(connection_id, None)
        if agent_id is not None:
            self._conn_agent[connection_id] = agent_id
        if user_id is not None:
            self._conn_user[connection_id] = user_id
            self._user_conns.setdefault(user_id, set()).add(connection_id)
        clean_sid = sid.strip() if isinstance(sid, str) else ""
        if clean_sid:
            self._conn_sid[connection_id] = clean_sid
            self._sid_conns.setdefault(clean_sid, set()).add(connection_id)

    def unregister(self, connection_id: str) -> None:
        self.unsubscribe_connection(connection_id)
        self._unbind_user(connection_id)
        self._unbind_sid(connection_id)
        self._connections.pop(connection_id, None)
        self._conn_agent.pop(connection_id, None)

    def _unbind_user(self, connection_id: str) -> None:
        user_id = self._conn_user.pop(connection_id, None)
        if user_id is None:
            return
        conns = self._user_conns.get(user_id)
        if conns is None:
            return
        conns.discard(connection_id)
        if not conns:
            self._user_conns.pop(user_id, None)

    def _unbind_sid(self, connection_id: str) -> None:
        sid = self._conn_sid.pop(connection_id, None)
        if sid is None:
            return
        conns = self._sid_conns.get(sid)
        if conns is None:
            return
        conns.discard(connection_id)
        if not conns:
            self._sid_conns.pop(sid, None)

    def subscribe(self, thread_id: str, connection_id: str) -> None:
        """Add *connection_id* as a subscriber for *thread_id*.

        Switching threads unsubscribes this connection from the previous
        thread. Other connections on the same thread keep receiving.
        """
        tid = thread_id.strip()
        if not tid or connection_id not in self._connections:
            return
        prev = self._conn_thread.get(connection_id)
        if prev == tid:
            return
        if prev is not None:
            self._drop_subscriber(prev, connection_id)
        self._thread_subscribers.setdefault(tid, set()).add(connection_id)
        self._conn_thread[connection_id] = tid

    def unsubscribe_connection(self, connection_id: str) -> None:
        tid = self._conn_thread.pop(connection_id, None)
        if tid is not None:
            self._drop_subscriber(tid, connection_id)

    def _drop_subscriber(self, thread_id: str, connection_id: str) -> None:
        conns = self._thread_subscribers.get(thread_id)
        if conns is None:
            return
        conns.discard(connection_id)
        if not conns:
            self._thread_subscribers.pop(thread_id, None)

    def mark_turn_active(self, thread_id: str) -> None:
        tid = thread_id.strip()
        if tid:
            self._active_turns.add(tid)

    def mark_turn_idle(self, thread_id: str) -> None:
        self._active_turns.discard(thread_id.strip())

    def is_turn_active(self, thread_id: str) -> bool:
        return thread_id.strip() in self._active_turns

    def has_subscribers(self, thread_id: str) -> bool:
        """True when at least one Dashboard socket is bound to *thread_id*."""
        tid = thread_id.strip()
        return bool(tid and self._thread_subscribers.get(tid))

    async def push(self, connection_id: str, frame: dict[str, Any]) -> None:
        tid = frame.get("thread_id")
        if isinstance(tid, str) and tid and not await self.thread_allowed(tid, connection_id):
            return
        await self._send(connection_id, frame)

    async def _send(self, connection_id: str, frame: dict[str, Any]) -> None:
        send_fn = self._connections.get(connection_id)
        if send_fn is None:
            logger.debug("ws hub: connection %s not found", connection_id)
            return
        try:
            await send_fn(frame)
        except Exception:
            logger.exception("ws hub: push failed for %s", connection_id)

    async def thread_allowed(self, thread_id: str, connection_id: str) -> bool:
        if self._get_thread is None:
            return True
        row = await asyncio.to_thread(self._get_thread, thread_id)
        return self._thread_visible(row, connection_id)

    def _thread_visible(self, row: ThreadRow | None, connection_id: str) -> bool:
        if row is None:
            return False
        user_id = self._conn_user.get(connection_id)
        agent_id = self._conn_agent.get(connection_id)
        if agent_id is not None:
            return (
                user_id is not None
                and row.agent_id == agent_id
                and thread_visible_to(row, user_id)
            )
        return not row.private_knowledge_protected or row.user_id == user_id

    async def push_to_thread(self, thread_id: str, frame: dict[str, Any]) -> None:
        tid = thread_id.strip()
        conns = self._thread_subscribers.get(tid)
        if not conns:
            logger.debug("ws hub: no subscriber for thread %s", thread_id)
            return
        outbound = stamp_thread_id(frame, tid)
        if self._get_thread is None:
            targets = list(conns)
        else:
            row = await asyncio.to_thread(self._get_thread, tid)
            targets = [
                conn_id
                for conn_id in self._thread_subscribers.get(tid, ())
                if self._thread_visible(row, conn_id)
            ]
        if len(targets) == 1:
            await self._send(targets[0], outbound)
            return
        await asyncio.gather(*(self._send(conn_id, outbound) for conn_id in targets))

    async def push_to_user(self, user_id: int, frame: dict[str, Any]) -> None:
        conns = self._user_conns.get(user_id)
        if not conns:
            logger.debug("ws hub: no subscriber for user %s", user_id)
            return
        targets = list(conns)
        if len(targets) == 1:
            await self.push(targets[0], frame)
            return
        await asyncio.gather(*(self.push(conn_id, frame) for conn_id in targets))

    async def push_to_sid(self, sid: str, frame: dict[str, Any]) -> None:
        clean_sid = sid.strip()
        conns = self._sid_conns.get(clean_sid) if clean_sid else None
        if not conns:
            return
        targets = list(conns)
        if len(targets) == 1:
            await self.push(targets[0], frame)
            return
        await asyncio.gather(*(self.push(conn_id, frame) for conn_id in targets))

    async def push_json(self, connection_id: str, payload: str) -> None:
        try:
            frame = json.loads(payload)
        except (TypeError, ValueError):
            logger.warning("ws hub: invalid json for %s", connection_id)
            return
        if isinstance(frame, dict):
            await self.push(connection_id, frame)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


__all__ = ["SendFn", "WebSocketHub", "stamp_thread_id"]
