"""Real harness peer tools, inbox worker, and target memory with local models."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver
from octop_harness.config import HarnessAgentConfig, ModelConfig, ProviderConfig
from octop_harness.llm.factory import ChatModelFactory
from octop_harness.manager import HarnessAgentManager
from octop_harness.teams.processor import default_compose_followup

from octop.infra.agents.middleware.private_memory import (
    guard_memory_service,
    invocation_root_thread,
    private_memory_scope,
)
from octop.infra.agents.teams.private_peer import wire_private_peer_guard
from octop.infra.cron.manager import CronManager
from octop.infra.cron.tools import build_cronjob_tools
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.sessions import SessionRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import RepoBundle
from octop.infra.knowledge.private_authority import dashboard_authority, install, reset


class LocalModel(FakeListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class Processor:
    def __init__(self):
        self.replies = []

    def compose_followup(self, msg, *, result_text, error_text):
        return default_compose_followup(msg, result_text=result_text, error_text=error_text)

    async def on_reply(self, event):
        self.replies.append(event)


@pytest.mark.asyncio
async def test_peer_tool_and_worker_check_database_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    monkeypatch.setattr(
        ChatModelFactory,
        "get_chat_model",
        lambda *args, **kwargs: LocalModel(responses=["A useful public answer for this task."]),
    )
    db = SqlitePool(tmp_path / "control.db")
    run_migrations(db)
    UserRepo(db).create(username="owner", password_hash="x", role="user")
    AgentRepo(db).create(agent_id="host", user_id=1, name="Host")
    AgentRepo(db).create(agent_id="target", user_id=1, name="Target")
    repo = ThreadRepo(db)
    for tid in ("private", "pending", "public", "public-two"):
        repo.insert(
            thread_id=tid,
            agent_id="host",
            user_id=1,
            channel_type="dashboard",
            session_key="host:dashboard:1:dm",
        )
    target_session = "target:dashboard:1:dm"
    repo.insert(
        thread_id="target-session",
        agent_id="target",
        user_id=1,
        channel_type="dashboard",
        session_key=target_session,
    )
    sessions = SessionRepo(db)
    sessions.upsert(
        session_key=target_session,
        agent_id="target",
        user_id=1,
        channel_type="dashboard",
        chat_type="dm",
        thread_id="target-session",
    )
    assert repo.protect_private_knowledge("private", 1)
    processor = Processor()
    provider = ProviderConfig(
        id="fake",
        base_url="https://example.invalid",
        api_key="test",
        models=[ModelConfig(id="fake", enabled=True)],
    )
    manager = HarnessAgentManager(providers=[provider], team_processor=processor)
    for aid in ("host", "target"):
        cfg = HarnessAgentConfig(
            workspace_dir=tmp_path / aid,
            providers=[provider],
            default_model="fake/fake",
            name=aid,
            memory_aux_model_enabled=False,
            memory_extract_on_session_end=False,
            memory_jsonl_enabled=False,
            checkpointer=InMemorySaver(),
            backend="state",
        )
        manager.create_agent(cfg, agent_id=aid, metadata={"user_id": 1}, init_workspace=False)
    wire_private_peer_guard(manager.team, repo, None)
    target = manager.get_agent("target").agent
    graph_entries = []
    cron_results = []
    gateway = MagicMock()
    gateway.thread_registry.get_session.side_effect = sessions.get
    cron = CronManager(
        gateway=gateway, delivery_service=MagicMock(), repos=RepoBundle.from_pool(db)
    )
    cron_create = next(t for t in build_cronjob_tools(cron) if t.name == "cronjob_create")
    graph = target._graph
    original_ainvoke = type(graph).ainvoke

    async def observe_graph(self, input, config=None, **kwargs):
        if self is graph:
            graph_entries.append((input, config, invocation_root_thread()))
            cron_results.append(
                json.loads(
                    await cron_create.ainvoke(
                        {
                            "trigger": "interval:60",
                            "prompt": "test",
                            "name": "test",
                            "enabled": False,
                        },
                        config={
                            "configurable": {
                                "agent_id": "target", "user": "1", "session_key": target_session,
                            }
                        },
                    )
                )
            )
        return await original_ainvoke(self, input, config=config, **kwargs)

    monkeypatch.setattr(type(graph), "ainvoke", observe_graph)
    inbox = manager.team.inbox
    statuses = []
    original_set_status = inbox._set_status

    def observe_status(msg, status):
        statuses.append((msg.id, status))
        original_set_status(msg, status)

    monkeypatch.setattr(inbox, "_set_status", observe_status)
    release_consumer = asyncio.Event()

    class BarrierQueue(asyncio.Queue):
        async def get(self):
            await release_consumer.wait()
            return await super().get()

    inbox._queue = BarrierQueue()
    memory = target._memory_runtime.service
    guard_memory_service(memory)
    middleware = target._memory_runtime.middleware
    tool = next(t for t in manager.team.team_tools() if t.name == "ask_agent")
    config = {"configurable": {"agent_id": "host", "user": 1, "thread_id": "private"}}
    private_text = "privatepeeruniquecanary " * 12
    public_text = "publicpeeruniquecanary " * 12
    try:
        with private_memory_scope(repo, "public"), private_memory_scope(repo, "private"):
            for mode in ("sync", "background"):
                denied = await tool.ainvoke(
                    {"expert": "target", "message": private_text, "mode": mode},
                    config=config,
                )
                assert "unavailable" in denied and private_text not in denied
            denied_cron = json.loads(
                await cron_create.ainvoke(
                    {"trigger": "interval:60", "prompt": private_text, "name": "private"},
                    config={"configurable": {"agent_id": "host", "user": "1"}},
                )
            )
            assert denied_cron == {"error": "Scheduled jobs are unavailable for this conversation."}
            assert not cron.list_by_agent("host", include_disabled=True)
            with pytest.raises(PermissionError):
                inbox.enqueue(
                    target_agent_id="target",
                    source_agent_id="host",
                    source_thread_id="public",
                    message=private_text,
                    user_id=1,
                )
        authority = install(dashboard_authority(2, []))
        try:
            with private_memory_scope(repo, "public"):
                forged = await tool.ainvoke(
                    {"expert": "target", "message": private_text},
                    config={"configurable": {"agent_id": "host", "user": 1, "thread_id": "public"}},
                )
                assert "unavailable" in forged
        finally:
            reset(authority)
        with private_memory_scope(repo, "private"):
            denied = await tool.ainvoke(
                {"expert": "target", "message": private_text}, config=config
            )
            assert "unavailable" in denied and private_text not in denied
            denied_background = await tool.ainvoke(
                {"expert": "target", "message": private_text, "mode": "background"},
                config=config,
            )
            assert "unavailable" in denied_background and private_text not in denied_background
            gate = asyncio.Event()

            async def inherited():
                await gate.wait()
                with private_memory_scope(repo, "public"):
                    return await tool.ainvoke(
                        {"expert": "target", "message": private_text},
                        config={
                            "configurable": {"agent_id": "host", "user": 1, "thread_id": "public"}
                        },
                    )

            child = asyncio.create_task(inherited())
        gate.set()
        assert "unavailable" in await child
        assert not graph_entries
        with private_memory_scope(repo, "pending"):
            queued = json.loads(
                await tool.ainvoke(
                    {"expert": "target", "message": private_text, "mode": "background"},
                    config={
                        "configurable": {"agent_id": "host", "user": 1, "thread_id": "pending"}
                    },
                )
            )
        assert queued["status"] == "queued"
        assert inbox._queue.qsize() == 1
        assert repo.protect_private_knowledge("pending", 1)
        release_consumer.set()
        await asyncio.wait_for(inbox._queue.join(), timeout=8)
        await asyncio.wait_for(asyncio.gather(*list(inbox._tasks.values())), timeout=8)
        assert (queued["job_id"], "running") in statuses
        assert (queued["job_id"], "cancelled") in statuses
        assert not graph_entries
        assert not processor.replies
        assert not memory.search("privatepeeruniquecanary", thread_id="other-user")["hits"]
        with pytest.raises(PermissionError):
            await manager.team._invoke_peer(
                from_agent_id="host",
                to_agent_id="target",
                message=private_text,
                user_id=1,
                source="inbox",
                source_thread_id="pending",
                source_session_key=None,
                job_id="late",
            )
        assert not graph_entries
        with private_memory_scope(repo, "public"):
            result = json.loads(
                await asyncio.wait_for(
                    tool.ainvoke(
                        {"expert": "target", "message": public_text, "mode": "background"},
                        config={
                            "configurable": {"agent_id": "host", "user": 1, "thread_id": "public"}
                        },
                    ),
                    timeout=8,
                )
            )
        assert result["status"] == "queued"
        await asyncio.wait_for(inbox._queue.join(), timeout=8)
        await asyncio.wait_for(asyncio.gather(*list(inbox._tasks.values())), timeout=8)
        assert (result["job_id"], "running") in statuses
        assert (result["job_id"], "done") in statuses
        assert len(processor.replies) == 1
        assert processor.replies[0].status == "done"
        assert len(graph_entries) == 1
        graph_input, graph_config, root = graph_entries[0]
        assert root == "public"
        assert "error" not in cron_results[0], cron_results[0]
        assert cron_results[0]["prompt"] == "test"
        assert cron.get(cron_results[0]["id"]).agent_id == "target"
        assert cron.get(cron_results[0]["id"]).session_key == target_session
        assert "publicpeeruniquecanary" in str(graph_input)
        assert graph_config["configurable"]["thread_id"] != "other-user"
        with middleware._background_condition:
            assert middleware._background_condition.wait_for(
                lambda: middleware._background_active == 0, timeout=5
            )
        assert not memory.search("privatepeeruniquecanary", thread_id="other-user")["hits"]
        assert memory.search("publicpeeruniquecanary", thread_id="other-user")["hits"]
        with private_memory_scope(repo, "public-two"):
            second = json.loads(
                await tool.ainvoke(
                    {"expert": "target", "message": "secondpublicpeer " * 12, "mode": "background"},
                    config={
                        "configurable": {"agent_id": "host", "user": 1, "thread_id": "public-two"}
                    },
                )
            )
        assert second["status"] == "queued"
        await asyncio.wait_for(inbox._queue.join(), timeout=8)
        await asyncio.wait_for(asyncio.gather(*list(inbox._tasks.values())), timeout=8)
        assert (second["job_id"], "done") in statuses
        assert graph_entries[-1][2] == "public-two"
        assert "error" not in cron_results[-1], cron_results[-1]
        assert cron.get(cron_results[-1]["id"]) is not None
    finally:
        release_consumer.set()
        await manager.aclose()
        db.close()
