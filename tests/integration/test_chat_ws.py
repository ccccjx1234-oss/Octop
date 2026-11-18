"""tests/integration/test_chat_ws.py — dashboard WebSocket chat + thread CRUD."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from tests.support.app import octop_client
from tests.support.auth import (
    auth_header,
    bootstrap_admin,
    create_agent,
    ensure_users,
    seed_openai_provider,
)
from tests.support.fakes import FakeHarnessAgent
from tests.support.http import chat_ws_path, ws_connect


def _chat_ws(c: httpx.AsyncClient, aid: str, auth: dict[str, str]) -> Any:
    return ws_connect(c._octop_app, chat_ws_path(aid, auth))  # type: ignore[attr-defined]


@pytest.fixture
async def env(tmp_octop_home: Path) -> AsyncIterator[Any]:
    fake = FakeHarnessAgent(
        chunks=[
            {"type": "token", "node": "agent", "content": "Hello "},
            {"type": "token", "node": "agent", "content": "Bob."},
        ]
    )
    async with octop_client(tmp_octop_home, fake_agent=fake) as (c, srv):
        await bootstrap_admin(c, tmp_octop_home)
        admin_auth = await auth_header(c)
        await seed_openai_provider(c, admin_auth)
        users = await ensure_users(c, admin_auth, "alice", "bob")
        aid = await create_agent(c, users["alice"])
        yield c, srv, fake, users["alice"], users["bob"], aid


async def _turn_then_rebind(
    c: httpx.AsyncClient,
    aid: str,
    auth: dict[str, str],
    thread_id: str,
    gate_release: asyncio.Event,
) -> list[dict[str, Any]]:
    """Start a turn on conn A, drop it after the first token, re-subscribe on B.

    The fake stream is gated between first and second token so B can subscribe
    while the turn is still active, then receive the remaining chunks.
    """
    async with _chat_ws(c, aid, auth) as ws_a:
        await ws_a.send_json(
            {
                "type": "user_turn",
                "text": "slow please",
                "thread_id": thread_id,
            }
        )
        first = await ws_a.receive_json()
        while first.get("type") != "token":
            # Skip steer protocol frames (turn.started, …) — harness chunks only.
            first = await ws_a.receive_json()
        assert first.get("content") == "first"
    # A is closed; turn is blocked on gate_release (still active, not cancelled).

    frames: list[dict[str, Any]] = []
    async with _chat_ws(c, aid, auth) as ws_b:
        await ws_b.send_json({"type": "subscribe", "thread_id": thread_id})
        frames.append(await ws_b.receive_json())
        # Release the slow stream only after B is subscribed.
        gate_release.set()
        frames.extend(await ws_b.drain_turn())
    return frames


async def test_ws_rebind_after_disconnect_receives_later_chunks(env: Any) -> None:
    """After disconnect, a new subscriber must see subsequent tokens + done."""
    c, srv, _fake, alice_auth, _bob_auth, aid = env
    create = await c.post(f"/api/agents/{aid}/threads", headers=alice_auth)
    tid = create.json()["thread_id"]
    agent = srv.app_runtime.agent_registry.get_agent(aid)

    gate = asyncio.Event()

    async def slow_stream(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "token", "node": "agent", "content": "first"}
        await gate.wait()
        yield {"type": "token", "node": "agent", "content": "second"}

    agent.stream = slow_stream
    cancel_spy = MagicMock(wraps=srv.app_runtime.agent_registry.cancel_stream)
    srv.app_runtime.agent_registry.cancel_stream = cancel_spy

    frames = await _turn_then_rebind(c, aid, alice_auth, tid, gate)

    cancel_spy.assert_not_called()
    assert frames[0] == {"type": "turn_status", "thread_id": tid, "active": True}
    contents = [f.get("content") for f in frames if f.get("type") == "token"]
    assert "second" in contents
    assert frames[-1].get("type") == "done"


async def _turn_with_second_subscriber(
    c: httpx.AsyncClient,
    aid: str,
    auth: dict[str, str],
    thread_id: str,
    gate_release: asyncio.Event,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep conn A open, subscribe conn B mid-turn; both receive remaining chunks."""
    a_frames: list[dict[str, Any]] = []
    b_frames: list[dict[str, Any]] = []
    async with _chat_ws(c, aid, auth) as ws_a:
        await ws_a.send_json(
            {
                "type": "user_turn",
                "text": "slow please",
                "thread_id": thread_id,
            }
        )
        first = await ws_a.receive_json()
        while first.get("type") != "token":
            # Skip steer protocol frames (turn.started, …) — harness chunks only.
            first = await ws_a.receive_json()
        a_frames.append(first)
        async with _chat_ws(c, aid, auth) as ws_b:
            await ws_b.send_json({"type": "subscribe", "thread_id": thread_id})
            b_frames.append(await ws_b.receive_json())
            gate_release.set()
            rest_a, rest_b = await asyncio.gather(ws_a.drain_turn(), ws_b.drain_turn())
            a_frames.extend(rest_a)
            b_frames.extend(rest_b)
    return a_frames, b_frames


