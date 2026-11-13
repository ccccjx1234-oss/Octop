"""Compatibility must preserve diagnostics without opening execution paths."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from octop.api.app import build_app
from octop.config import DatabaseConfig, load_config
from octop.infra.agents.manager import AgentManager
from octop.infra.db.factory import open_database
from octop.infra.db.migrate import run_migrations
from octop.infra.db.rebind import persist_database_config
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.isolation.context import actor_id, actor_scope, enabled
from octop.infra.isolation.policy import IsolationPolicy, WorkerIdentity
from octop.infra.server import OctopServer
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout
from tests.support.auth import TEST_PASSWORD, bearer, login


@pytest.fixture(autouse=True)
def isolation_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OCTOP_USER_ISOLATION", "1")
    monkeypatch.delenv("OCTOP_USER_ISOLATION_STARTUP", raising=False)
    enabled.cache_clear()
    yield
    enabled.cache_clear()


def test_invalid_startup_mode_cannot_select_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCTOP_USER_ISOLATION_STARTUP", "legacy")
    with pytest.raises(RuntimeError, match="strict or repair"):
        IsolationPolicy(PathLayout(tmp_path))


@pytest.mark.parametrize("failure", [RuntimeError("missing setpriv"), OSError("read-only mount")])
def test_failed_repair_blocks_every_worker_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def fail() -> None:
        raise failure

    policy = IsolationPolicy(PathLayout(tmp_path))
    monkeypatch.setattr(policy, "prepare", fail)
    policy.start()
    assert policy.state == "maintenance"
    assert policy.error == str(failure)
    with pytest.raises(OctopError) as error:
        policy.identity(1, "test")
    assert error.value.status == 503
    with pytest.raises(OctopError):
        policy.command(WorkerIdentity(1, 200001, tmp_path, tmp_path), ["id"])
    assert enabled() is True


def test_strict_startup_propagates_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCTOP_USER_ISOLATION_STARTUP", "strict")
    policy = IsolationPolicy(PathLayout(tmp_path))

    def fail() -> None:
        raise RuntimeError("missing capability")

    monkeypatch.setattr(policy, "prepare", fail)
    with pytest.raises(RuntimeError, match="missing capability"):
        policy.start()


@pytest.mark.skipif(sys.platform == "linux", reason="Checks the real non-Linux startup path")
@pytest.mark.asyncio
async def test_non_linux_can_serve_maintenance_without_starting_workers(tmp_path: Path) -> None:
    server = OctopServer(home=tmp_path)
    await server.start()
    try:
        assert server.isolation_policy is not None
        assert server.isolation_policy.state == "maintenance"
        assert "Linux root supervisor" in server.isolation_policy.error
        assert server.app_runtime is None
        assert server.services is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_app(server)), base_url="http://test"
        ) as client:
            assert (await client.get("/")).status_code == 503
            assert (await client.get("/api/health")).json()["isolation"]["ready"] is False
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_mcp_identity_probe_runs_off_loop_with_authenticated_actor(monkeypatch) -> None:
    manager = AgentManager.__new__(AgentManager)
    manager._mcp_tool_cache = {}
    manager._mcp_tool_cache_locks = {}
    manager._mcp_tool_cache_guard = asyncio.Lock()
    checked = []

    def identity(user_id, key):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        assert actor_id() == user_id == 7
        checked.append(key)
        return object()

    manager._user_runtimes = SimpleNamespace(policy=SimpleNamespace(identity=identity))
    monkeypatch.setattr(
        "octop.infra.isolation.backend.UserWorkerBackend",
        lambda *_: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr("octop.infra.isolation.mcp.scope_mcp_spec", lambda spec, _: spec)
    monkeypatch.setattr("octop_harness.mcp.aload_mcp_tools", AsyncMock(return_value=[]))
    with actor_scope(7):
        assert (
            await manager._get_or_load_mcp_tools(
                7, "test", {"transport": "stdio", "command": "tool"}
            )
            == []
        )
    assert len(checked) == 1


async def _seed_accounts(home: Path) -> None:
    config_path = home / "config.json"
    load_config(config_path)
    persist_database_config(config_path, DatabaseConfig())
    config = load_config(config_path)
    db = open_database(config, PathLayout(home))
    try:
        run_migrations(db)
        users = UserManager(build_shared_services(db=db, paths=PathLayout(home), config=config))
        await users.create(username="admin", password=TEST_PASSWORD, role="admin")
        await users.create(username="alice", password=TEST_PASSWORD, role="user")
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("safe_control_plane", [True, False])
async def test_maintenance_http_login_diagnostics_and_execution_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, safe_control_plane: bool
) -> None:
    await _seed_accounts(tmp_path)

    def fail(policy: IsolationPolicy) -> None:
        policy.control_plane_safe = safe_control_plane
        raise RuntimeError("worker runtime is private: /hidden/host/path")

    monkeypatch.setattr(IsolationPolicy, "prepare", fail)
    runtime_boot = AsyncMock(side_effect=AssertionError("runtime must remain stopped"))
    monkeypatch.setattr(OctopServer, "_boot_runtime", runtime_boot)
    monkeypatch.setattr(
        "octop.infra.server.ensure_mobile_capabilities_probed",
        lambda *_: pytest.fail("device probes must not run"),
    )
    monkeypatch.setattr(
        "octop.infra.server.PluginManager.load_installed",
        lambda *_args, **_kw: pytest.fail("plugins must not load"),
    )
    server = OctopServer(home=tmp_path)
    await server.start()
    try:
        assert server.app_runtime is None
        runtime_boot.assert_not_awaited()
        app = build_app(server)
        operation = app.openapi()["paths"]["/api/health/isolation"]["get"]
        assert operation["summary"] == "Administrator user isolation diagnostics"
        assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/IsolationDiagnostics"
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            health = await client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["isolation"] == {"state": "maintenance", "ready": False}
            assert health.json()["agents_running"] == 0
            assert "/hidden/host/path" not in health.text
            page = await client.get("/")
            assert page.status_code == 503
            assert "Maintenance" in page.text
            for path in (
                "/api/agents/COMMON/files",
                "/api/internal/mcp/test",
                "/api/setup/finish",
                "/api/downloads/link/test",
                "/api/connectors/oauth/callback",
                "/api/file-shares/test",
            ):
                response = await client.post(path)
                assert response.status_code == 503, (path, response.text)
                assert "/hidden/host/path" not in response.text
            if safe_control_plane:
                token = await login(client)
                diag = await client.get("/api/health/isolation", headers=bearer(token))
                assert diag.status_code == 200, diag.text
                assert diag.json()["state"] == "maintenance"
                assert "/hidden/host/path" in diag.json()["error"]
                assert (await client.get("/api/auth/me", headers=bearer(token))).status_code == 200
                user_token = await login(client, username="alice")
                assert (
                    await client.get("/api/health/isolation", headers=bearer(user_token))
                ).status_code == 403
                assert (await client.get("/api/health/isolation")).status_code == 401
            else:
                assert (await client.post("/api/auth/login", json={})).status_code == 503
                assert server.services is None
                assert server.user_manager is None
        messages = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            messages.append(message)

        await app({"type": "websocket", "path": "/api/terminal/ws", "headers": []}, receive, send)
        assert messages == [{"type": "websocket.close", "code": 1013}]
        with pytest.raises(OctopError):
            await server.bind_control_plane()
    finally:
        await server.stop()
    assert server.user_manager is None
