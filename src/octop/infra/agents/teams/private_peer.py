"""Fail closed at the shared harness peer and inbox boundaries."""

from __future__ import annotations

from functools import wraps
from typing import Any

from octop.infra.agents.middleware.private_memory import (
    _inbox_memory_scope,
    invocation_is_protected,
    invocation_root_thread,
)
from octop.infra.knowledge.private_authority import install, matches_actor, reset


def wire_private_peer_guard(team: Any, thread_repo: Any, jobs: Any) -> None:
    if getattr(team, "_octop_private_peer_guard", False) is True:
        return

    def allowed(thread_id: str | None) -> bool:
        repo = thread_repo() if callable(thread_repo) else thread_repo
        row = repo.get(thread_id) if thread_id else None
        return row is not None and not row.private_knowledge_protected

    def root() -> str:
        thread_id = invocation_root_thread()
        if invocation_is_protected() or not allowed(thread_id):
            raise PermissionError("Peer delegation is unavailable for this conversation.")
        return str(thread_id)

    original_call = team.call_peer
    original_submit = team.submit_peer
    original_invoke = team._invoke_peer
    original_build = team._build_peer_request

    @wraps(original_call)
    async def call_peer(**kwargs: Any) -> Any:
        kwargs["source_thread_id"] = root()
        if not matches_actor(kwargs.get("user_id")):
            raise PermissionError("Peer delegation is unavailable for this conversation.")
        return await original_call(**kwargs)

    @wraps(original_submit)
    def submit_peer(**kwargs: Any) -> Any:
        kwargs["source_thread_id"] = root()
        if not matches_actor(kwargs.get("user_id")):
            raise PermissionError("Peer delegation is unavailable for this conversation.")
        return original_submit(**kwargs)

    @wraps(original_invoke)
    async def invoke_peer(**kwargs: Any) -> Any:
        # Inbox jobs carry the server-selected root from enqueue time.
        if (
            invocation_is_protected()
            or not allowed(kwargs.get("source_thread_id"))
            or not matches_actor(kwargs.get("user_id"))
        ):
            raise PermissionError("Peer delegation is unavailable for this conversation.")
        current = invocation_root_thread()
        if kwargs.get("source") != "inbox" and (
            current is None or not allowed(current) or current != kwargs.get("source_thread_id")
        ):
            raise PermissionError("Peer delegation is unavailable for this conversation.")
        return await original_invoke(**kwargs)

    @wraps(original_build)
    async def build_peer_request(**kwargs: Any) -> Any:
        if (
            invocation_is_protected()
            or not allowed(kwargs.get("source_thread_id"))
            or not matches_actor(kwargs.get("user_id"))
        ):
            raise PermissionError("Peer delegation is unavailable for this conversation.")
        request = await original_build(**kwargs)
        if (
            invocation_is_protected()
            or not allowed(kwargs.get("source_thread_id"))
            or not matches_actor(kwargs.get("user_id"))
        ):
            raise PermissionError("Peer delegation is unavailable for this conversation.")
        return request

    team.call_peer = call_peer
    team.submit_peer = submit_peer
    team._invoke_peer = invoke_peer
    team._build_peer_request = build_peer_request

    inbox = team.inbox
    if inbox is not None:
        original_enqueue = inbox.enqueue
        original_process = inbox._process
        original_target = inbox._invoke_target
        original_reply = inbox._synthesize_reply
        original_finish = inbox._finish

        @wraps(original_enqueue)
        def enqueue(**kwargs: Any) -> str:
            source = root()
            if kwargs.get("source_thread_id") != source or not matches_actor(kwargs.get("user_id")):
                raise PermissionError("Peer delegation is unavailable for this conversation.")
            return str(original_enqueue(**kwargs))

        @wraps(original_process)
        async def process(msg: Any) -> None:
            repo = thread_repo() if callable(thread_repo) else thread_repo
            authority_token = install(None)
            try:
                with _inbox_memory_scope(repo, msg.source_thread_id):
                    await original_process(msg)
            finally:
                reset(authority_token)

        @wraps(original_target)
        async def invoke_target(msg: Any) -> Any:
            if not allowed(msg.source_thread_id):
                inbox.cancel(msg.id)
                return {}
            return await original_target(msg)

        @wraps(original_reply)
        async def synthesize(msg: Any, result_text: Any, error_text: Any) -> Any:
            if not allowed(msg.source_thread_id):
                inbox.cancel(msg.id)
                return None
            return await original_reply(msg, result_text, error_text)

        @wraps(original_finish)
        async def finish(msg: Any, **kwargs: Any) -> None:
            if not allowed(msg.source_thread_id):
                inbox._set_status(msg, "cancelled")
                inbox._prune_terminal(msg.id)
                if jobs is not None:
                    jobs.end(msg.source_agent_id, msg.target_agent_id, job_id=msg.id)
                return
            await original_finish(msg, **kwargs)

        inbox.enqueue = enqueue
        inbox._process = process
        inbox._invoke_target = invoke_target
        inbox._synthesize_reply = synthesize
        inbox._finish = finish

    team._octop_private_peer_guard = True