async def _two_threads_turn(
    c: httpx.AsyncClient,
    aid: str,
    auth: dict[str, str],
    tid_a: str,
    tid_b: str,
    *,
    text_a: str = "one",
    text_b: str = "two",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run two live turns on different threads over two concurrent sockets."""
    body_a: dict[str, Any] = {
        "type": "user_turn",
        "text": text_a,
        "messages": [{"role": "user", "content": text_a}],
        "thread_id": tid_a,
    }
    body_b: dict[str, Any] = {
        "type": "user_turn",
        "text": text_b,
        "messages": [{"role": "user", "content": text_b}],
        "thread_id": tid_b,
    }
    async with _chat_ws(c, aid, auth) as ws_a, _chat_ws(c, aid, auth) as ws_b:
        await ws_a.send_json(body_a)
        await ws_b.send_json(body_b)
        return await asyncio.gather(ws_a.drain_turn(), ws_b.drain_turn())  # type: ignore[return-value]


async def test_ws_concurrent_subscribers_both_receive_later_chunks(env: Any) -> None:
    """Two dashboard sockets on the same thread both get live tokens."""
    c, srv, _fake, alice_auth, _bob_auth, aid = env
    create = await c.post(f"/api/agents/{aid}/threads", headers=alice_auth)
    tid = create.json()["thread_id"]
    agent = srv.app_runtime.agent_registry.get_agent(aid)

    gate = asyncio.Event()

    async def slow_stream(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "token", "node": "agent", "content": "first"}
        await gate.wait()
        yield {"type": "token", "node": "agent", "content": "second"}

    agent.stream = slow_stream

    a_frames, b_frames = await _turn_with_second_subscriber(c, aid, alice_auth, tid, gate)

    assert a_frames[0].get("content") == "first"
    assert [f.get("content") for f in a_frames if f.get("type") == "token"] == ["first", "second"]
    assert a_frames[-1].get("type") == "done"
    assert b_frames[0] == {"type": "turn_status", "thread_id": tid, "active": True}
    assert [f.get("content") for f in b_frames if f.get("type") == "token"] == ["second"]
    assert b_frames[-1].get("type") == "done"


async def test_ws_steer_first_frame_on_new_connection_keeps_stream(env: Any) -> None:
    """A steer sent as a fresh connection's first user_turn must reach that socket.

    hub.subscribe used to run after _maybe_admit_steer — and was skipped
    entirely for steered frames — so steer.queued plus the running turn's
    tail were dropped when the original connection was already gone.
    """
    c, srv, _fake, alice_auth, _bob_auth, aid = env
    create = await c.post(f"/api/agents/{aid}/threads", headers=alice_auth)
    tid = create.json()["thread_id"]
    agent = srv.app_runtime.agent_registry.get_agent(aid)

    gate = asyncio.Event()

    async def slow_stream(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "token", "node": "agent", "content": "first"}
        await gate.wait()
        yield {"type": "token", "node": "agent", "content": "second"}

    agent.stream = slow_stream

    async with _chat_ws(c, aid, alice_auth) as ws_a:
        await ws_a.send_json({"type": "user_turn", "text": "slow please", "thread_id": tid})
        first = await ws_a.receive_json()
        while first.get("type") != "token":
            first = await ws_a.receive_json()
    # A is closed; the turn stays active, blocked on the gate.

    async with _chat_ws(c, aid, alice_auth) as ws_b:
        await ws_b.send_json(
            {
                "type": "user_turn",
                "text": "steer this",
                "thread_id": tid,
                "delivery": "auto",
                "client_pending_id": "cpid-steer-1",
            }
        )
        ack = await ws_b.receive_json()
        assert ack["type"] == "steer.ack"
        assert ack["status"] == "queued"
        gate.set()
        frames = await ws_b.drain_turn()

    types = [f.get("type") for f in frames]
    assert "steer.queued" in types
    assert [f.get("content") for f in frames if f.get("type") == "token"] == ["second"]
    assert frames[-1].get("type") == "done"


async def test_ws_two_threads_do_not_cross_stream(env: Any) -> None:
    """Two live turns on different threads must not mix tokens across sockets."""
    c, srv, _fake, alice_auth, _bob_auth, aid = env
    create_a = await c.post(f"/api/agents/{aid}/threads", headers=alice_auth)
    create_b = await c.post(f"/api/agents/{aid}/threads", headers=alice_auth)
    tid_a = create_a.json()["thread_id"]
    tid_b = create_b.json()["thread_id"]
    agent = srv.app_runtime.agent_registry.get_agent(aid)

    async def tagged_stream(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        tid = str(request.get("thread_id") or "")
        yield {"type": "token", "node": "agent", "content": f"from-{tid}"}

    agent.stream = tagged_stream

    a_frames, b_frames = await _two_threads_turn(c, aid, alice_auth, tid_a, tid_b)

    a_tokens = [f.get("content") for f in a_frames if f.get("type") == "token"]
    b_tokens = [f.get("content") for f in b_frames if f.get("type") == "token"]
    assert a_tokens == [f"from-{tid_a}"]
    assert b_tokens == [f"from-{tid_b}"]
    assert all(f.get("thread_id") == tid_a for f in a_frames if f.get("type") in ("token", "done"))
    assert all(f.get("thread_id") == tid_b for f in b_frames if f.get("type") in ("token", "done"))


async def _consume_ws_turn(
    c: httpx.AsyncClient,
    aid: str,
    auth: dict[str, str],
    *,
    text: str = "Hello",
    thread_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    body: dict[str, Any] = {
        "type": "user_turn",
        "text": text,
        "messages": [{"role": "user", "content": text}],
    }
    if thread_id:
        body["thread_id"] = thread_id
    if extra:
        body.update(extra)

    async with _chat_ws(c, aid, auth) as ws:
        await ws.send_json(body)
        return await ws.drain_turn()


async def test_ws_emits_chunks_then_done(env: Any) -> None:
    c, _srv, _fake, alice_auth, _bob_auth, aid = env
    chunks = await _consume_ws_turn(c, aid, alice_auth)
    await asyncio.sleep(0.05)
    types = [ch.get("type") for ch in chunks]
    assert "token" in types
    assert chunks[-1]["type"] == "done"


async def test_ws_disconnect_does_not_cancel_active_turn(env: Any) -> None:
    c, srv, _fake, alice_auth, _bob_auth, aid = env
    agent = srv.app_runtime.agent_registry.get_agent(aid)

    async def slow_stream(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "token", "node": "agent", "content": "started"}
        await asyncio.sleep(1)

    agent.stream = slow_stream
    original_cancel = srv.app_runtime.agent_registry.cancel_stream
    cancel_spy = MagicMock(wraps=original_cancel)
    srv.app_runtime.agent_registry.cancel_stream = cancel_spy

    async with _chat_ws(c, aid, alice_auth) as ws:
        await ws.send_json({"type": "user_turn", "text": "cancel me"})
        await ws.receive_json()

    await asyncio.sleep(0.05)
    cancel_spy.assert_not_called()


async def _subscribe_ws(
    c: httpx.AsyncClient,
    aid: str,
    auth: dict[str, str],
    thread_id: str,
) -> dict[str, Any]:
    async with _chat_ws(c, aid, auth) as ws:
        await ws.send_json({"type": "subscribe", "thread_id": thread_id})
        return await ws.receive_json()


async def test_ws_subscribe_turn_status_idle(env: Any) -> None:
    c, _srv, _fake, alice_auth, _bob_auth, aid = env
    create = await c.post(f"/api/agents/{aid}/threads", headers=alice_auth)
    assert create.status_code == 201
    tid = create.json()["thread_id"]

    frame = await _subscribe_ws(c, aid, alice_auth, tid)
    assert frame == {"type": "turn_status", "thread_id": tid, "active": False}


async def test_ws_subscribe_rejects_another_users_thread(env: Any) -> None:
    c, _srv, _fake, alice_auth, bob_auth, aid = env
    response = await c.patch(
        f"/api/agents/{aid}",
        headers=alice_auth,
        json={"is_shared": True},
    )
    assert response.status_code == 200, response.text

    response = await c.post(f"/api/agents/{aid}/threads", headers=bob_auth)
    assert response.status_code == 201, response.text
    tid = response.json()["thread_id"]

    frame = await _subscribe_ws(c, aid, alice_auth, tid)
    assert frame == {"type": "error", "message": f"thread {tid!r} not found"}


async def test_ws_cancel_frame_cancels_active_turn(env: Any) -> None:
    c, srv, _fake, alice_auth, _bob_auth, aid = env
    agent = srv.app_runtime.agent_registry.get_agent(aid)
    create = await c.post(f"/api/agents/{aid}/threads", headers=alice_auth)
    tid = create.json()["thread_id"]

    async def slow_stream(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "token", "node": "agent", "content": "started"}
        await asyncio.sleep(1)

    agent.stream = slow_stream
    original_cancel = srv.app_runtime.agent_registry.cancel_stream
    cancel_spy = MagicMock(wraps=original_cancel)
    srv.app_runtime.agent_registry.cancel_stream = cancel_spy

    async with _chat_ws(c, aid, alice_auth) as ws:
        await ws.send_json({"type": "user_turn", "text": "cancel me", "thread_id": tid})
        await ws.receive_json()  # first token
        await ws.send_json({"type": "cancel", "thread_id": tid})

    for _ in range(40):
        if cancel_spy.called:
            break
        await asyncio.sleep(0.01)
    cancel_spy.assert_called()
    assert cancel_spy.call_args.args[0] == aid
    assert cancel_spy.call_args.args[1] == tid


async def test_ws_emits_error_frame_on_exception(env: Any) -> None:
    c, srv, _fake, alice_auth, _bob_auth, aid = env
    agent = srv.app_runtime.agent_registry.get_agent(aid)
    agent.raise_on_stream = RuntimeError("upstream blew up")

    chunks = await _consume_ws_turn(c, aid, alice_auth, text="err")
    assert chunks[-1]["type"] == "error"


async def test_ws_auth_failure_has_actionable_error_and_releases_turn(env: Any) -> None:
    from langchain_core.exceptions import ModelAuthenticationError

    from octop.i18n.domains.stream import AUTH, stream_error_message

    c, srv, _fake, alice_auth, _bob_auth, aid = env
    agent = srv.app_runtime.agent_registry.get_agent(aid)
    agent.raise_on_stream = ModelAuthenticationError("opaque credential rejection")
    frames = await _consume_ws_turn(c, aid, alice_auth, text="check auth")
    error = next(frame for frame in frames if frame.get("type") == "error")
    assert error["message"] in (stream_error_message(AUTH, "zh"), stream_error_message(AUTH, "en"))
    # A corrected credential can be used by the very next turn.
    agent.raise_on_stream = None
    following = await _consume_ws_turn(c, aid, alice_auth, text="try after fixing credentials")
    assert not any(frame.get("type") == "error" for frame in following)


async def test_ws_bad_agent_rejected(env: Any) -> None:
    c, _srv, _fake, alice_auth, _bob_auth, _aid = env
    with pytest.raises(WebSocketDisconnect):
        async with _chat_ws(c, "01HMISSING0000000000000000", alice_auth):
            pass


async def test_ws_cross_user_rejected(env: Any) -> None:
    c, _srv, _fake, _admin_auth, bob_auth, aid = env
    with pytest.raises(WebSocketDisconnect):
        async with _chat_ws(c, aid, bob_auth):
            pass


async def test_ws_accepts_skills_and_model(env: Any) -> None:
    c, _srv, _fake, auth, _bob_auth, aid = env
    chunks = await _consume_ws_turn(
        c,
        aid,
        auth,
        extra={"skills": [], "model": "openai/gpt-4o"},
    )
    assert chunks[-1]["type"] == "done"


async def test_polish_rejects_empty_text(env: Any) -> None:
    c, _srv, _fake, alice_auth, _bob_auth, aid = env
    r = await c.post(
        f"/api/agents/{aid}/chat/polish",
        headers=alice_auth,
        json={"text": "   "},
    )
    assert r.status_code == 400


async def test_text_assist_chat_prompt_rejects_empty_text(env: Any) -> None:
    c, _srv, _fake, alice_auth, _bob_auth, aid = env
    r = await c.post(
        "/api/text/assist",
        headers=alice_auth,
        json={"kind": "chat.prompt", "text": "   ", "agent_id": aid},
    )
    assert r.status_code == 400


async def test_text_assist_form_requires_name(env: Any) -> None:
    c, _srv, _fake, alice_auth, _bob_auth, _aid = env
    r = await c.post(
        "/api/text/assist",
        headers=alice_auth,
        json={"kind": "expert.description", "text": "", "context": {}},
    )
    assert r.status_code == 400


async def test_threads_list_after_stream(env: Any) -> None:
    c, _srv, _fake, alice_auth, _bob_auth, aid = env
    await _consume_ws_turn(c, aid, alice_auth, text="What's up?")
    await asyncio.sleep(0.05)

    r = await c.get(f"/api/agents/{aid}/threads", headers=alice_auth)
    assert r.status_code == 200
    threads = r.json()
    assert len(threads) >= 1
    assert any(t.get("has_messages") for t in threads)


async def test_thread_history_after_stream(env: Any) -> None:
    c, _srv, _fake, alice_auth, _bob_auth, aid = env
    await _consume_ws_turn(c, aid, alice_auth, text="History test")
    await asyncio.sleep(0.05)

    r = await c.get(f"/api/agents/{aid}/threads", headers=alice_auth)
    tid = r.json()[0]["thread_id"]
    hist = await c.get(f"/api/agents/{aid}/threads/{tid}/history", headers=alice_auth)
    assert hist.status_code == 200
    assert "messages" in hist.json()


async def test_thread_history_reports_active_turn(env: Any) -> None:
    """History must expose whether a turn is still running, so a reloaded
    dashboard can re-subscribe instead of guessing from the message list."""
    c, srv, _fake, alice_auth, _bob_auth, aid = env
    create = await c.post(f"/api/agents/{aid}/threads", headers=alice_auth)
    tid = create.json()["thread_id"]

    idle = await c.get(f"/api/agents/{aid}/threads/{tid}/history", headers=alice_auth)
    assert idle.json()["turn_active"] is False

    srv.app_runtime.gateway.ws_hub.mark_turn_active(tid)
    active = await c.get(f"/api/agents/{aid}/threads/{tid}/history", headers=alice_auth)
    assert active.json()["turn_active"] is True


async def test_create_thread(env: Any) -> None:
    c, _srv, _fake, alice_auth, _bob_auth, aid = env
    r = await c.post(f"/api/agents/{aid}/threads", headers=alice_auth)
    assert r.status_code == 201
    body = r.json()
    assert "thread_id" in body
    assert "session_key" in body


async def test_fork_thread_from_assistant_message(env: Any) -> None:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    c, srv, _fake, alice_auth, bob_auth, aid = env
    created = await c.post(f"/api/agents/{aid}/threads", headers=alice_auth)
    source_id = created.json()["thread_id"]
    agent = srv.app_runtime.agent_registry.get_agent(aid)
    agent.seed_thread_messages(
        source_id,
        [
            HumanMessage(content="first question", id="h1"),
            AIMessage(content="first answer", id="a1"),
            ToolMessage(content="tool-out", id="t1", tool_call_id="c1"),
            HumanMessage(content="second question", id="h2"),
            AIMessage(content="second answer", id="a2"),
        ],
    )

    forked = await c.post(
        f"/api/agents/{aid}/threads/{source_id}/fork",
        headers=alice_auth,
        json={
            "message_id": "a1",
            "content": "first answer",
            "assistant_turns_from_end": 2,
        },
    )
    assert forked.status_code == 201, forked.text
    body = forked.json()
    dest_id = body["thread_id"]
    assert dest_id != source_id
    assert body["source_thread_id"] == source_id
    assert body["copied_messages"] == 2

    history = await c.get(
        f"/api/agents/{aid}/threads/{dest_id}/history",
        headers=alice_auth,
    )
    assert history.status_code == 200
    roles = [m["role"] for m in history.json()["messages"]]
    texts = []
    for msg in history.json()["messages"]:
        content = msg.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
                elif isinstance(block, dict) and block.get("type") == "tool_result":
                    texts.append(str(block.get("output") or ""))
    assert "user" in roles
    assert "assistant" in roles
    assert "first question" in texts
    assert "first answer" in texts
    assert "second question" not in texts
    assert "second answer" not in texts

    source_history = await c.get(
        f"/api/agents/{aid}/threads/{source_id}/history",
        headers=alice_auth,
    )
    source_texts: list[str] = []
    for msg in source_history.json()["messages"]:
        content = msg.get("content")
        if isinstance(content, str):
            source_texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    source_texts.append(str(block.get("text") or ""))
    assert "first question" in source_texts
    assert "second question" in source_texts

    denied = await c.post(
        f"/api/agents/{aid}/threads/{source_id}/fork",
        headers=bob_auth,
        json={"message_id": "a1", "assistant_turns_from_end": 2},
    )
    assert denied.status_code in {403, 404}


async def test_ws_private_knowledge_transport_to_executed_tool(env: Any, monkeypatch: Any) -> None:
    """Authenticated selection reaches the tool; forged contexts cannot grant it."""
    import json
    import time
    from types import SimpleNamespace

    from langgraph.config import var_child_runnable_config

    from octop.infra.auth.sso.crypto import encrypt_secret
    from octop.infra.feishu_kb import private_index
    from octop.infra.feishu_kb.service import FeishuKbService
    from octop.infra.knowledge import retrieve
    from octop.infra.knowledge.tools import build_knowledge_tools

    c, srv, _fake, alice_auth, bob_auth, aid = env
    services = srv.services
    uid = srv.app_runtime.agent_registry.get_row(aid).user_id
    subject = 'feishu-v1:["feishu","app","tenant","person"]'
    secret = encrypt_secret(services.secret_repo, "app-secret")
    token_blob = encrypt_secret(services.secret_repo, json.dumps({"access": "test", "refresh": "test"}))
    with services.db.transaction() as db:
        db.execute("INSERT INTO sso_providers (id,kind,enabled,client_id,client_secret_enc,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (9876, "feishu", 1, "app", secret, 1, 1))
        db.execute("INSERT INTO user_sso_identities(user_id,provider_id,subject,created_at) VALUES (?,?,?,?)", (uid, 9876, subject, 1))
        db.execute("INSERT INTO feishu_kb_grant(user_id,provider_id,subject,token_blob,expires_at) VALUES (?,?,?,?,?)", (uid, 9876, subject, token_blob, int(time.time()) + 3600))
        db.execute("INSERT INTO feishu_kb_source(source_id,user_id,provider_id,document_id,enabled,retrieval_enabled) VALUES (?,?,?,?,1,1)", ("test-source", uid, 9876, "doc"))
    monkeypatch.setattr(FeishuKbService, "_client", lambda *_: SimpleNamespace(raw_content=lambda *_: "private fact"))
    monkeypatch.setattr(private_index, "assert_knowledge_usable", lambda *_: None)
    monkeypatch.setattr(retrieve, "assert_knowledge_usable", lambda *_: None)
    monkeypatch.setattr(private_index, "embed_knowledge_texts", lambda _, texts: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(retrieve, "embed_knowledge_texts", lambda _, texts: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(retrieve, "get_rerank_capability", lambda *_: {"usable": False})
    monkeypatch.setattr(retrieve, "graph_enabled", lambda *_: False)
    tool = build_knowledge_tools(services)[0]
    agent = srv.app_runtime.agent_registry.get_agent(aid)
    observed = []

    async def run_tool(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        config = dict(request.get("configurable") or {})
        config["user"] = request["user"]
        ctx = var_child_runnable_config.set({"configurable": config})
        try:
            observed.append((config, await tool.ainvoke({"query": "fact", "user_id": int(uid) + 10000})))
            forged = dict(config, user=str(int(uid) + 10000), user_is_admin=True)
            forged_ctx = var_child_runnable_config.set({"configurable": forged})
            try:
                observed.append((forged, await tool.ainvoke({"query": "fact"})))
            finally:
                var_child_runnable_config.reset(forged_ctx)
        finally:
            var_child_runnable_config.reset(ctx)
        yield {"type": "token", "content": "complete"}

    agent.stream = run_tool
    selected = "private-feishu:test-source"
    await _consume_ws_turn(c, aid, alice_auth, extra={"knowledge_base_ids": [selected]})
    assert selected in observed[0][0]["knowledge_base_ids"]
    assert "private fact" in observed[0][1]
    assert "private fact" not in observed[1][1]
    await _consume_ws_turn(c, aid, alice_auth, extra={"knowledge_base_ids": []})
    assert observed[2][0]["knowledge_base_ids"] == []
    assert "private fact" not in observed[2][1]
    with pytest.raises(WebSocketDisconnect):
        async with _chat_ws(c, aid, bob_auth):
            pass
    bob_agent_id = await create_agent(c, bob_auth)
    bob_agent = srv.app_runtime.agent_registry.get_agent(bob_agent_id)
    bob_agent.stream = run_tool
    await _consume_ws_turn(c, bob_agent_id, bob_auth, extra={"knowledge_base_ids": [selected]})
    assert "private fact" not in observed[4][1]
    # IM/cron-style runnable configuration alone has no transport capability.
    ctx = var_child_runnable_config.set({"configurable": {"user": str(uid), "user_is_admin": True, "knowledge_base_ids": [selected]}})
    try:
        assert "private fact" not in await tool.ainvoke({"query": "fact"})
    finally:
        var_child_runnable_config.reset(ctx)


async def test_private_selection_blocks_sharing_and_existing_subscriber(env: Any) -> None:
    """A former subscriber must not receive a later private turn or its history."""
    c, srv, _fake, alice_auth, bob_auth, aid = env
    srv.services.agent_repo.set_shared(aid, True)
    created = await c.post(f"/api/agents/{aid}/threads", headers=alice_auth)
    tid = created.json()["thread_id"]
    path = f"/api/agents/{aid}/threads/{tid}"
    shared = await c.patch(path, headers=alice_auth, json={"contributed": True})
    assert shared.status_code == 200, shared.text

    async with _chat_ws(c, aid, bob_auth) as bob:
        await bob.send_json({"type": "subscribe", "thread_id": tid})
        assert (await bob.receive_json())["type"] == "turn_status"
        snapshot = await bob.receive_json()
        assert snapshot["type"] == "steer.queue_snapshot"
        assert snapshot["items"] == []
        denied = await _consume_ws_turn(
            c, aid, alice_auth, thread_id=tid,
            extra={"knowledge_base_ids": ["private-feishu:source"]},
        )
        assert denied[0]["type"] == "error"
        assert not srv.app_runtime.gateway.thread_registry.get_thread(tid).private_knowledge_protected
        # The subscription snapshot precedes the private turn, not a broadcast from it.
        await bob.send_json({"type": "ping"})
        assert (await bob.receive_json())["type"] == "pong"
        unshared = await c.patch(path, headers=alice_auth, json={"contributed": False})
        assert unshared.status_code == 200
        private_text = "private-turn-distinct-canary"
        frames = await _consume_ws_turn(
            c, aid, alice_auth, text=private_text, thread_id=tid,
            extra={"knowledge_base_ids": ["private-feishu:source"]},
        )
        assert any(frame.get("type") == "token" for frame in frames)
        assert srv.app_runtime.gateway.thread_registry.get_thread(tid).private_knowledge_protected
        await bob.send_json({"type": "ping"})
        while True:
            frame = await bob.receive_json()
            if frame["type"] == "pong":
                break
            # Refusals can be delivered to the socket; private turn events cannot.
            assert frame["type"] == "error", frame
            assert private_text not in str(frame)
            assert "Hello " not in str(frame) and "Bob." not in str(frame)
        await bob.send_json({"type": "subscribe", "thread_id": tid})
        refusal = await bob.receive_json()
        assert refusal["type"] == "error", refusal
        assert private_text not in str(refusal)

    for suffix in ("/history", "/trajectory/export", "/trajectory/stream"):
        response = await c.get(path + suffix, headers=bob_auth)
        assert response.status_code == 403, (suffix, response.text)
    response = await c.patch(path, headers=alice_auth, json={"contributed": True})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "THREAD_PRIVATE_KNOWLEDGE"
    fork = await c.post(path + "/fork", headers=alice_auth, json={"message_id": "missing"})
    assert fork.status_code == 403
    ordinary = await c.post(f"/api/agents/{aid}/threads", headers=alice_auth)
    ordinary_id = ordinary.json()["thread_id"]
    assert (await c.patch(
        f"/api/agents/{aid}/threads/{ordinary_id}",
        headers=alice_auth, json={"contributed": True},
    )).status_code == 200
