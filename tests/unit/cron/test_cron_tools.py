"""Unit tests for built-in cronjob LangChain tools."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.config import var_child_runnable_config

from octop.config import OctopConfig
from octop.infra.agents.middleware.private_memory import private_memory_scope
from octop.infra.cron.delivery import CronDeliveryService
from octop.infra.cron.manager import CronManager
from octop.infra.cron.tools import build_cronjob_tools
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.utils.paths import PathLayout
from octop.infra.utils.ulid import new_ulid


@contextmanager
def _configurable(services, *, root_agent_id: str | None = None, **kwargs: object):
    thread_id = str(kwargs.get("thread_id") or new_ulid())
    agent_id = str(kwargs["agent_id"])
    user_id = int(str(kwargs["user"]))
    repo = services.repos.thread_repo
    if repo.get(thread_id) is None:
        repo.insert(
            thread_id=thread_id,
            agent_id=root_agent_id or agent_id,
            user_id=user_id,
            channel_type="dashboard",
            session_key=ThreadRegistry.dashboard_key(
                agent_id=root_agent_id or agent_id, user_id=user_id
            ),
        )
    kwargs["thread_id"] = thread_id
    token = var_child_runnable_config.set({"configurable": kwargs})
    try:
        with private_memory_scope(repo, thread_id):
            yield
    finally:
        var_child_runnable_config.reset(token)


def _make_services(tmp_path: Path):
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    return build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())


def _make_manager(services) -> CronManager:
    gw = MagicMock()
    gw.thread_registry = MagicMock()
    gw.thread_registry.get_session = MagicMock(return_value=None)
    gw.thread_registry.get_or_create_by_key = AsyncMock(return_value="thr_test")
    mgr = CronManager(
        gateway=gw,
        delivery_service=CronDeliveryService(
            gateway=gw,
            agent_manager=MagicMock(),
            repos=services.repos,
        ),
        repos=services.repos,
        timezone="UTC",
    )
    mgr._scheduler = MagicMock()
    mgr._scheduler.get_job = MagicMock(return_value=None)
    return mgr


def _tool_by_name(tools: list, name: str):
    for tool in tools:
        if tool.name == name:
            return tool
    raise KeyError(name)


@pytest.mark.asyncio
async def test_cronjob_create_and_list(tmp_path: Path) -> None:
    services = _make_services(tmp_path)
    agent_id = new_ulid()
    user_id = services.repos.user_repo.create(username="u1", password_hash="x", role="user")
    services.repos.agent_repo.create(agent_id=agent_id, user_id=user_id, name="a1")
    mgr = _make_manager(services)
    tools = build_cronjob_tools(mgr)
    create = _tool_by_name(tools, "cronjob_create")
    list_tool = _tool_by_name(tools, "cronjob_list")

    with _configurable(services, agent_id=agent_id, user=str(user_id)):
        out = await create.ainvoke(
            {
                "trigger": "interval:120",
                "prompt": "say hi",
                "name": "say hi",
            }
        )
        data = json.loads(out)
        assert data["prompt"] == "say hi"
        assert data["name"] == "say hi"
        assert data["trigger"] == "interval:120"
        assert data["task_type"] == "text"
        cron_id = data["id"]

        listed = json.loads(await list_tool.ainvoke({"include_disabled": True}))
        assert len(listed) == 1
        assert listed[0]["id"] == cron_id


@pytest.mark.asyncio
async def test_cronjob_isolated_by_agent_and_user(tmp_path: Path) -> None:
    services = _make_services(tmp_path)
    agent_a = new_ulid()
    agent_b = new_ulid()
    other_agent = new_ulid()
    uid = services.repos.user_repo.create(username="owner", password_hash="x", role="user")
    other_uid = services.repos.user_repo.create(username="other", password_hash="x", role="user")
    services.repos.agent_repo.create(agent_id=agent_a, user_id=uid, name="a")
    services.repos.agent_repo.create(agent_id=agent_b, user_id=uid, name="b")
    services.repos.agent_repo.create(agent_id=other_agent, user_id=other_uid, name="other")
    mgr = _make_manager(services)
    tools = build_cronjob_tools(mgr)
    create = _tool_by_name(tools, "cronjob_create")
    get_tool = _tool_by_name(tools, "cronjob_get")
    list_tool = _tool_by_name(tools, "cronjob_list")

    with _configurable(services, agent_id=agent_a, user=str(uid)):
        created = json.loads(
            await create.ainvoke({"trigger": "interval:60", "prompt": "a", "name": "a"})
        )
    cron_id = created["id"]

    with _configurable(services, agent_id=agent_b, user=str(uid)):
        err = json.loads(await get_tool.ainvoke({"cron_id": cron_id}))
        assert "error" in err

    with _configurable(services, root_agent_id=other_agent, agent_id=agent_a, user=str(other_uid)):
        err = json.loads(await get_tool.ainvoke({"cron_id": cron_id}))
        assert "error" in err
        listed = json.loads(await list_tool.ainvoke({"include_disabled": True}))
        assert listed == []
        created = json.loads(
            await create.ainvoke({"trigger": "interval:60", "prompt": "nope", "name": "nope"})
        )
        assert "error" in created


@pytest.mark.asyncio
async def test_cronjob_delete(tmp_path: Path) -> None:
    services = _make_services(tmp_path)
    agent_id = new_ulid()
    user_id = services.repos.user_repo.create(username="del", password_hash="x", role="user")
    services.repos.agent_repo.create(agent_id=agent_id, user_id=user_id, name="a")
    mgr = _make_manager(services)
    tools = build_cronjob_tools(mgr)
    create = _tool_by_name(tools, "cronjob_create")
    delete = _tool_by_name(tools, "cronjob_delete")

    with _configurable(services, agent_id=agent_id, user=str(user_id)):
        created = json.loads(
            await create.ainvoke({"trigger": "interval:30", "prompt": "p", "name": "p"})
        )
        cron_id = created["id"]
        out = json.loads(await delete.ainvoke({"cron_id": cron_id}))
        assert out["deleted"] == cron_id
        assert mgr.get(cron_id) is None


@pytest.mark.asyncio
async def test_cronjob_run_now(tmp_path: Path) -> None:
    services = _make_services(tmp_path)
    agent_id = new_ulid()
    user_id = services.repos.user_repo.create(username="run", password_hash="x", role="user")
    services.repos.agent_repo.create(agent_id=agent_id, user_id=user_id, name="a")
    mgr = _make_manager(services)
    mgr.run_now = AsyncMock()  # type: ignore[method-assign]
    tools = build_cronjob_tools(mgr)
    create = _tool_by_name(tools, "cronjob_create")
    run_now = _tool_by_name(tools, "cronjob_run_now")

    with _configurable(services, agent_id=agent_id, user=str(user_id)):
        created = json.loads(
            await create.ainvoke({"trigger": "interval:30", "prompt": "p", "name": "p"})
        )
        cron_id = created["id"]
        out = json.loads(await run_now.ainvoke({"cron_id": cron_id}))
        assert out["triggered"] == cron_id
        mgr.run_now.assert_awaited_once_with(cron_id)


@pytest.mark.asyncio
async def test_cronjob_create_uses_configurable_session_key(tmp_path: Path) -> None:
    services = _make_services(tmp_path)
    agent_id = new_ulid()
    user_id = services.repos.user_repo.create(username="sk", password_hash="x", role="user")
    services.repos.agent_repo.create(agent_id=agent_id, user_id=user_id, name="a")
    mgr = _make_manager(services)
    tools = build_cronjob_tools(mgr)
    create = _tool_by_name(tools, "cronjob_create")
    feishu_sk = ThreadRegistry.make_key(
        agent_id=agent_id,
        channel_type="feishu",
        channel_subject_id="ou_abc",
        channel_chat_type=ThreadRegistry.CHAT_TYPE_DM,
    )

    with _configurable(services, agent_id=agent_id, user=str(user_id), session_key=feishu_sk):
        out = await create.ainvoke({"trigger": "interval:30", "prompt": "ping", "name": "ping"})
    data = json.loads(out)
    assert data["session_key"] == feishu_sk


@pytest.mark.asyncio
async def test_cronjob_create_pins_history_thread(tmp_path: Path) -> None:
    services = _make_services(tmp_path)
    agent_id = new_ulid()
    user_id = services.repos.user_repo.create(username="hist", password_hash="x", role="user")
    services.repos.agent_repo.create(agent_id=agent_id, user_id=user_id, name="a")
    mgr = _make_manager(services)
    mgr._gateway.thread_registry.get_thread = services.repos.thread_repo.get
    create = _tool_by_name(build_cronjob_tools(mgr), "cronjob_create")

    with _configurable(services, agent_id=agent_id, user=str(user_id), thread_id="thr_current"):
        out = await create.ainvoke(
            {
                "trigger": "interval:30",
                "prompt": "stay here",
                "name": "stay",
                "session_target": "history",
            }
        )
    data = json.loads(out)
    assert data["session_key"] == "thread:thr_current"


@pytest.mark.asyncio
async def test_cronjob_create_fresh_thread_ignores_history_pin(tmp_path: Path) -> None:
    services = _make_services(tmp_path)
    agent_id = new_ulid()
    user_id = services.repos.user_repo.create(username="fresh", password_hash="x", role="user")
    services.repos.agent_repo.create(agent_id=agent_id, user_id=user_id, name="a")
    mgr = _make_manager(services)
    create = _tool_by_name(build_cronjob_tools(mgr), "cronjob_create")
    channel_key = ThreadRegistry.dashboard_key(agent_id=agent_id, user_id=user_id)

    with _configurable(services, agent_id=agent_id, user=str(user_id), thread_id="thr_current"):
        out = await create.ainvoke(
            {
                "trigger": "interval:30",
                "prompt": "new chat",
                "name": "new",
                "session_target": "history",
                "fresh_thread": True,
            }
        )
    data = json.loads(out)
    assert data["session_key"] == channel_key
    assert data["fresh_thread"] is True


@pytest.mark.asyncio
async def test_cronjob_create_persists_task_type(tmp_path: Path) -> None:
    services = _make_services(tmp_path)
    agent_id = new_ulid()
    user_id = services.repos.user_repo.create(username="tt", password_hash="x", role="user")
    services.repos.agent_repo.create(agent_id=agent_id, user_id=user_id, name="a")
    mgr = _make_manager(services)
    tools = build_cronjob_tools(mgr)
    create = _tool_by_name(tools, "cronjob_create")

    with _configurable(services, agent_id=agent_id, user=str(user_id)):
        out = await create.ainvoke(
            {"trigger": "interval:30", "prompt": "ping", "name": "ping", "task_type": "text"}
        )
    data = json.loads(out)
    assert data["task_type"] == "text"
    row = mgr.get(data["id"])
    assert row is not None
    assert row.task_type == "text"


@pytest.mark.asyncio
async def test_cronjob_create_requires_name(tmp_path: Path) -> None:
    from pydantic import ValidationError

    services = _make_services(tmp_path)
    agent_id = new_ulid()
    user_id = services.repos.user_repo.create(username="req", password_hash="x", role="user")
    services.repos.agent_repo.create(agent_id=agent_id, user_id=user_id, name="a")
    mgr = _make_manager(services)
    create = _tool_by_name(build_cronjob_tools(mgr), "cronjob_create")

    with (
        _configurable(services, agent_id=agent_id, user=str(user_id)),
        pytest.raises(ValidationError),
    ):
        await create.ainvoke({"trigger": "interval:30", "prompt": "ping"})


@pytest.mark.asyncio
async def test_cronjob_create_blank_name_uses_prompt_prefix(tmp_path: Path) -> None:
    services = _make_services(tmp_path)
    agent_id = new_ulid()
    user_id = services.repos.user_repo.create(username="fb", password_hash="x", role="user")
    services.repos.agent_repo.create(agent_id=agent_id, user_id=user_id, name="a")
    mgr = _make_manager(services)
    create = _tool_by_name(build_cronjob_tools(mgr), "cronjob_create")

    with _configurable(services, agent_id=agent_id, user=str(user_id)):
        out = await create.ainvoke(
            {
                "trigger": "interval:30",
                "prompt": "该喝水了💧\n记得站起来活动一下",
                "name": "   ",
            }
        )
    data = json.loads(out)
    assert data["name"] == "该喝水了💧"


@pytest.mark.asyncio
async def test_cronjob_create_rejects_unknown_and_protected_source(tmp_path: Path) -> None:
    services = _make_services(tmp_path)
    user_id = services.repos.user_repo.create(username="guard", password_hash="x", role="user")
    agent_id = new_ulid()
    services.repos.agent_repo.create(agent_id=agent_id, user_id=user_id, name="a")
    mgr = _make_manager(services)
    tools = build_cronjob_tools(mgr)
    create = _tool_by_name(tools, "cronjob_create")
    update = _tool_by_name(tools, "cronjob_update")
    args = {"trigger": "interval:30", "prompt": "p", "name": "p"}
    with _configurable(services, agent_id=agent_id, user=str(user_id), thread_id="public"):
        created = json.loads(await create.ainvoke(args))
    cron_id = created["id"]
    before = mgr.get(cron_id)
    assert before is not None
    denial = {"error": "Scheduled jobs are unavailable for this conversation."}
    update_args = {"cron_id": cron_id, "prompt": "changed"}
    token = var_child_runnable_config.set(
        {"configurable": {"agent_id": agent_id, "user": str(user_id)}}
    )
    try:
        assert "error" in json.loads(await create.ainvoke(args))
        assert json.loads(await update.ainvoke(update_args)) == denial
        assert mgr.get(cron_id) == before
        with private_memory_scope(services.repos.thread_repo, "unknown"):
            assert "error" in json.loads(await create.ainvoke(args))
            assert json.loads(await update.ainvoke(update_args)) == denial
            assert mgr.get(cron_id) == before
        with _configurable(services, agent_id=agent_id, user=str(user_id), thread_id="protected"):
            assert services.repos.thread_repo.protect_private_knowledge("protected", user_id)
            assert "error" in json.loads(await create.ainvoke(args))
            assert json.loads(await update.ainvoke(update_args)) == denial
            assert mgr.get(cron_id) == before
    finally:
        var_child_runnable_config.reset(token)
