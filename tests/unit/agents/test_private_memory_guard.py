"""Exercise installed harness capture and memory tools against a real local store."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from octop_harness.builtin.tools.memory_tools import build_memory_tools
from octop_harness.middleware.memory import MemoryMiddleware
from octop_memory import Memory, MemoryService

from octop.infra.agents.manager import AgentManager
from octop.infra.agents.middleware.private_memory import (
    PrivateMemoryGuardMiddleware,
    guard_memory_service,
    private_memory_scope,
)


@pytest.mark.asyncio
async def test_default_subagent_inherits_guarded_memory_save(tmp_path, monkeypatch):
    import deepagents.graph as graph

    memory = Memory("child_privacy", backend_config={"db_path": str(tmp_path / "memory.db")})
    service = MemoryService(memory)
    tools = build_memory_tools(service)
    selected = []
    original = graph.SubAgentMiddleware

    class InspectSubAgentMiddleware(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            selected.extend(self._subagents)

    monkeypatch.setattr(graph, "SubAgentMiddleware", InspectSubAgentMiddleware)
    try:
        graph.create_deep_agent(model=FakeListChatModel(responses=["ok"]), tools=tools)
        child = next(spec for spec in selected if spec["name"] == "general-purpose")
        save = next(tool for tool in child["tools"] if tool.name == "memory_save")
        # Install after graph construction, as AgentManager does on the live service.
        guard_memory_service(service)
        rows = {
            "public": SimpleNamespace(private_knowledge_protected=False),
            "protected": SimpleNamespace(private_knowledge_protected=True),
        }
        repo = SimpleNamespace(get=lambda tid: rows.get(tid))
        ready = asyncio.Event()

        async def detached():
            await ready.wait()
            with private_memory_scope(repo, "public"):
                return await save.ainvoke({"content": "privatechildcanary"})

        with private_memory_scope(repo, "protected"):
            task = asyncio.create_task(detached())
        ready.set()
        assert "failed" in await task
        assert not service.search("privatechildcanary", thread_id="other")["hits"]
        with private_memory_scope(repo, "public"):
            assert "saved" in await save.ainvoke({"content": "publicchildcanary"})
        assert service.search("publicchildcanary", thread_id="other")["hits"]
    finally:
        memory.backend.close()


def test_protected_capture_and_tool_cannot_reach_shared_memory(tmp_path):
    memory = Memory("privacy_test", backend_config={"db_path": str(tmp_path / "memory.db")})
    service = MemoryService(memory, config={"capture": {"min_message_chars": 0}})
    middleware = MemoryMiddleware(service=service, jsonl_enabled=False)
    rows = {
        "public": SimpleNamespace(private_knowledge_protected=False),
        "protected": SimpleNamespace(private_knowledge_protected=True),
    }
    repo = SimpleNamespace(get=lambda tid: rows.get(tid))
    host = SimpleNamespace(_repos=SimpleNamespace(thread_repo=repo), get_config=lambda _: {})
    guard = PrivateMemoryGuardMiddleware(repo)
    save = next(tool for tool in build_memory_tools(service) if tool.name == "memory_save")
    try:
        for tid, word in (
            ("public", "publiccanary"),
            ("protected", "privatecanary"),
            ("protected", "latercanary"),
        ):
            req = AgentManager._prepare_stream_request(
                host,
                "agent",
                {"thread_id": tid, "configurable": {"octop_memory_capture_exclude": False}},
            )
            runtime = SimpleNamespace(
                config={
                    "configurable": {**req.get("configurable", {}), "thread_id": tid, "user": "1"}
                }
            )
            human = HumanMessage(content=f"question about {word}")
            middleware.before_model({"messages": [human]}, runtime)
            middleware.after_model(
                {"messages": [human, AIMessage(content=f"answer contains {word}")]}, runtime
            )
        with middleware._background_condition:
            assert middleware._background_condition.wait_for(
                lambda: middleware._background_active == 0, timeout=5
            )
        assert service.search("publiccanary", thread_id="other-user")["hits"]
        assert not service.search("privatecanary", thread_id="other-user")["hits"]
        assert not service.search("latercanary", thread_id="other-user")["hits"]
        for tid, word in (("public", "publicsaved"), ("protected", "privatesaved")):
            call = {
                "name": "memory_save",
                "args": {"content": word},
                "id": tid,
                "type": "tool_call",
            }
            request = ToolCallRequest(
                tool_call=call,
                tool=save,
                state={},
                runtime=SimpleNamespace(config={"configurable": {"thread_id": tid}}),
            )
            guard.wrap_tool_call(request, lambda r: save.invoke(r.tool_call))
        assert service.search("publicsaved", thread_id="other-user")["hits"]
        assert not service.search("privatesaved", thread_id="other-user")["hits"]
        middleware.end_session("protected", background=False)
        assert not service.search("privatecanary", thread_id="other-user")["hits"]
    finally:
        middleware.shutdown()
        memory.backend.close()
